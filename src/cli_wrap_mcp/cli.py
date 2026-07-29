"""console script エントリポイント。

使い方とエンジン全体の設計原則の正はパッケージ docstring (cli_wrap_mcp.__doc__) にあり、
--help の説明文にもそれを表示する。
"""
from __future__ import annotations

import argparse
import sys

import yaml

import cli_wrap_mcp
from cli_wrap_mcp.config import load_config
from cli_wrap_mcp.server import build_server
from cli_wrap_mcp.spec import ConfigError


def main(argv: list[str] | None = None) -> int:
    """config をロードして MCP サーバーを起動する (console script のエントリポイント)。"""
    parser = argparse.ArgumentParser(description=cli_wrap_mcp.__doc__)
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
