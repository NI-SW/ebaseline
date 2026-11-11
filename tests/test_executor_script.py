import threading
import time
import unittest
from pathlib import Path

from ebaseline.config import load_config
from ebaseline.executor import _run_script


def setUpModule():
    load_config(Path(__file__).resolve().parent.parent / "env.conf.example")


class RunScriptTest(unittest.TestCase):
    def test_inline_echo(self):
        res = _run_script("bash -c 'echo hello'", timeout=5)
        self.assertTrue(res["success"])
        self.assertIn("hello", res["stdout"])
        self.assertFalse(res["cancelled"])

    def test_stdin_is_passed(self):
        res = _run_script("bash -c 'cat'", timeout=5, stdin_text="payload")
        self.assertTrue(res["success"])
        self.assertIn("payload", res["stdout"])

    def test_cancel_terminates_process_group(self):
        event = threading.Event()
        timer = threading.Timer(0.3, event.set)
        timer.start()
        started = time.monotonic()
        try:
            res = _run_script("bash -c 'sleep 5'", timeout=10, cancel_event=event)
        finally:
            timer.cancel()
        elapsed = time.monotonic() - started
        self.assertTrue(res["cancelled"])
        self.assertEqual(res["returncode"], -2)
        self.assertLess(elapsed, 4)

    def test_timeout_terminates_process_group(self):
        started = time.monotonic()
        res = _run_script("bash -c 'sleep 5'", timeout=1)
        elapsed = time.monotonic() - started
        self.assertTrue(res.get("timed_out"))
        self.assertFalse(res["success"])
        self.assertLess(elapsed, 5)

    def test_missing_script(self):
        res = _run_script("does_not_exist_12345.py", timeout=1)
        self.assertFalse(res["success"])
        self.assertIn("脚本不存在", res["stderr"])


if __name__ == "__main__":
    unittest.main()
