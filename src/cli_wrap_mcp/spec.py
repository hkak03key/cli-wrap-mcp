"""エンジン全体で共有する語彙: 定数・例外・config のデータモデル。"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

DEFAULT_TIMEOUT_SEC = 60
DEFAULT_INLINE_MAX_OUTPUT_BYTES = 50_000
STDERR_TAIL_BYTES = 2_000
FILE_EXCERPT_BYTES = 1_000
# 出力の返し方:
# - inline: 応答にそのまま含める。上限 (inline_max_output_bytes) 超過時の挙動は
#           inline_on_large_output (truncate: 切り詰め / file: ファイルへ書き出し)
# - file:   成否やサイズに関係なく常にファイルへ全量書き出し (証跡用途)、
#           応答はパス + 抜粋 (抜粋の予算に収まるなら全量)
OUTPUT_MODES = {"inline", "file"}
INLINE_ON_LARGE_OUTPUT_MODES = {"truncate", "file"}
SUPPORTED_MODES = {"sync", "job"}

# job モード 1 tool が公開する MCP ツール名の suffix 一覧。
# config のロード時衝突検査と server の登録名生成の両方がこの一覧から導出する
# (片方だけ変えると検査が形骸化するため、正はここ 1 箇所に置く)
JOB_TOOL_SUFFIXES = ("start", "status", "result", "cancel")

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
    """ツールの 1 パラメータの定義 (型・検証規則・default)。"""

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
        """default が定義されているか (None は未定義扱い)。"""
        return self.default is not None


@dataclass
class ToolSpec:
    """MCP ツール 1 つの定義 (argv テンプレート・実行モード・出力の返し方)。"""

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
    """サーバー全体の定義 (名前と登録するツール群)。"""

    name: str
    description: str = ""
    tools: dict[str, ToolSpec] = field(default_factory=dict)
