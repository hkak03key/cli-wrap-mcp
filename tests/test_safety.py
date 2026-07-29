"""パッケージ横断の安全不変条件 (src/cli_wrap_mcp/__init__.py 宣言) の機械検査。

実行: uv run pytest
"""
import unittest
from pathlib import Path



class SafetyInvariantsTest(unittest.TestCase):
    """パッケージ横断の安全不変条件 (src/cli_wrap_mcp/__init__.py 宣言) を AST 走査で検査する。"""

    SRC_DIR = Path(__file__).parent.parent / "src" / "cli_wrap_mcp"

    def _iter_calls(self):
        import ast

        for path in sorted(self.SRC_DIR.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    yield path.name, node

    def test_all_subprocess_calls_are_shell_false(self):
        import ast

        found = 0
        for fname, call in self._iter_calls():
            func = call.func
            if not (
                isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name)
                and func.value.id == "subprocess"
                and func.attr in ("run", "Popen")
            ):
                continue
            found += 1
            shell_kwargs = [k for k in call.keywords if k.arg == "shell"]
            self.assertTrue(
                shell_kwargs
                and all(
                    isinstance(k.value, ast.Constant) and k.value.value is False
                    for k in shell_kwargs
                ),
                f"{fname}: subprocess.{func.attr} は明示の shell=False が必須",
            )
        self.assertGreaterEqual(found, 2, "subprocess 実行箇所の検出漏れ (走査の壊れ)")

    def test_all_print_calls_go_to_stderr(self):
        import ast

        found = 0
        for fname, call in self._iter_calls():
            if not (isinstance(call.func, ast.Name) and call.func.id == "print"):
                continue
            found += 1
            file_kwargs = [k for k in call.keywords if k.arg == "file"]
            self.assertTrue(
                file_kwargs
                and all(
                    isinstance(k.value, ast.Attribute)
                    and k.value.attr == "stderr"
                    and isinstance(k.value.value, ast.Name)
                    and k.value.value.id == "sys"
                    for k in file_kwargs
                ),
                f"{fname}: print は file=sys.stderr 必須 (stdout は MCP プロトコル専用)",
            )
        self.assertGreaterEqual(found, 1, "print 呼び出しの検出漏れ (走査の壊れ)")


if __name__ == "__main__":
    unittest.main()
