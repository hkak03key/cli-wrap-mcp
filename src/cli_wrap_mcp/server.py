"""MCP サーバー組み立て: ServerSpec から FastMCP サーバーとツール群を構成する。"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

from cli_wrap_mcp.execution import run_sync
from cli_wrap_mcp.jobs import JobManager
from cli_wrap_mcp.rendering import render_argv
from cli_wrap_mcp.spec import (
    ANNOTATIONS,
    JOB_TOOL_SUFFIXES,
    ConfigError,
    ParamValidationError,
    ServerSpec,
    ToolSpec,
)


# job 補助ツールの annotations はエンジンが固定する (config の宣言は _start だけに効く)。
# 状態を読むだけの _status / _result は read-only、プロセスを止める _cancel はそうではない
JOB_AUX_ANNOTATIONS: dict[str, dict[str, Any]] = {
    "status": {"readOnlyHint": True},
    "result": {"readOnlyHint": True},
    "cancel": {"readOnlyHint": False},
}


def default_cache_dir() -> Path:
    """出力ルート未指定時の既定 cache dir (CLI_MCP_CACHE_DIR で上書き可) を返す。"""
    env = os.environ.get("CLI_MCP_CACHE_DIR")
    return Path(env) if env else Path.home() / ".cache" / "cli-mcp"


def _make_tool_fn(fn_name: str, tool: ToolSpec, invoke, inject_output_dir: bool = False):
    """config のパラメータ定義から、FastMCP がスキーマ推論できる関数を生成する。

    FastMCP は関数シグネチャから inputSchema を作るため、exec で実シグネチャを持つ
    関数を組み立てる。パラメータ名は config ロード時に識別子として検証済み。
    inject_output_dir=True で予約パラメータ file_output_dir (optional) を注入する。
    """
    required_args: list[str] = []
    optional_args: list[str] = []
    for pname, spec in tool.params.items():
        ann = ANNOTATIONS[spec.type]
        if not (spec.has_default or not spec.required):
            required_args.append(f"{pname}: {ann}")
        elif spec.type == "array":
            # mutable default を避けて None を sentinel にする (render_argv が default に解決)
            optional_args.append(f"{pname}: list[str] | None = None")
        else:
            optional_args.append(f"{pname}: {ann} = {spec.default!r}")
    if inject_output_dir:
        optional_args.append("file_output_dir: str | None = None")
    signature = ", ".join(required_args + optional_args)
    src = (
        f"def {fn_name}({signature}) -> str:\n"
        f"    kwargs = dict(locals())\n"
        f"    return _invoke(kwargs)\n"
    )
    namespace: dict[str, Any] = {"_invoke": invoke}
    exec(src, namespace)  # noqa: S102 - config は信頼済みローカルファイル
    return namespace[fn_name]


def _annotations(values: dict[str, Any]):
    """検証済みの annotations 宣言を MCP の ToolAnnotations にする (空なら None)。"""
    if not values:
        return None
    from mcp.types import ToolAnnotations

    return ToolAnnotations(**values)


def build_server(spec: ServerSpec, cache_dir: Path | None = None):
    """ServerSpec の全ツールを登録した FastMCP サーバーを返す。"""
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP(spec.name, instructions=spec.description)
    server_cache_dir = (cache_dir or default_cache_dir()) / spec.name
    # 出力ルートは tool の file_output_dir (未指定は cache)。sync は <root>/outputs/、
    # job は <root>/jobs/ と、証跡が同じルート配下に集約される
    jobs_by_root: dict[Path, JobManager] = {}
    for tool in spec.tools.values():
        root = Path(tool.file_output_dir) if tool.file_output_dir else server_cache_dir
        jobs: JobManager | None = None
        if tool.mode == "job":
            jobs = jobs_by_root.setdefault(root, JobManager(root / "jobs"))
        register_tool(mcp, spec, tool, jobs, root / "outputs")
    return mcp


def register_tool(
    mcp,
    server_spec: ServerSpec,
    tool: ToolSpec,
    jobs: JobManager | None,
    file_dir: Path | None = None,
) -> None:
    """tool の mode に応じた MCP ツール (sync は 1 つ、job は 4 つ) を登録する。"""
    if tool.mode == "sync":
        def invoke(arguments: dict[str, Any], _tool=tool) -> str:
            call_dir_arg = arguments.pop("file_output_dir", None)
            call_dir: Path | None = None
            if call_dir_arg is not None:
                if not str(call_dir_arg).startswith("/"):
                    return "error: file_output_dir must be an absolute path (starting with '/')"
                call_dir = Path(call_dir_arg)
            try:
                argv = render_argv(_tool, arguments)
            except ParamValidationError as exc:
                return f"error: {exc}"
            print(f"cliwrap: exec {argv!r}", file=sys.stderr)
            return run_sync(_tool, argv, file_dir=file_dir, call_dir=call_dir)

        fn = _make_tool_fn(f"tool_{tool.name}", tool, invoke, inject_output_dir=True)
        description = (
            _tool_description(tool)
            + "\n- file_output_dir (string, optional) — absolute directory path; if set, "
            "the full output is always written under it (regardless of size or exit "
            "code) and only the file path + excerpts are returned"
        )
        mcp.add_tool(
            fn, name=tool.name, description=description,
            annotations=_annotations(tool.annotations),
        )
    elif tool.mode == "job":
        assert jobs is not None
        register_job_tool(mcp, tool, jobs)
    else:  # pragma: no cover - モードはロード時に検証済み
        raise ConfigError(f"unsupported mode: {tool.mode}")


def register_job_tool(mcp, tool: ToolSpec, jobs: JobManager) -> None:
    """job モードのツール一式 (<name>_start/_status/_result/_cancel) を登録する。"""

    def invoke_start(arguments: dict[str, Any], _tool=tool) -> str:
        try:
            argv = render_argv(_tool, arguments)
        except ParamValidationError as exc:
            return f"error: {exc}"
        return jobs.start(_tool, argv)

    start_fn = _make_tool_fn(f"tool_{tool.name}_start", tool, invoke_start)

    def status_fn(job_id: str) -> str:
        try:
            return jobs.status(job_id)
        except ParamValidationError as exc:
            return f"error: {exc}"

    def result_fn(job_id: str) -> str:
        try:
            return jobs.result(job_id, tool.inline_max_output_bytes)
        except ParamValidationError as exc:
            return f"error: {exc}"

    def cancel_fn(job_id: str) -> str:
        try:
            return jobs.cancel(job_id)
        except ParamValidationError as exc:
            return f"error: {exc}"

    # 公開名は spec.JOB_TOOL_SUFFIXES から導出する (config のロード時衝突検査と同じ一覧)
    handlers = {
        "start": (
            start_fn,
            _tool_description(tool)
            + "\nStarts the command as a background job and returns a job_id immediately.",
        ),
        "status": (
            status_fn,
            f"Check a background job started by {tool.name}_start: "
            "running/exited state plus stdout/stderr tail.",
        ),
        "result": (
            result_fn,
            f"Fetch the output of a finished job started by {tool.name}_start "
            "(tail-limited). Returns a notice if the job is still running.",
        ),
        "cancel": (
            cancel_fn,
            f"Cancel a running job started by {tool.name}_start "
            "(SIGTERM to the process group).",
        ),
    }
    if set(handlers) != set(JOB_TOOL_SUFFIXES):
        raise ConfigError(
            f"job tool handlers {sorted(handlers)} do not match "
            f"JOB_TOOL_SUFFIXES {sorted(JOB_TOOL_SUFFIXES)}"
        )
    if set(JOB_AUX_ANNOTATIONS) != set(JOB_TOOL_SUFFIXES) - {"start"}:
        raise ConfigError(
            f"job aux annotations {sorted(JOB_AUX_ANNOTATIONS)} do not cover "
            f"JOB_TOOL_SUFFIXES {sorted(JOB_TOOL_SUFFIXES)} except 'start'"
        )
    for suffix in JOB_TOOL_SUFFIXES:
        fn, description = handlers[suffix]
        mcp.add_tool(
            fn, name=f"{tool.name}_{suffix}", description=description,
            # 宣言が効くのは実際にコマンドを起動する _start だけ。補助 3 つは固定値
            annotations=_annotations(
                tool.annotations if suffix == "start" else JOB_AUX_ANNOTATIONS[suffix]
            ),
        )


def _tool_description(tool: ToolSpec) -> str:
    """ツールの description (説明とパラメータ一覧) を組み立てる。"""
    lines = [tool.description or tool.name]
    for pname, spec in tool.params.items():
        parts = ["array of strings" if spec.type == "array" else spec.type]
        if not spec.required or spec.has_default:
            parts.append(f"optional, default={spec.default!r}")
        if spec.enum is not None:
            parts.append(f"enum={spec.enum}")
        desc = f" — {spec.description}" if spec.description else ""
        lines.append(f"- {pname} ({', '.join(parts)}){desc}")
    return "\n".join(lines)
