"""sync 実行と出力の返し方 (inline / truncate / file)・env 強制のテスト。

実行: uv run pytest
"""
import sys
import tempfile
import unittest
from pathlib import Path

from cli_wrap_mcp.execution import run_sync
from cli_wrap_mcp.jobs import JobManager
from cli_wrap_mcp.spec import ToolSpec


class RunSyncTest(unittest.TestCase):
    def tool(self, argv, **kwargs) -> ToolSpec:
        return ToolSpec(name="t", description="", argv=argv, **kwargs)

    def test_stdout_returned(self):
        tool = self.tool([sys.executable, "-c", "print('hello')"])
        self.assertEqual("hello\n", run_sync(tool, tool.argv).text)

    def test_output_truncated_with_note(self):
        tool = self.tool(
            [sys.executable, "-c", "print('x' * 1000)"], inline_max_output_bytes=100,
        )
        out = run_sync(tool, tool.argv).text
        self.assertIn("output truncated at 100 bytes", out)
        self.assertLess(len(out), 300)

    def test_nonzero_exit_returns_error_with_stderr_tail(self):
        tool = self.tool(
            [sys.executable, "-c", "import sys; sys.stderr.write('boom'); sys.exit(3)"],
        )
        out = run_sync(tool, tool.argv).text
        self.assertIn("exited with code 3", out)
        self.assertIn("boom", out)

    def test_timeout_returns_error(self):
        tool = self.tool(
            [sys.executable, "-c", "import time; time.sleep(5)"], timeout_sec=1,
        )
        out = run_sync(tool, tool.argv).text
        self.assertIn("timed out after 1s", out)

    def test_missing_binary_returns_error(self):
        tool = self.tool(["/nonexistent/binary"])
        out = run_sync(tool, tool.argv).text
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
        out = run_sync(tool, tool.argv, file_dir=self.file_dir).text
        self.assertIn("full output saved to file", out)
        self.assertIn("1001 bytes", out)  # 総バイト数 (1000 + 改行)
        self.assertIn("offset/limit", out)
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

    def test_file_mode_writes_even_small_output(self):
        # file mode は証跡目的なので、上限以下でも常にファイル化する
        tool = self.tool(inline_max_output_bytes=5000)
        out = run_sync(tool, tool.argv, file_dir=self.file_dir).text
        self.assertIn("full output saved to file", out)
        self.assertEqual(1, len(list(self.file_dir.iterdir())))

    def test_file_mode_writes_on_failure_too(self):
        tool = self.tool(
            argv=[sys.executable, "-c",
                  "import sys; print('partial'); sys.stderr.write('boom'); sys.exit(3)"],
        )
        out = run_sync(tool, tool.argv, file_dir=self.file_dir).text
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
        out = run_sync(tool, tool.argv, file_dir=self.file_dir).text
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
        out = run_sync(tool, tool.argv, file_dir=self.file_dir).text
        self.assertIn("output truncated at 100 bytes", out)
        self.assertFalse(self.file_dir.exists())

    def test_inline_on_large_output_file_writes_only_on_overflow(self):
        tool = self.tool(output_mode="inline", inline_on_large_output="file")
        out = run_sync(tool, tool.argv, file_dir=self.file_dir).text
        self.assertIn("full output saved to file", out)  # 超過 → ファイル化 (旧 spill)
        self.assertEqual(1, len(list(self.file_dir.iterdir())))

    def test_inline_on_large_output_file_stays_inline_under_limit(self):
        tool = self.tool(
            output_mode="inline", inline_on_large_output="file",
            inline_max_output_bytes=5000,
        )
        out = run_sync(tool, tool.argv, file_dir=self.file_dir).text
        self.assertEqual("x" * 1000 + "\n", out)
        self.assertFalse(self.file_dir.exists())

    def test_file_mode_without_dir_falls_back_to_truncate(self):
        tool = self.tool()
        out = run_sync(tool, tool.argv, file_dir=None).text
        self.assertIn("output truncated at 100 bytes", out)

