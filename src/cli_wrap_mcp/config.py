"""YAML config のロードと検証: ファイルから ServerSpec を組み立てる。

argv 内の未定義プレースホルダ・型と default の不整合・ツール名の衝突など、
呼び出し時ではなくロード時に検出できる誤りはすべてここで ConfigError にする。
"""
from __future__ import annotations

import keyword
import re
import sys
from pathlib import Path
from typing import Any

import yaml

from cli_wrap_mcp.rendering import placeholders
from cli_wrap_mcp.spec import (
    DEFAULT_INLINE_MAX_OUTPUT_BYTES,
    DEFAULT_TIMEOUT_SEC,
    ENV_NAME_RE,
    INLINE_ON_LARGE_OUTPUT_MODES,
    OUTPUT_MODES,
    PARAM_NAME_RE,
    PY_TYPES,
    RESERVED_PARAM_NAMES,
    SUPPORTED_MODES,
    TOOL_NAME_RE,
    ANNOTATIONS,
    ConfigError,
    Defaults,
    ParamSpec,
    ServerSpec,
    ToolSpec,
)


def _load_param(tool_name: str, pname: str, raw: dict[str, Any]) -> ParamSpec:
    """params の 1 エントリを検証して ParamSpec にする。"""
    ctx = f"tools.{tool_name}.params.{pname}"
    if not isinstance(pname, str) or not PARAM_NAME_RE.match(pname) or keyword.iskeyword(pname):
        raise ConfigError(f"{ctx}: invalid param name (must match {PARAM_NAME_RE.pattern})")
    if pname in RESERVED_PARAM_NAMES:
        raise ConfigError(
            f"{ctx}: param name {pname!r} is reserved (injected by the engine)"
        )
    ptype = raw.get("type", "string")
    if ptype not in ANNOTATIONS:
        raise ConfigError(f"{ctx}: unknown type {ptype!r} (expected one of {sorted(ANNOTATIONS)})")
    unknown = set(raw) - {
        "type", "description", "required", "pattern", "deny_pattern", "enum", "default",
        "allow_dash_prefix",
    }
    if unknown:
        raise ConfigError(f"{ctx}: unknown keys {sorted(unknown)}")
    spec = ParamSpec(
        name=pname,
        type=ptype,
        description=raw.get("description", ""),
        required=raw.get("required", True),
        pattern=raw.get("pattern"),
        deny_pattern=raw.get("deny_pattern"),
        enum=raw.get("enum"),
        default=raw.get("default"),
        allow_dash_prefix=raw.get("allow_dash_prefix", False),
    )
    for attr in ("pattern", "deny_pattern"):
        regex = getattr(spec, attr)
        if regex is None:
            continue
        if spec.type not in ("string", "array"):
            raise ConfigError(f"{ctx}: {attr} is only supported for string/array params")
        try:
            re.compile(regex)
        except re.error as exc:
            raise ConfigError(f"{ctx}: invalid {attr}: {exc}") from exc
    if spec.type == "array":
        # array の enum / pattern / deny_pattern / dash guard は各 item (常に str) に適用
        if spec.enum is not None and not all(isinstance(i, str) for i in spec.enum):
            raise ConfigError(f"{ctx}: enum values for array params must be strings")
        if spec.default is not None and (
            not isinstance(spec.default, list)
            or not all(isinstance(i, str) for i in spec.default)
        ):
            raise ConfigError(f"{ctx}: default for array params must be a list of strings")
        if not spec.required and spec.default is None:
            spec.default = []  # 空展開 (0 要素) は well-defined なので暗黙 default にできる
        return spec
    py_type = PY_TYPES[spec.type]
    if spec.enum is not None:
        for item in spec.enum:
            if not isinstance(item, py_type) or (py_type is int and isinstance(item, bool)):
                raise ConfigError(f"{ctx}: enum value {item!r} does not match type {ptype}")
    if spec.default is not None:
        if not isinstance(spec.default, py_type) or (
            py_type is int and isinstance(spec.default, bool)
        ):
            raise ConfigError(f"{ctx}: default {spec.default!r} does not match type {ptype}")
    return spec


