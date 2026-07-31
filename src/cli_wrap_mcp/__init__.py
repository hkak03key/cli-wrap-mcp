"""cli-wrap-mcp: 宣言的 YAML config から CLI ラップ MCP サーバーを動的生成するエンジン。

使い方:
    cli-wrap-mcp --config <path.yml>

設計原則 (安全性がこの仕組みの核。この docstring が正で、CLI の --help にも表示される):
- 実行は常に shell=False の argv 配列。シェル文字列連結の経路は存在しない
  (execution.run_sync / jobs.JobManager.start。SafetyInvariantsTest が機械検査)
- パラメータ値は検証 (type / pattern fullmatch / deny_pattern / enum) を通過してから
  argv 要素に埋め込む。array param は要素全体の placeholder のみに展開を許し、
  各 item に同じ検証を適用する (rendering)
- 引数インジェクション対策: `-` で始まる値は既定で拒否 (per-param の
  allow_dash_prefix = true で明示的に許可可能)
- argv の置換は単一パス。`{` に隣接しない `{param 名}` だけが placeholder で、
  それ以外の波括弧はリテラル。format spec・attribute access・conversion は
  「禁止」ではなく解釈自体が存在しない (rendering)
- config ロード時に argv 内の未定義プレースホルダはエラー (config)
- stdout は MCP プロトコル専用。ログ・デバッグ出力は必ず stderr へ
  (全モジュールの print。SafetyInvariantsTest が機械検査)
"""
