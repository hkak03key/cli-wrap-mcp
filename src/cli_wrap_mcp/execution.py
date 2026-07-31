"""sync 実行と実行証跡: サブプロセス実行・出力の返し方 (inline / file) の解決。

実行は常に shell=False の argv 配列。シェル文字列連結の経路は存在しない。
"""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cli_wrap_mcp.runtime import exec_env, new_invocation_id
from cli_wrap_mcp.spec import FILE_EXCERPT_BYTES, STDERR_TAIL_BYTES, ToolSpec


def _truncate(data: bytes, limit: int) -> str:
    """出力を limit バイトで切り詰めたテキストを返す (超過時は末尾に注記)。"""
    text = data.decode("utf-8", errors="replace")
    if len(data) <= limit:
        return text
    truncated = data[:limit].decode("utf-8", errors="replace")
    return f"{truncated}\n[cliwrap: output truncated at {limit} bytes (total {len(data)} bytes)]"


def _invocation_meta(
    tool: ToolSpec,
    argv: list[str],
    started_at: str,
    exit_code: int | None,
    timed_out: bool = False,
) -> dict[str, Any]:
    """meta.json に書く 1 実行分のメタ情報 (何を実行してどう終わったか) を組む。"""
    meta: dict[str, Any] = {
        "tool": tool.name,
        "argv": argv,
        "started_at": started_at,
        "exit_code": exit_code,
    }
    if timed_out:
        meta["timed_out"] = True
    return meta


def _write_invocation_dir(
    tool: ToolSpec,
    parent: Path,
    stdout: bytes,
    stderr: bytes,
    meta: dict[str, Any],
) -> Path:
    """1 実行分の出力一式を <parent>/<tool>-<id>/ に書く (OSError は呼び出し側で処理)。

    stdout.log / stderr.log / meta.json という構成で、job モードの job dir と
    レイアウトを揃えている。meta.json (argv・時刻・exit code) があることで
    「何を実行してこの出力が出たか」までが証跡として残る。
    """
    parent.mkdir(parents=True, exist_ok=True)
    inv_dir = parent / f"{tool.name}-{new_invocation_id()}"
    inv_dir.mkdir()
    (inv_dir / "stdout.log").write_bytes(stdout)
    (inv_dir / "stderr.log").write_bytes(stderr)
    (inv_dir / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False), encoding="utf-8",
    )
    return inv_dir


def _file_reply(data: bytes, inv_dir: Path, reason: str = "") -> str:
    """全量ファイルへの参照+抜粋だけを返す (呼び出し側 context の節約)。

    抜粋は同じ内容を二度返さない: 全量が FILE_EXCERPT_BYTES 以下なら本文を枠なしで
    一度だけ返し (応答が全量なので「全部読むな」の助言も省く)、head と tail が
    重なるサイズでは tail を head の続きから始めて中間部の重複を消す。
    """
    header = (
        f"[cliwrap: output is {len(data)} bytes{reason}; full output saved to file]\n"
        f"file: {inv_dir / 'stdout.log'}\n"
        f"(stderr.log and meta.json with the executed argv are in the same directory)\n"
    )
    if len(data) <= FILE_EXCERPT_BYTES:
        return header + data.decode("utf-8", errors="replace")
    head = data[:FILE_EXCERPT_BYTES]
    tail = data[max(FILE_EXCERPT_BYTES, len(data) - FILE_EXCERPT_BYTES):]
    return (
        f"{header}"
        f"Do not read it whole: use Read with offset/limit, or grep, to inspect parts.\n"
        f"--- head ({len(head)} bytes) ---\n{head.decode('utf-8', errors='replace')}\n"
        f"--- tail ({len(tail)} bytes) ---\n{tail.decode('utf-8', errors='replace')}"
    )


def _stderr_tail(stderr: bytes) -> str:
    """stderr の末尾 (エラー要約向け) をテキストで返す。"""
    return stderr[-STDERR_TAIL_BYTES:].decode("utf-8", errors="replace")


def run_sync(
    tool: ToolSpec,
    argv: list[str],
    file_dir: Path | None = None,
    call_dir: Path | None = None,
) -> str:
    """コマンドを同期実行し、出力の返し方 (inline / truncate / file) を解決した応答を返す。"""
    started_at = datetime.now(timezone.utc).isoformat()
    # per-call 指定 (call_dir = 予約 param file_output_dir) または file mode では、
    # 成否・サイズに関係なく常に全量をファイル化する (証跡: 失敗した実行も記録に残す)
    dest = call_dir if call_dir is not None else (
        file_dir if tool.output_mode == "file" else None
    )
    try:
        proc = subprocess.run(
            argv,
            shell=False,
            capture_output=True,
            timeout=tool.timeout_sec,
            env=exec_env(tool),
        )
    except subprocess.TimeoutExpired as exc:
        msg = f"error: command timed out after {tool.timeout_sec}s: {argv!r}"
        if dest is not None:
            # timeout でも捕捉済みの部分出力を best-effort で証跡に残す
            meta = _invocation_meta(tool, argv, started_at, None, timed_out=True)
            try:
                inv_dir = _write_invocation_dir(
                    tool, dest, exc.stdout or b"", exc.stderr or b"", meta,
                )
                msg += f"\npartial output saved to: {inv_dir}"
            except OSError as write_exc:
                msg += f"\n(failed to save partial output: {write_exc})"
        return msg
    except OSError as exc:
        return f"error: failed to execute {argv!r}: {exc}"
    if dest is not None:
        meta = _invocation_meta(tool, argv, started_at, proc.returncode)
        try:
            inv_dir = _write_invocation_dir(tool, dest, proc.stdout, proc.stderr, meta)
        except OSError as exc:
            return f"error: failed to write output to {dest}: {exc}"
        if proc.returncode != 0:
            return (
                f"error: command exited with code {proc.returncode}\n"
                f"output saved to: {inv_dir}\n"
                f"stderr (tail):\n{_stderr_tail(proc.stderr)}"
            )
        return _file_reply(proc.stdout, inv_dir)
    if proc.returncode != 0:
        return (
            f"error: command exited with code {proc.returncode}\n"
            f"stderr (tail):\n{_stderr_tail(proc.stderr)}"
        )
    if (
        len(proc.stdout) > tool.inline_max_output_bytes
        and tool.inline_on_large_output == "file"
        and file_dir is not None
    ):
        meta = _invocation_meta(tool, argv, started_at, proc.returncode)
        try:
            inv_dir = _write_invocation_dir(tool, file_dir, proc.stdout, proc.stderr, meta)
        except OSError as exc:
            print(
                f"cliwrap: file output failed ({exc}); falling back to truncate",
                file=sys.stderr,
            )
            return _truncate(proc.stdout, tool.inline_max_output_bytes)
        return _file_reply(
            proc.stdout, inv_dir, reason=f" (> {tool.inline_max_output_bytes})",
        )
    return _truncate(proc.stdout, tool.inline_max_output_bytes)
