#!/usr/bin/env python3
"""审核命令写死快速模式：DEFAULT_CMD 带 service_tier=priority，型号不写死。
运行：python3 -m unittest tests.test_review_cmd
"""
import os
import sys
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SCRIPTS = os.path.join(ROOT, "hooks", "scripts")
sys.path.insert(0, SCRIPTS)
import review  # noqa: E402


class DefaultReviewCmdTest(unittest.TestCase):
    def test_fast_mode_pinned(self):
        self.assertIn("-c service_tier=priority", review.DEFAULT_CMD)

    def test_reasoning_effort_max(self):
        self.assertIn("-c model_reasoning_effort=max", review.DEFAULT_CMD)

    def test_model_not_pinned(self):
        self.assertNotIn("model=", review.DEFAULT_CMD)


if __name__ == "__main__":
    unittest.main()
