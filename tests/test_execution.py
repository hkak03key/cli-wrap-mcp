"""sync 実行と出力の返し方 (inline / truncate / file)・env 強制のテスト。

実行: uv run pytest
"""
import sys
import tempfile
import unittest
from pathlib import Path

from cli_wrap_mcp.execution import run_sync
from cli_wrap_mcp.jobs import JobManager
from cli_wrap_mcp.spec import FILE_EXCERPT_BYTES, ToolSpec


def _numbered_body(nbytes: int) -> bytes:
    """連番行を nbytes ちょうどに切り詰めた出力を組む。

    全行が相異なるので、抜粋の重複や取りこぼしを内容で判定できる。
    """
    body = "".join(f"{i:04d}\n" for i in range(nbytes // 5 + 1))
    return body.encode()[:nbytes]


class NumberedBodyTest(unittest.TestCase):
    def test_length_is_exact(self):
        # 抜粋の境界を突くテストの土台なので、要求バイト数ちょうどであること
        for nbytes in (0, 1, 4, 5, 2_000, 2_001, 50_005):
            self.assertEqual(nbytes, len(_numbered_body(nbytes)))


class RunSyncTest(unittest.TestCase):
    def tool(self, argv, **kwargs) -> ToolSpec:
        return ToolSpec(name="t", description="", argv=argv, **kwargs)

    def test_stdout_returned(self):
        tool = self.tool([sys.executable, "-c", "print('hello')"])
        self.assertEqual("hello\n", run_sync(tool, tool.argv))

    def test_output_truncated_with_note(self):
        tool = self.tool(
            [sys.executable, "-c", "print('x' * 1000)"], inline_max_output_bytes=100,
        )
        out = run_sync(tool, tool.argv)
        self.assertIn("output truncated at 100 bytes", out)
        self.assertLess(len(out), 300)

    def test_nonzero_exit_returns_error_with_stderr_tail(self):
        tool = self.tool(
            [sys.executable, "-c", "import sys; sys.stderr.write('boom'); sys.exit(3)"],
        )
        out = run_sync(tool, tool.argv)
        self.assertIn("exited with code 3", out)
        self.assertIn("boom", out)

    def test_timeout_returns_error(self):
        tool = self.tool(
            [sys.executable, "-c", "import time; time.sleep(5)"], timeout_sec=1,
        )
        out = run_sync(tool, tool.argv)
        self.assertIn("timed out after 1s", out)

    def test_missing_binary_returns_error(self):
        tool = self.tool(["/nonexistent/binary"])
        out = run_sync(tool, tool.argv)
        self.assertIn("failed to execute", out)

class FileOutputModeTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.file_dir = Path(self._tmp.name) / "outputs"

    def tearDown(self):
        self._tmp.cleanup()

    def tool(self, output_mode="file", inline_max_output_bytes=100, argv=None,
             inline_on_large_output="truncate") -> ToolSpec:
        return ToolSpec(
            name="t", description="",
            argv=argv or [sys.executable, "-c", "print('x' * 1000)"],
            inline_max_output_bytes=inline_max_output_bytes,
            output_mode=output_mode,
            inline_on_large_output=inline_on_large_output,
        )

    def test_file_mode_writes_file_holding_full_output(self):
        tool = self.tool()
        out = run_sync(tool, tool.argv, file_dir=self.file_dir)
        self.assertIn("full output saved to file", out)
        self.assertIn("1001 bytes", out)  # 総バイト数 (1000 + 改行)
        dirs = list(self.file_dir.iterdir())
        self.assertEqual(1, len(dirs))
        inv = dirs[0]
        self.assertTrue(inv.is_dir())
        self.assertTrue(inv.name.startswith("t-"))
        self.assertIn(str(inv / "stdout.log"), out)  # 絶対パスが返り値に含まれる
        self.assertEqual(b"x" * 1000 + b"\n", (inv / "stdout.log").read_bytes())  # 全量
        self.assertEqual(b"", (inv / "stderr.log").read_bytes())
        import json

        meta = json.loads((inv / "meta.json").read_text())
        self.assertEqual("t", meta["tool"])
        self.assertEqual(tool.argv, meta["argv"])
        self.assertEqual(0, meta["exit_code"])
        self.assertIn("started_at", meta)

    def body_tool(self, body: bytes, **kwargs) -> ToolSpec:
        """与えた bytes をそのまま stdout に流す tool (出力を 1 バイト単位で決められる)。"""
        path = Path(self._tmp.name) / f"body-{len(body)}"
        path.write_bytes(body)
        return self.tool(
            argv=[sys.executable, "-c",
                  "import sys; sys.stdout.buffer.write(open(sys.argv[1], 'rb').read())",
                  str(path)],
            **kwargs,
        )

    def excerpts(self, out: str, omitted: str) -> tuple[str, str]:
        """file reply から head / tail 抜粋の本文を取り出す (omitted は省略注記の本文)。"""
        head = out.split(f"--- head ({FILE_EXCERPT_BYTES} bytes) ---\n", 1)[1]
        head = head.split(f"\n--- {omitted} ---\n", 1)[0]
        tail = out.split(f"--- tail ({FILE_EXCERPT_BYTES} bytes) ---\n", 1)[1]
        return head, tail

    def test_file_mode_returns_body_once_within_excerpt_budget(self):
        # 抜粋予算 (head + tail) ちょうど: 枠に分けず本文を一度だけ返す
        body = _numbered_body(2 * FILE_EXCERPT_BYTES)
        tool = self.body_tool(body)
        out = run_sync(tool, tool.argv, file_dir=self.file_dir)
        self.assertIn(f"{2 * FILE_EXCERPT_BYTES} bytes", out)
        self.assertIn("full output saved to file", out)
        self.assertNotIn("--- head (", out)
        self.assertNotIn("--- tail (", out)
        self.assertTrue(out.endswith(body.decode()))
        self.assertEqual(1, out.count("0000\n"))  # 先頭行が二度現れない
        # 応答が全量を含むので「全部読むな」の助言は不要
        self.assertNotIn("Do not read it whole", out)

    def test_file_mode_excerpts_one_byte_past_the_budget(self):
        # 予算超過の最小ケース: 両端を抜粋し、省いた 1 バイトを注記する
        body = _numbered_body(2 * FILE_EXCERPT_BYTES + 1)
        tool = self.body_tool(body)
        omitted = f"bytes {FILE_EXCERPT_BYTES}-{FILE_EXCERPT_BYTES + 1} omitted"
        out = run_sync(tool, tool.argv, file_dir=self.file_dir)
        head, tail = self.excerpts(out, omitted)
        self.assertEqual(body[:FILE_EXCERPT_BYTES].decode(), head)
        self.assertEqual(body[-FILE_EXCERPT_BYTES:].decode(), tail)
        self.assertIn(f"--- {omitted} ---", out)
        self.assertIn("Do not read it whole", out)

    def test_file_mode_large_output_omits_the_middle(self):
        # 省略範囲は stdout.log 内の offset なので、そのまま Read の offset に使える
        body = _numbered_body(5_000)
        tool = self.body_tool(body)
        omitted = f"bytes {FILE_EXCERPT_BYTES}-{5_000 - FILE_EXCERPT_BYTES} omitted"
        out = run_sync(tool, tool.argv, file_dir=self.file_dir)
        head, tail = self.excerpts(out, omitted)
        self.assertEqual(body[:FILE_EXCERPT_BYTES].decode(), head)
        self.assertEqual(body[-FILE_EXCERPT_BYTES:].decode(), tail)
        self.assertIn(f"--- {omitted} ---", out)

    def test_file_mode_body_within_budget_keeps_multibyte_chars(self):
        # 予算内は分割しないので、抜粋境界に跨る文字が置換文字に化けることがない
        body = ("あ" * 400).encode()  # 1200 bytes
        tool = self.body_tool(body)
        out = run_sync(tool, tool.argv, file_dir=self.file_dir)
        self.assertEqual(400, out.count("あ"))
        self.assertNotIn("�", out)

    def test_inline_on_large_output_file_returns_body_once(self):
        # 上限超過でファイル化した応答も、全量が予算に収まるなら本文を一度だけ載せる
        # (載る量が inline_max_output_bytes を超えうる点の裁定は issue #17)
        body = _numbered_body(500)
        tool = self.body_tool(
            body, output_mode="inline", inline_on_large_output="file",
            inline_max_output_bytes=100,
        )
        out = run_sync(tool, tool.argv, file_dir=self.file_dir)
        self.assertIn("(> 100)", out)
        self.assertEqual(1, out.count("0000\n"))
        self.assertTrue(out.endswith(body.decode()))

    def test_file_mode_writes_even_small_output(self):
        # file mode は証跡目的なので、上限以下でも常にファイル化する
        tool = self.tool(inline_max_output_bytes=5000)
        out = run_sync(tool, tool.argv, file_dir=self.file_dir)
        self.assertIn("full output saved to file", out)
        self.assertEqual(1, len(list(self.file_dir.iterdir())))

    def test_file_mode_writes_on_failure_too(self):
        tool = self.tool(
            argv=[sys.executable, "-c",
                  "import sys; print('partial'); sys.stderr.write('boom'); sys.exit(3)"],
        )
        out = run_sync(tool, tool.argv, file_dir=self.file_dir)
        self.assertIn("exited with code 3", out)
        self.assertIn("output saved to:", out)
        self.assertIn("boom", out)
        dirs = list(self.file_dir.iterdir())
        self.assertEqual(1, len(dirs))  # 失敗した実行も証跡が残る
        self.assertEqual(b"partial\n", (dirs[0] / "stdout.log").read_bytes())
        self.assertEqual(b"boom", (dirs[0] / "stderr.log").read_bytes())
        import json

        self.assertEqual(3, json.loads((dirs[0] / "meta.json").read_text())["exit_code"])

    def test_file_mode_timeout_saves_partial_output(self):
        tool = ToolSpec(
            name="t", description="", output_mode="file", timeout_sec=1,
            argv=[sys.executable, "-u", "-c",
                  "import time; print('before-sleep'); time.sleep(5)"],
        )
        out = run_sync(tool, tool.argv, file_dir=self.file_dir)
        self.assertIn("timed out after 1s", out)
        self.assertIn("partial output saved to:", out)
        dirs = list(self.file_dir.iterdir())
        self.assertEqual(1, len(dirs))
        self.assertEqual(b"before-sleep\n", (dirs[0] / "stdout.log").read_bytes())
        import json

        meta = json.loads((dirs[0] / "meta.json").read_text())
        self.assertIsNone(meta["exit_code"])
        self.assertTrue(meta["timed_out"])

    def test_inline_truncate_never_writes_file(self):
        tool = self.tool(output_mode="inline")
        out = run_sync(tool, tool.argv, file_dir=self.file_dir)
        self.assertIn("output truncated at 100 bytes", out)
        self.assertFalse(self.file_dir.exists())

    def test_inline_on_large_output_file_writes_only_on_overflow(self):
        tool = self.tool(output_mode="inline", inline_on_large_output="file")
        out = run_sync(tool, tool.argv, file_dir=self.file_dir)
        self.assertIn("full output saved to file", out)  # 超過 → ファイル化 (旧 spill)
        self.assertEqual(1, len(list(self.file_dir.iterdir())))

    def test_inline_on_large_output_file_stays_inline_under_limit(self):
        tool = self.tool(
            output_mode="inline", inline_on_large_output="file",
            inline_max_output_bytes=5000,
        )
        out = run_sync(tool, tool.argv, file_dir=self.file_dir)
        self.assertEqual("x" * 1000 + "\n", out)
        self.assertFalse(self.file_dir.exists())

    def test_file_mode_without_dir_falls_back_to_truncate(self):
        tool = self.tool()
        out = run_sync(tool, tool.argv, file_dir=None)
        self.assertIn("output truncated at 100 bytes", out)

class EnvExecTest(unittest.TestCase):
    PRINT_VAR = "import os; print(os.environ.get('CLIWRAP_TEST_VAR', '(unset)'))"

    def tool(self, env) -> ToolSpec:
        return ToolSpec(
            name="t", description="", argv=[sys.executable, "-c", self.PRINT_VAR], env=env,
        )

    def test_run_sync_forces_env_var(self):
        tool = self.tool({"CLIWRAP_TEST_VAR": "forced"})
        self.assertEqual("forced\n", run_sync(tool, tool.argv))

    def test_forced_env_overrides_inherited(self):
        import os

        os.environ["CLIWRAP_TEST_VAR"] = "parent"
        self.addCleanup(os.environ.pop, "CLIWRAP_TEST_VAR", None)
        tool = self.tool({"CLIWRAP_TEST_VAR": "forced"})
        self.assertEqual("forced\n", run_sync(tool, tool.argv))

    def test_parent_env_still_inherited_alongside_forced(self):
        import os

        os.environ["CLIWRAP_OTHER_VAR"] = "inherited"
        self.addCleanup(os.environ.pop, "CLIWRAP_OTHER_VAR", None)
        tool = ToolSpec(
            name="t", description="",
            argv=[sys.executable, "-c", "import os; print(os.environ['CLIWRAP_OTHER_VAR'])"],
            env={"CLIWRAP_TEST_VAR": "forced"},
        )
        self.assertEqual("inherited\n", run_sync(tool, tool.argv))

    def test_no_env_config_inherits_parent(self):
        import os

        os.environ["CLIWRAP_TEST_VAR"] = "parent"
        self.addCleanup(os.environ.pop, "CLIWRAP_TEST_VAR", None)
        tool = self.tool({})
        self.assertEqual("parent\n", run_sync(tool, tool.argv))

    def test_job_start_forces_env_var(self):
        with tempfile.TemporaryDirectory() as tmp:
            jobs = JobManager(Path(tmp) / "jobs")
            tool = ToolSpec(
                name="j", description="", argv=[], mode="job",
                env={"CLIWRAP_TEST_VAR": "forced"},
            )
            msg = jobs.start(tool, [sys.executable, "-c", self.PRINT_VAR])
            job_id = msg.splitlines()[0].removeprefix("job started: ")
            import time

            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                state, _rc = jobs._poll(job_id)
                if state != "running":
                    break
                time.sleep(0.05)
            self.assertEqual(
                "forced\n", (jobs.jobs_dir / job_id / "stdout.log").read_text(),
            )


if __name__ == "__main__":
    unittest.main()
