"""MCP サーバー組み立てとツール登録 (自動注入 param を含む) のテスト。

実行: uv run pytest
"""
import sys
import tempfile
import unittest
from pathlib import Path

from _helpers import JOB_YAML, MINIMAL, load_yaml
from cli_wrap_mcp.server import build_server
from cli_wrap_mcp.spec import ConfigError


class BuildServerTest(unittest.TestCase):
    def test_tools_registered_on_fastmcp(self):
        spec = load_yaml(MINIMAL)
        server = build_server(spec)
        import anyio

        tools = anyio.run(server.list_tools)
        self.assertEqual(["echo"], [t.name for t in tools])
        schema = tools[0].inputSchema
        self.assertEqual(["msg", "file_output_dir"], list(schema["properties"]))
        self.assertEqual(["msg"], schema.get("required", []))
        self.assertEqual("string", schema["properties"]["msg"]["type"])

    def test_call_tool_executes_argv(self):
        spec = load_yaml(
            'server: {name: t}\n'
            'tools:\n'
            '  - name: pyprint\n'
            '    description: print\n'
            f'    argv: ["{sys.executable}", "-c", "print(\'ok:\' + \'{{msg}}\')"]\n'
            '    params: {msg: {type: string}}\n'
        )
        server = build_server(spec)
        import anyio

        result = anyio.run(lambda: server.call_tool("pyprint", {"msg": "ping"}))
        content = result[0] if isinstance(result, tuple) else result
        self.assertIn("ok:ping", content[0].text)

    ARRAY_YAML = (
        'server: {name: t}\n'
        'tools:\n'
        '  - name: pyargs\n'
        '    description: print argv\n'
        f'    argv: ["{sys.executable}", "-c", "import sys; print(sys.argv[1:])", "{{args}}"]\n'
        '    params:\n'
        '      args: {type: array, required: false}\n'
    )

    def test_array_param_in_input_schema(self):
        server = build_server(load_yaml(self.ARRAY_YAML))
        import anyio

        schema = anyio.run(server.list_tools)[0].inputSchema
        self.assertNotIn("args", schema.get("required", []))
        prop = schema["properties"]["args"]
        # optional array は list[str] | None なので anyOf 形になる
        variants = prop.get("anyOf", [prop])
        array_variants = [v for v in variants if v.get("type") == "array"]
        self.assertEqual(1, len(array_variants))
        self.assertEqual({"type": "string"}, array_variants[0]["items"])

    def test_call_tool_with_array_argument(self):
        server = build_server(load_yaml(self.ARRAY_YAML))
        import anyio

        result = anyio.run(lambda: server.call_tool("pyargs", {"args": ["a", "b c"]}))
        content = result[0] if isinstance(result, tuple) else result
        self.assertIn("['a', 'b c']", content[0].text)

    def test_call_tool_with_array_omitted_uses_empty_default(self):
        server = build_server(load_yaml(self.ARRAY_YAML))
        import anyio

        result = anyio.run(lambda: server.call_tool("pyargs", {}))
        content = result[0] if isinstance(result, tuple) else result
        self.assertIn("[]", content[0].text)

class OutputDirTest(unittest.TestCase):
    """全 sync ツールに自動注入される file_output_dir param の挙動。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dest = str(Path(self._tmp.name) / "dest")

    def tearDown(self):
        self._tmp.cleanup()

    def call(self, server, name, args) -> str:
        import anyio

        result = anyio.run(lambda: server.call_tool(name, args))
        content = result[0] if isinstance(result, tuple) else result
        return content[0].text

    def server(self):
        return build_server(load_yaml(MINIMAL), cache_dir=Path(self._tmp.name))

    def test_call_file_output_dir_forces_file_even_for_small_output(self):
        out = self.call(self.server(), "echo", {"msg": "hi", "file_output_dir": self.dest})
        self.assertIn("full output saved to file", out)
        dirs = list(Path(self.dest).iterdir())  # dir は mkdir -p 相当で自動作成
        self.assertEqual(1, len(dirs))
        self.assertTrue(dirs[0].name.startswith("echo-"))
        self.assertIn(str(dirs[0] / "stdout.log"), out)
        self.assertEqual(b"hi\n", (dirs[0] / "stdout.log").read_bytes())  # 小出力でも全量

    def test_without_call_file_output_dir_behaves_as_before(self):
        out = self.call(self.server(), "echo", {"msg": "hi"})
        self.assertEqual("hi\n", out)
        self.assertFalse(Path(self.dest).exists())

    def test_relative_call_file_output_dir_rejected(self):
        out = self.call(self.server(), "echo", {"msg": "hi", "file_output_dir": "rel/dir"})
        self.assertIn("error: file_output_dir must be an absolute path", out)

    def test_write_failure_returns_error_string(self):
        # 既存ファイルの下に dir は作れない → OSError → エラー文字列
        blocker = Path(self._tmp.name) / "blocker"
        blocker.write_text("file")
        out = self.call(
            self.server(), "echo", {"msg": "hi", "file_output_dir": str(blocker / "sub")},
        )
        self.assertIn("error: failed to write output", out)

    def test_file_output_dir_in_schema_as_optional(self):
        import anyio

        tools = anyio.run(self.server().list_tools)
        schema = tools[0].inputSchema
        self.assertIn("file_output_dir", schema["properties"])
        self.assertNotIn("file_output_dir", schema.get("required", []))

    def test_job_tools_not_injected(self):
        import anyio

        spec = load_yaml(JOB_YAML)
        server = build_server(spec, cache_dir=Path(self._tmp.name))
        for tool in anyio.run(server.list_tools):
            self.assertNotIn("file_output_dir", tool.inputSchema["properties"], tool.name)

    def test_call_file_output_dir_wins_over_truncate_for_large_output(self):
        spec = load_yaml(
            'server: {name: t}\n'
            'tools:\n'
            '  - name: big\n'
            '    description: big\n'
            '    inline_max_output_bytes: 100\n'
            f'    argv: ["{sys.executable}", "-c", "print(\'x\' * 1000)"]\n'
        )
        server = build_server(spec, cache_dir=Path(self._tmp.name))
        out = self.call(server, "big", {"file_output_dir": self.dest})
        self.assertIn("1001 bytes", out)
        dirs = list(Path(self.dest).iterdir())
        self.assertEqual(b"x" * 1000 + b"\n", (dirs[0] / "stdout.log").read_bytes())  # 全量

    def test_reserved_param_name_is_config_error(self):
        with self.assertRaisesRegex(ConfigError, "reserved"):
            load_yaml(
                'server: {name: t}\n'
                'tools:\n'
                '  - name: x\n'
                '    argv: ["echo", "{file_output_dir}"]\n'
                '    params: {file_output_dir: {type: string}}\n'
            )


if __name__ == "__main__":
    unittest.main()
