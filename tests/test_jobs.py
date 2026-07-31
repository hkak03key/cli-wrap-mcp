"""job モード (起動・状態確認・結果取得・キャンセル) のテスト。

実行: uv run pytest
"""
import sys
import tempfile
import unittest
from pathlib import Path

from cli_wrap_mcp.jobs import JobManager
from cli_wrap_mcp.spec import ParamValidationError, ToolSpec


class JobModeTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.jobs = JobManager(Path(self._tmp.name) / "jobs")
        self.tool = ToolSpec(
            name="j", description="", argv=[], mode="job", inline_max_output_bytes=10_000,
        )

    def tearDown(self):
        self._tmp.cleanup()

    def wait_exit(self, job_id, timeout=10.0):
        import time

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state, rc = self.jobs._poll(job_id)
            if state != "running":
                return state, rc
            time.sleep(0.05)
        raise AssertionError("job did not exit in time")

    def job_id_from(self, message: str) -> str:
        first = message.splitlines()[0]
        self.assertTrue(first.startswith("job started: "), message)
        return first.removeprefix("job started: ")

    def test_start_status_result_lifecycle(self):
        msg = self.jobs.start(
            self.tool, [sys.executable, "-c", "print('job-out'); import sys; sys.exit(0)"],
        ).text
        job_id = self.job_id_from(msg)
        state, rc = self.wait_exit(job_id)
        self.assertEqual(("exited", 0), (state, rc))
        status = self.jobs.status(job_id).text
        self.assertIn("exited", status)
        self.assertIn("job-out", status)
        result = self.jobs.result(job_id, self.tool.inline_max_output_bytes).text
        self.assertIn("job-out", result)
        jdir = self.jobs.jobs_dir / job_id
        for name in ("stdout.log", "stderr.log", "pid", "meta.json", "exit_code"):
            self.assertTrue((jdir / name).exists(), name)

    def test_result_while_running_says_running(self):
        msg = self.jobs.start(self.tool, [sys.executable, "-c", "import time; time.sleep(30)"]).text
        job_id = self.job_id_from(msg)
        try:
            self.assertIn("still running", self.jobs.result(job_id, 1000).text)
            self.assertIn("running", self.jobs.status(job_id).text)
        finally:
            self.jobs.cancel(job_id)

    def test_cancel_terminates_job(self):
        msg = self.jobs.start(self.tool, [sys.executable, "-c", "import time; time.sleep(30)"]).text
        job_id = self.job_id_from(msg)
        out = self.jobs.cancel(job_id).text
        self.assertIn("SIGTERM", out)
        state, rc = self.wait_exit(job_id)
        self.assertEqual("exited", state)
        self.assertNotEqual(0, rc)

    def test_nonzero_exit_result_includes_stderr(self):
        msg = self.jobs.start(
            self.tool,
            [sys.executable, "-c", "import sys; sys.stderr.write('job-err'); sys.exit(2)"],
        ).text
        job_id = self.job_id_from(msg)
        self.wait_exit(job_id)
        result = self.jobs.result(job_id, 1000).text
        self.assertIn("exit code 2", result)
        self.assertIn("job-err", result)

    # --- job_id インジェクション (パストラバーサル) 対策 -------------------

    def test_malformed_job_id_rejected(self):
        for bad in ("../../etc/passwd", "x; rm -rf /", "20260715T000000-XYZ!!", ""):
            with self.assertRaisesRegex(ParamValidationError, "invalid job_id"):
                self.jobs.status(bad)

    def test_unknown_but_wellformed_job_id_is_error(self):
        with self.assertRaisesRegex(ParamValidationError, "unknown job_id"):
            self.jobs.status("20260101T000000-abc123")


if __name__ == "__main__":
    unittest.main()
