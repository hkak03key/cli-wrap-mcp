"""cli-wrap-mcp: turn any CLI into an MCP server with a declarative YAML config.

パッケージ横断の安全不変条件 (全モジュールを拘束する。tests の
SafetyInvariantsTest が AST 走査で機械検査する):
- 実行は常に shell=False の argv 配列 (execution.run_sync / jobs.JobManager.start)。
  シェル文字列連結の経路は作らない
- stdout は MCP プロトコル専用。ログ・デバッグ出力は必ず stderr へ (全モジュールの print)
"""
