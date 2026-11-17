#!/usr/bin/env python3
"""审核命令不继承调用方的标准输入：调用方的 stdin 是一个一直不关的管道时，
审核命令拿到的是空输入，不会一直等下去（codex exec 在这种情况下会等 stdin 结束）。
运行：python3 -m unittest tests.test_review_stdin
"""
import os
import sys
import tempfile
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SCRIPTS = os.path.join(ROOT, "hooks", "scripts")
sys.path.insert(0, SCRIPTS)
import review  # noqa: E402

FAKE = """#!/usr/bin/env python3
import sys
data = sys.stdin.read()
print("STDIN_BYTES=%d" % len(data))
print("VERDICT: PASS")
"""


class ReviewStdinTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.fake = os.path.join(self.tmp.name, "fake-review")
        with open(self.fake, "w", encoding="utf-8") as f:
            f.write(FAKE)
        os.chmod(self.fake, 0o755)
        # 把本进程的 fd 0 换成一个管道的读端，写端一直开着不关
        self.saved = os.dup(0)
        self.r, self.w = os.pipe()
        os.dup2(self.r, 0)
        self.old_timeout = review.TIMEOUT
        review.TIMEOUT = 10

    def tearDown(self):
        review.TIMEOUT = self.old_timeout
        os.dup2(self.saved, 0)
        for fd in (self.saved, self.r, self.w):
            os.close(fd)
        self.tmp.cleanup()

    def test_stdin是不关的管道_审核命令拿到空输入_不挂住(self):
        try:
            out, usage = review.run_codex(self.fake, "high", "提示词", self.tmp.name)
        except review.CodexError as e:
            self.fail(f"审核命令挂住了：{e}")
        self.assertIn("STDIN_BYTES=0", out)
        self.assertIsNone(usage)


if __name__ == "__main__":
    unittest.main()
