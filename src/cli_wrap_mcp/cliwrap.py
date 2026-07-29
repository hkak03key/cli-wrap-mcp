"""cliwrap: 宣言的 YAML config から CLI ラップ MCP サーバーを動的生成するエンジン。

使い方:
    cli-wrap-mcp --config <path.yml>

設計原則 (安全性がこの仕組みの核):
- 実行は常に shell=False の argv 配列。シェル文字列連結の経路は存在しない
- パラメータ値は検証 (type / pattern fullmatch / deny_pattern / enum) を通過してから
  argv 要素に埋め込む。array param は要素全体の placeholder のみに展開を許し、
  各 item に同じ検証を適用する
- 引数インジェクション対策: `-` で始まる値は既定で拒否 (per-param の
  allow_dash_prefix = true で明示的に許可可能)
- config ロード時に argv 内の未定義プレースホルダはエラー
- stdout は MCP プロトコル専用。ログ・デバッグ出力は必ず stderr へ
"""
from __future__ import annotations

import argparse
import json
import keyword
import os
import re
import signal
import string
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEFAULT_TIMEOUT_SEC = 60
DEFAULT_INLINE_MAX_OUTPUT_BYTES = 50_000
STDERR_TAIL_BYTES = 2_000
FILE_EXCERPT_BYTES = 1_000
# 出力の返し方:
# - inline: 応答にそのまま含める。上限 (inline_max_output_bytes) 超過時の挙動は
#           inline_on_large_output (truncate: 切り詰め / file: ファイルへ書き出し)
# - file:   成否やサイズに関係なく常にファイルへ全量書き出し (証跡用途)、
#           応答はパス + 抜粋のみ
OUTPUT_MODES = {"inline", "file"}
INLINE_ON_LARGE_OUTPUT_MODES = {"truncate", "file"}

PARAM_NAME_RE = re.compile(r"^[a-z_][a-z0-9_]*$")
TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# エンジンが全 sync ツールに自動注入するパラメータ名 (config での定義は禁止)
RESERVED_PARAM_NAMES = {"file_output_dir"}

# scalar 型の Python 型 (enum / default の型検査に使用)。array は別扱い (items は常に str)
PY_TYPES: dict[str, type] = {"string": str, "integer": int, "boolean": bool}

# exec 生成する関数シグネチャの型注釈 (このキー集合が合法な param type の全て)
ANNOTATIONS: dict[str, str] = {
    "string": "str",
    "integer": "int",
    "boolean": "bool",
    "array": "list[str]",
}


class ConfigError(Exception):
    """config が不正なときにロード時に送出する。"""


class ParamValidationError(Exception):
    """ツール呼び出し時のパラメータ検証エラー。"""


@dataclass
class ParamSpec:
    name: str
    type: str = "string"
    description: str = ""
    required: bool = True
    pattern: str | None = None
    deny_pattern: str | None = None
    enum: list[Any] | None = None
    default: Any = None
    allow_dash_prefix: bool = False

    @property
    def has_default(self) -> bool:
        return self.default is not None


@dataclass
class ToolSpec:
    name: str
    description: str
    argv: list[str]
    mode: str = "sync"
    timeout_sec: int = DEFAULT_TIMEOUT_SEC
    inline_max_output_bytes: int = DEFAULT_INLINE_MAX_OUTPUT_BYTES
    output_mode: str = "inline"
    inline_on_large_output: str = "truncate"
    file_output_dir: str | None = None
    params: dict[str, ParamSpec] = field(default_factory=dict)
    env: dict[str, str] = field(default_factory=dict)


@dataclass
class Defaults:
    """`defaults:` セクション (tool 側で未指定のときに使う server 全体の既定値)。"""
    output_mode: str = "inline"
    inline_max_output_bytes: int = DEFAULT_INLINE_MAX_OUTPUT_BYTES
    inline_on_large_output: str = "truncate"
    file_output_dir: str | None = None
    env: dict[str, str] = field(default_factory=dict)


@dataclass
class ServerSpec:
    name: str
    description: str = ""
    tools: dict[str, ToolSpec] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# config ロード
# ---------------------------------------------------------------------------

def _placeholders(template: str) -> list[str]:
    """format テンプレート中のプレースホルダ名を列挙する。

    format spec / conversion / ネストしたフィールドアクセスは安全のため禁止する。
    """
    names: list[str] = []
    for _literal, field_name, format_spec, conversion in string.Formatter().parse(template):
        if field_name is None:
            continue
        if field_name == "":
            raise ConfigError(f"positional placeholder '{{}}' is not allowed: {template!r}")
        if format_spec or conversion:
            raise ConfigError(
                f"format spec / conversion is not allowed in placeholder: {template!r}"
            )
        if "." in field_name or "[" in field_name:
            raise ConfigError(
                f"attribute/index access is not allowed in placeholder: {template!r}"
            )
        names.append(field_name)
    return names