def _check_choice(ctx: str, key: str, value: Any, allowed: set[str]) -> None:
    """値が選択肢集合に含まれることを検査する。"""
    if value not in allowed:
        raise ConfigError(f"{ctx}: {key} must be one of {sorted(allowed)}, got {value!r}")


def _load_file_output_dir(ctx: str, raw: Any) -> str | None:
    """`file_output_dir:` (ファイル出力のルート) を検証して返す。"""
    if raw is None:
        return None
    if not isinstance(raw, str) or not raw.startswith("/"):
        raise ConfigError(f"{ctx}: file_output_dir must be an absolute path, got {raw!r}")
    return raw


def _load_env(ctx: str, raw: Any) -> dict[str, str]:
    """`env:` セクション (環境変数名 -> 値) を検証して返す。"""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{ctx}: env must be a mapping of VAR_NAME -> string")
    env: dict[str, str] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not ENV_NAME_RE.match(key):
            raise ConfigError(
                f"{ctx}: invalid env var name {key!r} (must match {ENV_NAME_RE.pattern})"
            )
        if not isinstance(value, str):
            raise ConfigError(
                f"{ctx}: env value for {key!r} must be a string (quote numbers in YAML)"
            )
        env[key] = value
    return env


def _load_tool(
    raw: dict[str, Any],
    defaults: Defaults | None = None,
) -> ToolSpec:
    """tools の 1 エントリを検証して ToolSpec にする (defaults の継承もここで解決)。"""
    defaults = defaults or Defaults()
    name = raw.get("name")
    if not name or not isinstance(name, str) or not TOOL_NAME_RE.match(name):
        raise ConfigError(f"tools[].name is required and must match {TOOL_NAME_RE.pattern}: {name!r}")
    ctx = f"tools.{name}"
    unknown = set(raw) - {
        "name", "description", "argv", "mode", "timeout_sec", "inline_max_output_bytes",
        "output_mode", "inline_on_large_output", "file_output_dir", "params", "env",
    }
    if unknown:
        raise ConfigError(f"{ctx}: unknown keys {sorted(unknown)}")
    output_mode = raw.get("output_mode", defaults.output_mode)
    _check_choice(ctx, "output_mode", output_mode, OUTPUT_MODES)
    inline_on_large_output = raw.get("inline_on_large_output", defaults.inline_on_large_output)
    _check_choice(ctx, "inline_on_large_output", inline_on_large_output, INLINE_ON_LARGE_OUTPUT_MODES)
    argv = raw.get("argv")
    if not argv or not isinstance(argv, list) or not all(isinstance(a, str) for a in argv):
        raise ConfigError(f"{ctx}: argv must be a non-empty list of strings")
    mode = raw.get("mode", "sync")
    if mode not in SUPPORTED_MODES:
        raise ConfigError(f"{ctx}: unknown mode {mode!r} (expected one of {sorted(SUPPORTED_MODES)})")
    params_raw = raw.get("params") or {}
    if not isinstance(params_raw, dict) or not all(
        isinstance(p, dict) for p in params_raw.values()
    ):
        raise ConfigError(f"{ctx}: params must be a mapping of param name -> mapping")
    params = {
        pname: _load_param(name, pname, praw) for pname, praw in params_raw.items()
    }
    tool = ToolSpec(
        name=name,
        description=raw.get("description", ""),
        argv=list(argv),
        mode=mode,
        timeout_sec=raw.get("timeout_sec", DEFAULT_TIMEOUT_SEC),
        inline_max_output_bytes=raw.get(
            "inline_max_output_bytes", defaults.inline_max_output_bytes,
        ),
        output_mode=output_mode,
        inline_on_large_output=inline_on_large_output,
        file_output_dir=(
            _load_file_output_dir(ctx, raw.get("file_output_dir"))
            or defaults.file_output_dir
        ),
        params=params,
        # tool の env は server 全体の defaults.env の上にマージ (同名キーは tool 側が勝つ)
        env={**defaults.env, **_load_env(ctx, raw.get("env"))},
    )

    referenced: set[str] = set()
    for element in tool.argv:
        try:
            names = placeholders(element)
        except ConfigError as exc:
            raise ConfigError(f"{ctx}: {exc}") from exc
        referenced.update(names)
        # array param は N 要素に展開されるため、要素全体が placeholder のときだけ許可
        # ("--x={args}" のような埋め込みは 1 要素に潰れてしまい意味が壊れる)
        for n in names:
            pspec = params.get(n)
            if pspec is not None and pspec.type == "array" and element != "{" + n + "}":
                raise ConfigError(
                    f"{ctx}: array param {n!r} placeholder must be the entire "
                    f"argv element, got {element!r}"
                )
    undefined = referenced - set(params)
    if undefined:
        raise ConfigError(f"{ctx}: undefined placeholders in argv: {sorted(undefined)}")
    for pname in sorted(set(params) - referenced):
        print(f"cliwrap: warning: {ctx}: param {pname!r} is never used in argv", file=sys.stderr)
    # 省略可能 (required=false) かつ default なしのパラメータは argv を組めないので禁止する
    for pname in sorted(referenced):
        spec = params[pname]
        if not spec.required and not spec.has_default:
            raise ConfigError(
                f"{ctx}: param {pname!r} is optional but has no default; "
                "argv rendering would be undefined"
            )
    return tool


