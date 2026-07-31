"""sync / job 両モードが共有する実行部品: 実行環境の合成と実行 ID の採番。"""
from __future__ import annotations

import os
import re
import time
import uuid

from cli_wrap_mcp.spec import ToolSpec

# new_invocation_id() が返す書式の検証用。client 入力の job_id の形式検証にも使うため、
# 生成側を変えるときは必ずここも揃える (生成と検証を隣接させて 1 箇所で検査できる形に保つ)。
# 照合は必ず fullmatch で行う: `$` は末尾の改行の手前にもマッチするため、`^...$` +
# match だと "……-abcdef\n" のような末尾改行付きの値を通してしまう
INVOCATION_ID_RE = re.compile(r"[0-9]{8}T[0-9]{6}-[0-9a-f]{6}")


def new_invocation_id() -> str:
    """1 実行分の証跡ディレクトリ名に使う ID (時刻 + 乱数) を採番する。"""
    return time.strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:6]


def exec_env(tool: ToolSpec) -> dict[str, str] | None:
    """config の env 強制を反映した実行環境を返す (強制なしなら None = 親環境継承)。

    継承環境の上にマージするので、同名の変数は config 側が常に勝つ。
    """
    if not tool.env:
        return None
    return {**os.environ, **tool.env}
