"""job モード (起動・状態確認・結果取得・キャンセル) のテスト。

実行: uv run pytest
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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

    # --- is_error (ラップ先の成否と、操作自体の成否の区別) -----------------

    def test_start_failure_is_error(self):
        reply = self.jobs.start(self.tool, ["/nonexistent/binary"])
        self.assertIn("failed to start", reply.text)
        self.assertTrue(reply.is_error)

    def test_start_success_is_not_error(self):
        reply = self.jobs.start(self.tool, [sys.executable, "-c", "pass"])
        self.assertFalse(reply.is_error)
        self.wait_exit(self.job_id_from(reply.text))

    def test_result_of_failed_job_is_error(self):
        msg = self.jobs.start(self.tool, [sys.executable, "-c", "import sys; sys.exit(3)"]).text
        job_id = self.job_id_from(msg)
        self.wait_exit(job_id)
        self.assertTrue(self.jobs.result(job_id, 1000).is_error)

    def test_result_of_signal_killed_job_is_error(self):
        # 非ゼロ側のもう一方の境界: シグナル終了は exit code が負値になる
        msg = self.jobs.start(
            self.tool,
            [sys.executable, "-c", "import os, signal; os.kill(os.getpid(), signal.SIGKILL)"],
        ).text
        job_id = self.job_id_from(msg)
        _state, rc = self.wait_exit(job_id)
        self.assertEqual(-9, rc)
        self.assertTrue(self.jobs.result(job_id, 1000).is_error)

    def test_result_of_successful_job_is_not_error(self):
        msg = self.jobs.start(self.tool, [sys.executable, "-c", "print('ok')"]).text
        job_id = self.job_id_from(msg)
        self.wait_exit(job_id)
        self.assertFalse(self.jobs.result(job_id, 1000).is_error)

    def test_result_while_running_is_not_error(self):
        # 実行中の案内は問い合わせとして成立しているので失敗ではない
        msg = self.jobs.start(self.tool, [sys.executable, "-c", "import time; time.sleep(30)"]).text
        job_id = self.job_id_from(msg)
        try:
            self.assertFalse(self.jobs.result(job_id, 1000).is_error)
        finally:
            self.jobs.cancel(job_id)

    def test_status_of_failed_job_is_not_error(self):
        msg = self.jobs.start(self.tool, [sys.executable, "-c", "import sys; sys.exit(3)"]).text
        job_id = self.job_id_from(msg)
        self.wait_exit(job_id)
        self.assertFalse(self.jobs.status(job_id).is_error)

    # --- cancel の全経路 ---------------------------------------------------

    def orphan_job(self, pid_text: str | None) -> str:
        """プロセスハンドルを持たない job dir を作る (サーバー再起動後の孤児を模す)。"""
        job_id = "20260101T000000-abcdef"
        jdir = self.jobs.jobs_dir / job_id
        jdir.mkdir(parents=True)
        if pid_text is not None:
            (jdir / "pid").write_text(pid_text, encoding="utf-8")
        return job_id

    def test_cancel_running_job_is_not_error(self):
        msg = self.jobs.start(self.tool, [sys.executable, "-c", "import time; time.sleep(30)"]).text
        job_id = self.job_id_from(msg)
        reply = self.jobs.cancel(job_id)
        self.assertIn("SIGTERM", reply.text)
        self.assertFalse(reply.is_error)
        self.wait_exit(job_id)

    def test_cancel_already_exited_job_is_not_error(self):
        # 止める対象が既に終わっているのは要求どおりの結末
        msg = self.jobs.start(self.tool, [sys.executable, "-c", "pass"]).text
        job_id = self.job_id_from(msg)
        self.wait_exit(job_id)
        reply = self.jobs.cancel(job_id)
        self.assertIn("already exited", reply.text)
        self.assertFalse(reply.is_error)

    def test_cancel_when_process_already_gone_is_not_error(self):
        # pid は読めるがプロセスが居ない → 要求どおり (止める対象が居ない)
        job_id = self.orphan_job(str(self.unused_pid()))
        reply = self.jobs.cancel(job_id)
        self.assertIn("not running", reply.text)
        self.assertFalse(reply.is_error)

    def test_cancel_with_unreadable_pid_is_error(self):
        # pid を読めないのは要求に応えられていない → 失敗
        job_id = self.orphan_job(None)
        reply = self.jobs.cancel(job_id)
        self.assertIn("cannot read pid", reply.text)
        self.assertTrue(reply.is_error)

    def test_cancel_when_signal_fails_is_error(self):
        job_id = self.orphan_job(str(self.unused_pid()))
        with mock.patch("os.killpg", side_effect=PermissionError("denied")):
            reply = self.jobs.cancel(job_id)
        self.assertIn("failed to terminate", reply.text)
        self.assertTrue(reply.is_error)

    def unused_pid(self) -> int:
        """生きていない pid を 1 つ返す (見つからなければテストを skip する)。"""
        for pid in range(400_000, 400_200):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return pid
            except OSError:
                continue
        self.skipTest("no unused pid found")

    # --- 未知 (unknown) 状態 -----------------------------------------------

    def test_result_of_unknown_state_is_error(self):
        # exit code を確定できない以上、成功とは伝えられない
        job_id = self.orphan_job(None)
        reply = self.jobs.result(job_id, 1000)
        self.assertIn("unknown", reply.text)
        self.assertNotIn("stderr (tail)", reply.text)  # 失敗自体が未確定なので付けない
        self.assertTrue(reply.is_error)

    def test_status_of_unknown_state_is_not_error(self):
        job_id = self.orphan_job(None)
        reply = self.jobs.status(job_id)
        self.assertIn("unknown", reply.text)
        self.assertFalse(reply.is_error)

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
