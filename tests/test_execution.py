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

    def numbered_output_tool(self, lines: int) -> ToolSpec:
        """1 行 5 バイト (連番 + 改行) の出力を lines 行だけ出す file mode tool。

        全行が相異なるので、抜粋が重複しているかを内容で判定できる。
        """
        return self.tool(
            argv=[sys.executable, "-c",
                  f'import sys; sys.stdout.write("".join(f"{{i:04d}}\\n" '
                  f"for i in range({lines})))"],
        )

    def excerpts(self, out: str) -> tuple[str, str]:
        """file reply から head / tail 抜粋の本文を取り出す。"""
        head_section = out.split("--- head (", 1)[1]
        head = head_section.split(" bytes) ---\n", 1)[1].split("\n--- tail (", 1)[0]
        tail_section = out.split("\n--- tail (", 1)[1]
        return head, tail_section.split(" bytes) ---\n", 1)[1]

    def test_file_mode_small_output_is_not_repeated(self):
        # 全量が FILE_EXCERPT_BYTES 以下: head と tail に分けず本文を一度だけ返す
        tool = self.numbered_output_tool(100)  # 500 bytes
        out = run_sync(tool, tool.argv, file_dir=self.file_dir)
        body = "".join(f"{i:04d}\n" for i in range(100))
        self.assertIn("500 bytes", out)
        self.assertNotIn("--- head (", out)
        self.assertNotIn("--- tail (", out)
        self.assertIn("full output saved to file", out)
        self.assertTrue(out.endswith(body))
        self.assertEqual(1, out.count("0000\n"))  # 先頭行が二度現れない
        # 応答が全量を含むので「全部読むな」の助言は不要
        self.assertNotIn("Do not read it whole", out)

    def test_file_mode_excerpts_do_not_overlap(self):
        # FILE_EXCERPT_BYTES 超 2 倍以下: tail を head の続きから始めて重複を消す
        tool = self.numbered_output_tool(300)  # 1500 bytes
        out = run_sync(tool, tool.argv, file_dir=self.file_dir)
        head, tail = self.excerpts(out)
        self.assertIn("--- head (1000 bytes) ---", out)
        self.assertIn("--- tail (500 bytes) ---", out)
        self.assertEqual("".join(f"{i:04d}\n" for i in range(300)), head + tail)

    def test_file_mode_large_output_keeps_head_and_tail(self):
        # 2 倍超: 従来どおり先頭と末尾を FILE_EXCERPT_BYTES ずつ抜粋する
        tool = self.numbered_output_tool(600)  # 3000 bytes
        out = run_sync(tool, tool.argv, file_dir=self.file_dir)
        head, tail = self.excerpts(out)
        self.assertEqual("".join(f"{i:04d}\n" for i in range(200)), head)
        self.assertEqual("".join(f"{i:04d}\n" for i in range(400, 600)), tail)
        self.assertIn("Do not read it whole", out)

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
