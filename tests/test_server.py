"""MCP サーバー組み立てとツール登録 (自動注入 param を含む) のテスト。

実行: uv run pytest
"""
import sys
import tempfile
import unittest
from pathlib import Path

from _helpers import JOB_YAML, MINIMAL, call_tool, load_yaml
from cli_wrap_mcp.server import SCALAR_RESULT_KEY, build_server
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
        content = call_tool(server, "pyprint", {"msg": "ping"}).content
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
        content = call_tool(server, "pyargs", {"args": ["a", "b c"]}).content
        self.assertIn("['a', 'b c']", content[0].text)

    def test_call_tool_with_array_omitted_uses_empty_default(self):
        server = build_server(load_yaml(self.ARRAY_YAML))
        content = call_tool(server, "pyargs", {}).content
        self.assertIn("[]", content[0].text)

class IsErrorTest(unittest.TestCase):
    """client が受け取る CallToolResult.isError が失敗を伝えること (issue #5)。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def server(self, yaml_text):
        return build_server(load_yaml(yaml_text), cache_dir=Path(self._tmp.name))

    def sync_server(self, body: str, **tool_keys):
        keys = "".join(f"    {k}: {v}\n" for k, v in tool_keys.items())
        return self.server(
            'server: {name: t}\n'
            'tools:\n'
            '  - name: run\n'
            '    description: run\n'
            + keys
            + f'    argv: ["{sys.executable}", "-c", "{body}"]\n'
        )

    FAIL_BODY = (
        "import sys; print('stdout line'); "
        "print('stderr line', file=sys.stderr); sys.exit(3)"
    )

    def test_nonzero_exit_sets_is_error_without_changing_text(self):
        # issue #5 の再現ケース: isError が立ち、本文は従来どおりのまま
        result = call_tool(self.sync_server(self.FAIL_BODY), "run")
        self.assertTrue(result.isError)
        self.assertEqual(
            "error: command exited with code 3\nstderr (tail):\nstderr line\n",
            result.content[0].text,
        )

    def test_success_is_not_error(self):
        result = call_tool(self.sync_server("print('ok')"), "run")
        self.assertFalse(result.isError)
        self.assertEqual("ok\n", result.content[0].text)

    def test_success_printing_error_prefix_is_not_error(self):
        # 成功時に "error:" を出す CLI を失敗と取り違えない (文字列照合との差)
        result = call_tool(self.sync_server("print('error: not really')"), "run")
        self.assertFalse(result.isError)

    def test_timeout_sets_is_error(self):
        server = self.sync_server("import time; time.sleep(5)", timeout_sec=1)
        self.assertTrue(call_tool(server, "run").isError)

    def test_file_mode_nonzero_exit_sets_is_error(self):
        server = self.sync_server(self.FAIL_BODY, output_mode="file")
        self.assertTrue(call_tool(server, "run").isError)

    def test_param_validation_error_sets_is_error(self):
        server = self.server(
            'server: {name: t}\n'
            'tools:\n'
            '  - name: run\n'
            '    argv: ["echo", "{msg}"]\n'
            '    params: {msg: {type: string, pattern: "[a-z]+"}}\n'
        )
        result = call_tool(server, "run", {"msg": "NOPE!"})
        self.assertTrue(result.isError)
        self.assertTrue(result.content[0].text.startswith("error: "))

    def test_relative_file_output_dir_sets_is_error(self):
        server = self.sync_server("print('ok')")
        self.assertTrue(call_tool(server, "run", {"file_output_dir": "rel/dir"}).isError)

    # --- job モード ---------------------------------------------------------

    JOB_FAIL = (
        'server: {name: t}\n'
        'tools:\n'
        '  - name: task\n'
        '    mode: job\n'
        f'    argv: ["{sys.executable}", "-c", "import sys; sys.exit(3)"]\n'
    )

    def wait_exited(self, server, job_id: str) -> None:
        import time

        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if "exited" in call_tool(server, "task_status", {"job_id": job_id}).content[0].text:
                return
            time.sleep(0.05)
        raise AssertionError("job did not exit in time")

    def start_job(self, server) -> str:
        result = call_tool(server, "task_start")
        self.assertFalse(result.isError)
        return result.content[0].text.splitlines()[0].removeprefix("job started: ")

    def test_job_result_of_failed_command_sets_is_error(self):
        server = self.server(self.JOB_FAIL)
        job_id = self.start_job(server)
        self.wait_exited(server, job_id)
        result = call_tool(server, "task_result", {"job_id": job_id})
        self.assertIn("exit code 3", result.content[0].text)
        self.assertTrue(result.isError)

    def test_job_status_of_failed_command_is_not_error(self):
        # 状態問い合わせ自体は成立している (ラップ先の成否を受け取る地点は result)
        server = self.server(self.JOB_FAIL)
        job_id = self.start_job(server)
        self.wait_exited(server, job_id)
        self.assertFalse(call_tool(server, "task_status", {"job_id": job_id}).isError)

    def test_job_result_of_successful_command_is_not_error(self):
        server = self.server(
            'server: {name: t}\n'
            'tools:\n'
            '  - name: task\n'
            '    mode: job\n'
            f'    argv: ["{sys.executable}", "-c", "print(\'done\')"]\n'
        )
        job_id = self.start_job(server)
        self.wait_exited(server, job_id)
        self.assertFalse(call_tool(server, "task_result", {"job_id": job_id}).isError)

    def test_invalid_job_id_sets_is_error(self):
        server = self.server(self.JOB_FAIL)
        for suffix in ("status", "result", "cancel"):
            result = call_tool(server, f"task_{suffix}", {"job_id": "../../etc/passwd"})
            self.assertTrue(result.isError, suffix)
            self.assertIn("invalid job_id", result.content[0].text, suffix)


class StructuredContentShapeTest(unittest.TestCase):
    """server.SCALAR_RESULT_KEY が SDK の包み方と一致していることの機械検査。

    CallToolResult を自前で組む以上この形も自前で再現しており、ズレると
    outputSchema 検証に落ちる (server._call_result の docstring 参照)。
    """

    def test_scalar_result_key_matches_sdk_wrapping(self):
        from mcp.server.fastmcp import FastMCP

        def plain(msg: str) -> str:
            return msg

        server = FastMCP("shape")
        server.add_tool(plain, name="plain")
        result = call_tool(server, "plain", {"msg": "hi"})
        self.assertEqual({SCALAR_RESULT_KEY: "hi"}, result.structuredContent)

    def test_engine_tools_keep_structured_content_and_schema(self):
        server = build_server(load_yaml(MINIMAL))
        import anyio

        schema = anyio.run(server.list_tools)[0].outputSchema
        self.assertEqual([SCALAR_RESULT_KEY], list(schema["properties"]))
        result = call_tool(server, "echo", {"msg": "hi"})
        self.assertEqual({SCALAR_RESULT_KEY: "hi\n"}, result.structuredContent)


class OutputDirTest(unittest.TestCase):
    """全 sync ツールに自動注入される file_output_dir param の挙動。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dest = str(Path(self._tmp.name) / "dest")

    def tearDown(self):
        self._tmp.cleanup()

    def call(self, server, name, args) -> str:
        return call_tool(server, name, args).content[0].text

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
