"""console script の配線 (pyproject の参照先と main の入出力) のテスト。

実行: uv run pytest
"""
import tempfile
import unittest
from pathlib import Path

from _helpers import MINIMAL


class CliEntryPointTest(unittest.TestCase):
    """console script の配線 (pyproject の参照先と main の入出力) を固定する。"""

    def test_pyproject_script_target_resolves(self):
        import importlib
        import tomllib

        pyproject = Path(__file__).parent.parent / "pyproject.toml"
        with open(pyproject, "rb") as fp:
            scripts = tomllib.load(fp)["project"]["scripts"]
        module_name, func_name = scripts["cli-wrap-mcp"].split(":")
        module = importlib.import_module(module_name)
        self.assertTrue(callable(getattr(module, func_name)))

    def test_main_returns_1_on_config_error(self):
        from cli_wrap_mcp import cli

        self.assertEqual(1, cli.main(["--config", "/nonexistent/config.yml"]))

    def test_main_builds_and_runs_server_on_valid_config(self):
        from cli_wrap_mcp import cli

        with tempfile.NamedTemporaryFile("w", suffix=".yml", delete=False) as fp:
            fp.write(MINIMAL)
            path = fp.name
        ran = []

        class FakeServer:
            def run(self):
                ran.append(True)

        original = cli.build_server
        cli.build_server = lambda spec: FakeServer()
        try:
            rc = cli.main(["--config", path])
        finally:
            cli.build_server = original
            Path(path).unlink()
        self.assertEqual(0, rc)
        self.assertEqual([True], ran)


if __name__ == "__main__":
    unittest.main()
