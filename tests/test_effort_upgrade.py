#!/usr/bin/env python3
"""升档消息：子代理窗口里调高档位后，发【升档】+ 第二行「子代理：<名字> · <档位>」，状态文件里的档位和名字跟着变。"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_dispatch_label  # noqa: E402
import common  # noqa: E402
wrapped = test_dispatch_label.wrapped

DISPATCH = "【派活单】修状态栏\n子代理：dev-sonnet · 中\n1. 目标：……"
SUB = {"role": "subagent", "task": "修状态栏", "agent": "dev-sonnet", "effort": "中"}


class EffortUpgradeTest(test_dispatch_label.DispatchLabelTest):
    def test_升档消息_档位和名字更新_任务不变(self):
        self.submit(DISPATCH)
        out = self.submit("【升档】\n子代理：dev-opus · 高\n第三行起：边界情况没处理")
        self.assertEqual(self.state("s1"), {"role": "subagent", "task": "修状态栏",
                                            "agent": "dev-opus", "effort": "高"})
        self.assertNotIn("守则", out)  # 不重复注入派活单守则

    def test_包装形式也认(self):
        self.submit(DISPATCH)
        self.submit(wrapped("【升档】\n子代理：dev-sonnet · 高\n哪里不对"))
        self.assertEqual(self.state("s1"), dict(SUB, effort="高"))

    def test_非子代理会话_不写状态(self):
        self.set_state("s1", role="lead")
        self.submit("【升档】\n子代理：dev-opus · 高\n哪里不对")
        self.assertEqual(self.state("s1"), {"role": "lead"})
        self.assertEqual(self.state_or_empty("s2"), {})
        self.submit("【升档】\n子代理：dev-opus · 高", sid="s2")
        self.assertEqual(self.state_or_empty("s2"), {})

    def test_格式不对_不写状态(self):
        self.submit(DISPATCH)
        for text in ("【升档】\n子代理：dev-opus · 特高", "【升档】\n\n子代理：dev-opus · 高", "【升档】", "【升档】 \n子代理：dev-opus · 高",
                     "【升档】x\n子代理：dev-opus · 高", "请【升档】\n子代理：dev-opus · 高", "子代理：dev-opus · 高\n【升档】"):
            self.submit(text)
            self.assertEqual(self.state("s1"), SUB, text)

    def test_原文不严格_不写状态(self):
        self.submit(DISPATCH)
        for text in (" 【升档】\n子代理：dev-opus · 高", "\n【升档】\n子代理：dev-opus · 高",
                     wrapped("  【升档】\n子代理：dev-opus · 高"), wrapped("\t【升档】\n子代理：dev-opus · 高")):
            self.submit(text)
            self.assertEqual(self.state("s1"), SUB, text)

    def test_被拒升档_不写told_无输出(self):
        self.submit(DISPATCH)
        self.submit("普通消息")
        told = common.load_told("s1")
        self.assertIsNotNone(told)
        self.set_state("s3", role="lead")
        for text, sid in (("【升档】\n子代理：dev-opus · 特高", "s1"), ("【升档】\n子代理：dev-opus · 高", "s3")):
            self.assertEqual(self.submit(text, sid=sid), "", text)
        self.assertEqual(self.state_or_empty("s3"), {"role": "lead"})
        self.assertEqual(common.load_told("s1"), told)
        self.assertIsNone(common.load_told("s3"))


if __name__ == "__main__":
    unittest.main()