def _load_param(tool_name: str, pname: str, raw: dict[str, Any]) -> ParamSpec:
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
            names = _placeholders(element)
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


# ---------------------------------------------------------------------------
# パラメータ検証と argv レンダリング
# ---------------------------------------------------------------------------

def _validate_item(spec: ParamSpec, item: Any, index: int) -> str:
    """array param の 1 item を検証する (enum / pattern / deny / dash guard は per-item)。"""
    label = f"{spec.name!r}[{index}]"
    if not isinstance(item, str):
        raise ParamValidationError(
            f"param {label}: expected string item, got {type(item).__name__}"
        )
    if spec.enum is not None and item not in spec.enum:
        raise ParamValidationError(f"param {label}: value {item!r} is not in enum {spec.enum}")
    if spec.pattern is not None and not re.fullmatch(spec.pattern, item):
        raise ParamValidationError(
            f"param {label}: value {item!r} does not match pattern {spec.pattern!r}"
        )
    if spec.deny_pattern is not None and re.fullmatch(spec.deny_pattern, item):
        raise ParamValidationError(
            f"param {label}: value {item!r} is denied by deny_pattern {spec.deny_pattern!r}"
        )
    if item.startswith("-") and not spec.allow_dash_prefix:
        raise ParamValidationError(
            f"param {label}: value starting with '-' is rejected "
            "(set allow_dash_prefix = true in config to allow)"
        )
    return item


def validate_param(spec: ParamSpec, value: Any) -> str | list[str]:
    """値を検証し、argv に埋め込む文字列表現 (array param は item リスト) を返す。"""
    if spec.type == "array":
        if not isinstance(value, list):
            raise ParamValidationError(
                f"param {spec.name!r}: expected array of strings, got {type(value).__name__}"
            )
        return [_validate_item(spec, item, i) for i, item in enumerate(value)]
    py_type = PY_TYPES[spec.type]
    if not isinstance(value, py_type) or (py_type is int and isinstance(value, bool)):
        raise ParamValidationError(
            f"param {spec.name!r}: expected {spec.type}, got {type(value).__name__}"
        )
    if spec.enum is not None and value not in spec.enum:
        raise ParamValidationError(
            f"param {spec.name!r}: value {value!r} is not in enum {spec.enum}"
        )
    if spec.pattern is not None and not re.fullmatch(spec.pattern, value):
        raise ParamValidationError(
            f"param {spec.name!r}: value {value!r} does not match pattern {spec.pattern!r}"
        )
    if spec.deny_pattern is not None and re.fullmatch(spec.deny_pattern, value):
        raise ParamValidationError(
            f"param {spec.name!r}: value {value!r} is denied by "
            f"deny_pattern {spec.deny_pattern!r}"
        )
    if spec.type == "boolean":
        rendered = "true" if value else "false"
    else:
        rendered = str(value)
    # 引数インジェクション対策: `-` 始まりの値はオプションとして解釈されうるので既定で拒否
    if rendered.startswith("-") and not spec.allow_dash_prefix:
        raise ParamValidationError(
            f"param {spec.name!r}: value starting with '-' is rejected "
            "(set allow_dash_prefix = true in config to allow)"
        )
    return rendered


def render_argv(tool: ToolSpec, arguments: dict[str, Any]) -> list[str]:
    """検証済みパラメータで argv テンプレートを埋め、実行可能な argv を返す。

    array param は placeholder 単独の要素を N 要素に展開する (0 要素も可)。
    """
    rendered_params: dict[str, str | list[str]] = {}
    for pname, spec in tool.params.items():
        if pname in arguments and arguments[pname] is not None:
            value = arguments[pname]
        elif spec.has_default:
            value = spec.default
        elif spec.required:
            raise ParamValidationError(f"param {pname!r} is required")
        else:
            continue  # 未参照の optional (ロード時に argv 参照は禁止済み)
        rendered_params[pname] = validate_param(spec, value)

    argv: list[str] = []
    for element in tool.argv:
        names = _placeholders(element)
        if not names:
            argv.append(element)
            continue
        values = {n: rendered_params[n] for n in names}
        list_names = [n for n, v in values.items() if isinstance(v, list)]
        if list_names:
            # ロード時に検証済みだが、ToolSpec 直組みに対する防御として再確認する
            name = list_names[0]
            if len(names) != 1 or element != "{" + name + "}":
                raise ParamValidationError(
                    f"param {name!r}: array value must fill an entire argv element"
                )
            argv.extend(values[name])
            continue
        argv.append(element.format(**values))
    return argv


# ---------------------------------------------------------------------------
# sync 実行
# ---------------------------------------------------------------------------

