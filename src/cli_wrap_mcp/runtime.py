"""sync / job 両モードが共有する実行部品: 実行環境の合成と実行 ID の採番。"""
from __future__ import annotations

import os
import re
import time
import uuid

from cli_wrap_mcp.spec import ToolSpec

# new_invocation_id() が返す書式の検証用。client 入力の job_id の形式検証にも使うため、
# 生成側を変えるときは必ずここも揃える (生成と検証を隣接させて 1 箇所で検査できる形に保つ)。
# アンカーは `^`/`$` ではなく `\A`/`\Z` を使う: `$` は末尾の改行の手前にもマッチするので、
# `$` だと "……-abcdef\n" が通り job dir 名に紛れ込む。`\Z` なら照合が fullmatch でも
# match でも search でも、この 1 行だけで形式を保証できる
INVOCATION_ID_RE = re.compile(r"\A[0-9]{8}T[0-9]{6}-[0-9a-f]{6}\Z")


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
