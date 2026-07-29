"""テスト共通のヘルパ (YAML 文字列からの config ロードと最小 config)。"""
import tempfile
from pathlib import Path

from cli_wrap_mcp.config import load_config
from cli_wrap_mcp.spec import ServerSpec


def load_yaml(text: str) -> ServerSpec:
    with tempfile.NamedTemporaryFile("w", suffix=".yml", delete=False) as fp:
        fp.write(text)
        path = fp.name
    try:
        return load_config(path)
    finally:
        Path(path).unlink()


MINIMAL = """
server:
  name: test
tools:
  - name: echo
    description: echo a message
    argv: ["echo", "{msg}"]
    params:
      msg:
        type: string
        description: message
"""


# job モードの最小 config (config のロード検査と server の登録検査で共用)
JOB_YAML = (
    'server: {name: t}\n'
    'tools:\n'
    '  - name: task\n'
    '    mode: job\n'
    '    argv: ["sleep", "{sec}"]\n'
    '    params: {sec: {type: integer, default: 1}}\n'
)