def _truncate(data: bytes, limit: int) -> str:
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
    name = time.strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:6]
    inv_dir = parent / f"{tool.name}-{name}"
    inv_dir.mkdir()
    (inv_dir / "stdout.log").write_bytes(stdout)
    (inv_dir / "stderr.log").write_bytes(stderr)
    (inv_dir / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False), encoding="utf-8",
    )
    return inv_dir


def _file_reply(data: bytes, inv_dir: Path, reason: str = "") -> str:
    """全量ファイルへの参照+抜粋だけを返す (呼び出し側 context の節約)。"""
    head = data[:FILE_EXCERPT_BYTES].decode("utf-8", errors="replace")
    tail = data[-FILE_EXCERPT_BYTES:].decode("utf-8", errors="replace")
    return (
        f"[cliwrap: output is {len(data)} bytes{reason}; full output saved to file]\n"
        f"file: {inv_dir / 'stdout.log'}\n"
        f"(stderr.log and meta.json with the executed argv are in the same directory)\n"
        f"Do not read it whole: use Read with offset/limit, or grep, to inspect parts.\n"
        f"--- head ({FILE_EXCERPT_BYTES} bytes) ---\n{head}\n"
        f"--- tail ({FILE_EXCERPT_BYTES} bytes) ---\n{tail}"
    )


def _stderr_tail(stderr: bytes) -> str:
    return stderr[-STDERR_TAIL_BYTES:].decode("utf-8", errors="replace")


def _exec_env(tool: ToolSpec) -> dict[str, str] | None:
    """config の env 強制を反映した実行環境を返す (強制なしなら None = 親環境継承)。

    継承環境の上にマージするので、同名の変数は config 側が常に勝つ。
    """
    if not tool.env:
        return None
    return {**os.environ, **tool.env}


def run_sync(
    tool: ToolSpec,
    argv: list[str],
    file_dir: Path | None = None,
    call_dir: Path | None = None,
) -> str:
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
            env=_exec_env(tool),
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


# ---------------------------------------------------------------------------
# job モード (長時間 CLI の非同期実行)
# ---------------------------------------------------------------------------

JOB_ID_RE = re.compile(r"^[0-9]{8}T[0-9]{6}-[0-9a-f]{6}$")


def default_cache_dir() -> Path:
    env = os.environ.get("CLI_MCP_CACHE_DIR")
    return Path(env) if env else Path.home() / ".cache" / "cli-mcp"


def _tail_file(path: Path, limit: int) -> str:
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
        self.jobs_dir = jobs_dir
        self._procs: dict[str, subprocess.Popen] = {}

    def _job_dir(self, job_id: str) -> Path:
        # job_id は client 入力なので、パストラバーサルを形式検証で遮断する
        if not JOB_ID_RE.match(job_id):
            raise ParamValidationError(f"invalid job_id: {job_id!r}")
        return self.jobs_dir / job_id

    def start(self, tool: ToolSpec, argv: list[str]) -> str:
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
                    env=_exec_env(tool),
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


# ---------------------------------------------------------------------------
# MCP サーバー組み立て
# ---------------------------------------------------------------------------

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


def build_server(spec: ServerSpec, cache_dir: Path | None = None):
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
        mcp.add_tool(fn, name=tool.name, description=description)
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
    mcp.add_tool(
        start_fn,
        name=f"{tool.name}_start",
        description=_tool_description(tool)
        + "\nStarts the command as a background job and returns a job_id immediately.",
    )

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

    mcp.add_tool(
        status_fn,
        name=f"{tool.name}_status",
        description=f"Check a background job started by {tool.name}_start: "
        "running/exited state plus stdout/stderr tail.",
    )
    mcp.add_tool(
        result_fn,
        name=f"{tool.name}_result",
        description=f"Fetch the output of a finished job started by {tool.name}_start "
        "(tail-limited). Returns a notice if the job is still running.",
    )
    mcp.add_tool(
        cancel_fn,
        name=f"{tool.name}_cancel",
        description=f"Cancel a running job started by {tool.name}_start "
        "(SIGTERM to the process group).",
    )


def _tool_description(tool: ToolSpec) -> str:
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


SUPPORTED_MODES = {"sync", "job"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, help="YAML config path (.yml/.yaml)")
    args = parser.parse_args(argv)
    try:
        spec = load_config(args.config)
    except (ConfigError, OSError, yaml.YAMLError) as exc:
        print(f"cliwrap: config error: {exc}", file=sys.stderr)
        return 1
    print(
        f"cliwrap: starting MCP server {spec.name!r} with tools: {sorted(spec.tools)}",
        file=sys.stderr,
    )
    server = build_server(spec)
    server.run()  # stdio transport (stdout はプロトコル専用)
    return 0


if __name__ == "__main__":
    sys.exit(main())
