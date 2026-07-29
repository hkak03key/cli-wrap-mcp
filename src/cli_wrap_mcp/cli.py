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
import sys

import yaml

from cli_wrap_mcp.config import load_config
from cli_wrap_mcp.server import build_server
from cli_wrap_mcp.spec import ConfigError


def main(argv: list[str] | None = None) -> int:
    """config をロードして MCP サーバーを起動する (console script のエントリポイント)。"""
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
