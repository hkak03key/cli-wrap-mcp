"""job モード (長時間 CLI の非同期実行): job の起動・状態確認・結果取得・キャンセル。"""
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from cli_wrap_mcp.execution import exec_env
from cli_wrap_mcp.spec import STDERR_TAIL_BYTES, ParamValidationError, ToolSpec

JOB_ID_RE = re.compile(r"^[0-9]{8}T[0-9]{6}-[0-9a-f]{6}$")


def _tail_file(path: Path, limit: int) -> str:
    """ファイル末尾 limit バイトをテキストで返す (読めなければ空文字列)。"""
    try:
        with open(path, "rb") as fp:
            fp.seek(0, os.SEEK_END)
            size = fp.tell()
            fp.seek(max(0, size - limit))
            data = fp.read()
    except OSError:
        return ""
    prefix = "...(truncated)...\n" if size > limit else ""
    return prefix + data.decode("utf-8", errors="replace")


class JobManager:
    """job モードの状態管理 (MVP)。

    サーバープロセス内の Popen 管理を主とし、出力・pid・メタ情報は
    <jobs_dir>/<job_id>/ のファイルに残す (jobs_dir は tool の出力ルート配下の
    jobs/。既定は cache)。サーバー再起動後の孤児 job は best-effort
    (pid 生存確認と exit_code ファイル) でのみ参照できる。
    """

    def __init__(self, jobs_dir: Path):
        """jobs_dir (job ごとのファイルを置くルート) を受け取る。"""
        self.jobs_dir = jobs_dir
        self._procs: dict[str, subprocess.Popen] = {}

    def _job_dir(self, job_id: str) -> Path:
        """job_id を検証して job dir のパスを返す。"""
        # job_id は client 入力なので、パストラバーサルを形式検証で遮断する
        if not JOB_ID_RE.match(job_id):
            raise ParamValidationError(f"invalid job_id: {job_id!r}")
        return self.jobs_dir / job_id

    def start(self, tool: ToolSpec, argv: list[str]) -> str:
        """コマンドをバックグラウンド起動し、job_id と操作方法を返す。"""
        job_id = time.strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:6]
        jdir = self.jobs_dir / job_id
        jdir.mkdir(parents=True)
        with open(jdir / "stdout.log", "wb") as out_fp, open(jdir / "stderr.log", "wb") as err_fp:
            try:
                proc = subprocess.Popen(
                    argv,
                    shell=False,
                    stdin=subprocess.DEVNULL,
                    stdout=out_fp,
                    stderr=err_fp,
                    start_new_session=True,  # cancel で process group ごと止めるため
                    env=exec_env(tool),
                )
            except OSError as exc:
                return f"error: failed to start {argv!r}: {exc}"
        (jdir / "pid").write_text(str(proc.pid), encoding="utf-8")
        meta = {
            "tool": tool.name,
            "argv": argv,
            "pid": proc.pid,
            "started_at": datetime.now(timezone.utc).isoformat(),
        }
        (jdir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        self._procs[job_id] = proc
        print(f"cliwrap: job {job_id} started: {argv!r}", file=sys.stderr)
        return (
            f"job started: {job_id}\n"
            f"logs: {jdir}\n"
            f"use {tool.name}_status / {tool.name}_result / {tool.name}_cancel with this job_id"
        )

    def _poll(self, job_id: str) -> tuple[str, int | None]:
        """('running' | 'exited' | 'unknown', exit_code) を返す。"""
        jdir = self._job_dir(job_id)
        proc = self._procs.get(job_id)
        if proc is not None:
            rc = proc.poll()
            if rc is None:
                return "running", None
            exit_file = jdir / "exit_code"
            if not exit_file.exists():
                exit_file.write_text(str(rc), encoding="utf-8")
            return "exited", rc
        # fallback: サーバー再起動などでプロセスハンドルを失った job (best-effort)
        if not jdir.is_dir():
            raise ParamValidationError(f"unknown job_id: {job_id!r}")
        exit_file = jdir / "exit_code"
        if exit_file.exists():
            return "exited", int(exit_file.read_text())
        try:
            pid = int((jdir / "pid").read_text())
            os.kill(pid, 0)
            return "running", None
        except (OSError, ValueError):
            return "unknown", None

    def status(self, job_id: str, tail_bytes: int = 2_000) -> str:
        """job の状態と stdout/stderr の末尾を返す。"""
        state, rc = self._poll(job_id)
        jdir = self._job_dir(job_id)
        lines = [f"job {job_id}: {state}" + (f" (exit code {rc})" if rc is not None else "")]
        if state == "unknown":
            lines.append(
                "(process handle lost and no exit code recorded; "
                "the server may have restarted while the job was running)"
            )
        stdout_tail = _tail_file(jdir / "stdout.log", tail_bytes)
        stderr_tail = _tail_file(jdir / "stderr.log", tail_bytes)
        lines.append(f"stdout (tail):\n{stdout_tail or '(empty)'}")
        if stderr_tail:
            lines.append(f"stderr (tail):\n{stderr_tail}")
        return "\n".join(lines)

    def result(self, job_id: str, max_bytes: int) -> str:
        """終了した job の出力 (末尾 max_bytes) を返す。実行中なら案内のみ返す。"""
        state, rc = self._poll(job_id)
        if state == "running":
            return f"job {job_id} is still running; try again later (or check _status)"
        jdir = self._job_dir(job_id)
        header = f"job {job_id}: {state}" + (f" (exit code {rc})" if rc is not None else "")
        stdout = _tail_file(jdir / "stdout.log", max_bytes) or "(empty)"
        parts = [header, f"stdout:\n{stdout}"]
        if rc not in (0, None):
            parts.append(f"stderr (tail):\n{_tail_file(jdir / 'stderr.log', STDERR_TAIL_BYTES)}")
        return "\n".join(parts)

    def cancel(self, job_id: str) -> str:
        """実行中の job にプロセスグループごと SIGTERM を送る。"""
        state, rc = self._poll(job_id)
        if state == "exited":
            return f"job {job_id} already exited (exit code {rc})"
        jdir = self._job_dir(job_id)
        try:
            pid = int((jdir / "pid").read_text())
        except (OSError, ValueError) as exc:
            return f"error: cannot read pid for job {job_id}: {exc}"
        try:
            # start_new_session で起動しているので pid == pgid。グループごと止める
            os.killpg(pid, signal.SIGTERM)
        except ProcessLookupError:
            return f"job {job_id} is not running (process already gone)"
        except OSError as exc:
            return f"error: failed to terminate job {job_id}: {exc}"
        return f"job {job_id}: SIGTERM sent to process group {pid}"