def _load_defaults(raw: Any) -> Defaults:
    """`defaults:` セクションを検証して Defaults にする。"""
    if raw is None:
        return Defaults()
    if not isinstance(raw, dict):
        raise ConfigError("'defaults' must be a mapping")
    unknown = set(raw) - {
        "output_mode", "inline_max_output_bytes", "inline_on_large_output",
        "file_output_dir", "env",
    }
    if unknown:
        raise ConfigError(f"defaults: unknown keys {sorted(unknown)}")
    defaults = Defaults(
        output_mode=raw.get("output_mode", "inline"),
        inline_max_output_bytes=raw.get(
            "inline_max_output_bytes", DEFAULT_INLINE_MAX_OUTPUT_BYTES,
        ),
        inline_on_large_output=raw.get("inline_on_large_output", "truncate"),
        file_output_dir=_load_file_output_dir("defaults", raw.get("file_output_dir")),
        env=_load_env("defaults", raw.get("env")),
    )
    _check_choice("defaults", "output_mode", defaults.output_mode, OUTPUT_MODES)
    _check_choice(
        "defaults", "inline_on_large_output",
        defaults.inline_on_large_output, INLINE_ON_LARGE_OUTPUT_MODES,
    )
    return defaults


def load_config(path: str | Path) -> ServerSpec:
    """YAML config を読み、検証済みの ServerSpec を返す。"""
    with open(path, encoding="utf-8") as fp:
        raw = yaml.safe_load(fp)
    if not isinstance(raw, dict):
        raise ConfigError("config must be a YAML mapping")
    server_raw = raw.get("server")
    if not isinstance(server_raw, dict) or not server_raw.get("name"):
        raise ConfigError("'server' section with name is required")
    server = ServerSpec(
        name=server_raw["name"],
        description=server_raw.get("description", ""),
    )
    defaults = _load_defaults(raw.get("defaults"))
    tools_raw = raw.get("tools") or []
    if not isinstance(tools_raw, list):
        raise ConfigError("'tools' must be a list")
    for tool_raw in tools_raw:
        if not isinstance(tool_raw, dict):
            raise ConfigError("each tools entry must be a mapping")
        tool = _load_tool(tool_raw, defaults=defaults)
        if tool.name in server.tools:
            raise ConfigError(f"duplicate tool name: {tool.name}")
        server.tools[tool.name] = tool
    if not server.tools:
        raise ConfigError("at least one tools entry is required")
    # MCP に実際に登録される名前 (job は _start 等を生成) の衝突をロード時に検出する
    exposed: set[str] = set()
    for tool in server.tools.values():
        if tool.mode == "job":
            names = [f"{tool.name}_{suffix}" for suffix in ("start", "status", "result", "cancel")]
        else:
            names = [tool.name]
        for n in names:
            if n in exposed:
                raise ConfigError(f"exposed tool name collision: {n!r}")
            exposed.add(n)
    return server
