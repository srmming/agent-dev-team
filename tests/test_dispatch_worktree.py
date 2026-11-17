#!/usr/bin/env python3
"""桌面应用在工作副本里开的新会话：第一条消息最前面多一段固定的工作副本提醒，后面才是包装和派活单。
这段提醒逐字认、只认开头；正文中间出现【派活单】不认。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_dispatch_label as tdl  # noqa: E402
from test_modes import Base  # noqa: E402

HEAD, wrapped = tdl.HEAD, tdl.wrapped

NOTE = ("<system-reminder>\nYou are operating in a git worktree.\n"
        "Worktree path: /Users/x/proj/.claude/worktrees/sweet-dewdney-3efd18\n"
        "Worktree name: sweet-dewdney-3efd18\n</system-reminder>\n\n")
TAIL = "\n\nShared notes folder for this group of sessions (outside the repo; ...): /x/notes"


class WorktreeDispatchTest(Base):
    submit = tdl.DispatchLabelTest.submit
    context = tdl.DispatchLabelTest.context

    def test_工作副本提醒加包装_认成子代理(self):
        ctx = self.context(self.submit(NOTE + wrapped("【派活单】守卫误拦\n子代理：dev-opus · 高\n1. 目标") + TAIL))
        self.assertIn("这个窗口是子代理", ctx)
        self.assertEqual(self.state("s1"), {"role": "subagent", "task": "守卫误拦", "agent": "dev-opus", "effort": "高"})

    def test_工作副本提醒后直接是派活单或快修_也认(self):
        self.context(self.submit(NOTE + "【派活单】T\n子代理：dev-haiku · 低"))
        self.assertEqual(self.state("s1"), {"role": "subagent", "task": "T", "agent": "dev-haiku", "effort": "低"})
        self.assertIn("对接窗口", self.context(self.submit(NOTE + wrapped("【快修】改按钮"), sid="k")))
        self.assertEqual(self.state("k"), {"role": "kuaixiu"})

    def test_提醒不是逐字那一段_不认(self):
        body = wrapped("【派活单】T\n子代理：dev-opus · 中")
        for note in (NOTE.replace("Worktree name: sweet-dewdney-3efd18\n", ""),       # 少一行
                     NOTE.replace("</system-reminder>", "Extra line\n</system-reminder>"),  # 多一行
                     NOTE.replace("operating in", "working in"),                     # 改了字
                     NOTE.replace("Worktree path: /Users/x/proj/.claude/worktrees/sweet-dewdney-3efd18", "Worktree path: "),
                     NOTE.replace("\n\n", ""),                                       # 后面没换行
                     "前言\n" + NOTE,                                                # 不在开头
                     NOTE + NOTE):                                                   # 两段
            self.set_state("s1", role="lead")
            self.submit(note + body)
            self.assertEqual(self.state("s1"), {"role": "lead"}, note)

    def test_提醒加包装但派活单不在规定位置_不认(self):
        for text in (NOTE + wrapped("帮我看下这个【派活单】"), NOTE + wrapped("修个 bug\n【派活单】T"),
                     NOTE + f"{HEAD}\n背景\n【派活单】T", NOTE + "请看：\n【派活单】T",
                     NOTE + f"{HEAD}\n背景\nTask: 【派活单】T"):
            self.set_state("s1", role="lead")
            self.submit(text)
            self.assertEqual(self.state("s1"), {"role": "lead"}, text)

    def test_提醒后面是模式口令_不切换(self):
        self.set_state("s1", role="lead")
        self.submit(NOTE + "普通对话")
        self.assertEqual(self.state("s1"), {"role": "lead"})


if __name__ == "__main__":
    unittest.main()