class ErrorFlagTest(unittest.TestCase):
    """ラップ先の失敗が is_error に出ること (本文の文字列照合に頼らせない)。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.file_dir = Path(self._tmp.name) / "outputs"

    def tearDown(self):
        self._tmp.cleanup()

    def tool(self, argv, **kwargs) -> ToolSpec:
        return ToolSpec(name="t", description="", argv=argv, **kwargs)

    def test_success_is_not_error(self):
        tool = self.tool([sys.executable, "-c", "print('ok')"])
        self.assertFalse(run_sync(tool, tool.argv).is_error)

    def test_exit_zero_printing_error_prefix_is_not_error(self):
        # 本文が "error:" で始まっても exit 0 なら成功 (issue #5 の誤検出シナリオ)
        tool = self.tool([sys.executable, "-c", "print('error: looks scary')"])
        reply = run_sync(tool, tool.argv)
        self.assertTrue(reply.text.startswith("error:"))
        self.assertFalse(reply.is_error)

    def test_nonzero_exit_is_error(self):
        tool = self.tool([sys.executable, "-c", "import sys; sys.exit(3)"])
        self.assertTrue(run_sync(tool, tool.argv).is_error)

    def test_exit_one_is_error(self):
        # 境界: 非ゼロの最小値でも立つ (ガードレールの exit 1 が主な用途)
        tool = self.tool([sys.executable, "-c", "import sys; sys.exit(1)"])
        self.assertTrue(run_sync(tool, tool.argv).is_error)

    def test_timeout_is_error(self):
        tool = self.tool([sys.executable, "-c", "import time; time.sleep(5)"], timeout_sec=1)
        self.assertTrue(run_sync(tool, tool.argv).is_error)

    def test_missing_binary_is_error(self):
        self.assertTrue(run_sync(self.tool(["/nonexistent/binary"]), ["/nonexistent/binary"]).is_error)

    def test_file_mode_nonzero_exit_is_error(self):
        tool = self.tool(
            [sys.executable, "-c", "import sys; sys.exit(3)"], output_mode="file",
        )
        self.assertTrue(run_sync(tool, tool.argv, file_dir=self.file_dir).is_error)

    def test_file_mode_success_is_not_error(self):
        tool = self.tool([sys.executable, "-c", "print('ok')"], output_mode="file")
        self.assertFalse(run_sync(tool, tool.argv, file_dir=self.file_dir).is_error)

    def test_write_failure_is_error(self):
        blocker = Path(self._tmp.name) / "blocker"
        blocker.write_text("file")
        tool = self.tool([sys.executable, "-c", "print('ok')"], output_mode="file")
        reply = run_sync(tool, tool.argv, file_dir=blocker / "sub")
        self.assertIn("failed to write output", reply.text)
        self.assertTrue(reply.is_error)


class EnvExecTest(unittest.TestCase):
    PRINT_VAR = "import os; print(os.environ.get('CLIWRAP_TEST_VAR', '(unset)'))"

    def tool(self, env) -> ToolSpec:
        return ToolSpec(
            name="t", description="", argv=[sys.executable, "-c", self.PRINT_VAR], env=env,
        )

    def test_run_sync_forces_env_var(self):
        tool = self.tool({"CLIWRAP_TEST_VAR": "forced"})
        self.assertEqual("forced\n", run_sync(tool, tool.argv).text)

    def test_forced_env_overrides_inherited(self):
        import os

        os.environ["CLIWRAP_TEST_VAR"] = "parent"
        self.addCleanup(os.environ.pop, "CLIWRAP_TEST_VAR", None)
        tool = self.tool({"CLIWRAP_TEST_VAR": "forced"})
        self.assertEqual("forced\n", run_sync(tool, tool.argv).text)

    def test_parent_env_still_inherited_alongside_forced(self):
        import os

        os.environ["CLIWRAP_OTHER_VAR"] = "inherited"
        self.addCleanup(os.environ.pop, "CLIWRAP_OTHER_VAR", None)
        tool = ToolSpec(
            name="t", description="",
            argv=[sys.executable, "-c", "import os; print(os.environ['CLIWRAP_OTHER_VAR'])"],
            env={"CLIWRAP_TEST_VAR": "forced"},
        )
        self.assertEqual("inherited\n", run_sync(tool, tool.argv).text)

    def test_no_env_config_inherits_parent(self):
        import os

        os.environ["CLIWRAP_TEST_VAR"] = "parent"
        self.addCleanup(os.environ.pop, "CLIWRAP_TEST_VAR", None)
        tool = self.tool({})
        self.assertEqual("parent\n", run_sync(tool, tool.argv).text)

    def test_job_start_forces_env_var(self):
        with tempfile.TemporaryDirectory() as tmp:
            jobs = JobManager(Path(tmp) / "jobs")
            tool = ToolSpec(
                name="j", description="", argv=[], mode="job",
                env={"CLIWRAP_TEST_VAR": "forced"},
            )
            msg = jobs.start(tool, [sys.executable, "-c", self.PRINT_VAR]).text
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
