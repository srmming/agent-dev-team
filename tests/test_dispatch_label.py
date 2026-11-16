#!/usr/bin/env python3
"""桌面应用包装过的派活单也认成子代理；派活单里的任务名、子代理、档位存进对话状态文件（状态栏显示用）。
状态目录用 AGENT_DEV_TEAM_HOME 指到临时目录，不碰真实的 ~/.claude。
"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_modes import Base  # noqa: E402  (它会把 hooks/scripts 加进 sys.path)
import common  # noqa: E402

HEAD = "Background (from the session that started this one):"


def wrapped(task_text, background="负责人在改状态栏\n要读 register.tsx"):
    return f"{HEAD}\n{background}\n\nTask:\n{task_text}"


class DispatchLabelTest(Base):
    def submit(self, prompt, sid="s1"):
        self.cwd = getattr(self, "cwd", None) or self.make_repo()
        data = {"session_id": sid, "cwd": self.cwd, "hook_event_name": "UserPromptSubmit",
                "transcript_path": "/x/t.jsonl", "permission_mode": "default", "prompt": prompt}
        r = self.run_script("prompt_submit.py", json.dumps(data))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stderr, "")
        return r.stdout

    def context(self, out):
        return json.loads(out)["hookSpecificOutput"]["additionalContext"]

    def state_or_empty(self, sid="s1"):
        return self.state(sid) if os.path.exists(self.session_file(sid)) else {}

    def test_普通开头_存任务名子代理档位(self):
        ctx = self.context(self.submit("【派活单】子代理窗口识别\n子代理：dev-opus · 中\n1. 目标：……"))
        self.assertIn("这个窗口是子代理", ctx)
        self.assertEqual(self.state("s1"), {"role": "subagent", "task": "子代理窗口识别",
                                            "agent": "dev-opus", "effort": "中"})

    def test_没有子代理那一行_只存任务名_不报错(self):
        self.context(self.submit("【派活单】  修个 bug  \n1. 目标：……"))
        self.assertEqual(self.state("s1"), {"role": "subagent", "task": "修个 bug"})

    def test_新派活单没有子代理行_清掉旧的子代理和档位(self):
        self.set_state("s1", role="subagent", task="旧", agent="dev-haiku", effort="低", status="x")
        self.context(self.submit("【派活单】新任务\n1. 目标"))
        self.assertEqual(self.state("s1"), {"role": "subagent", "task": "新任务", "status": "x"})

    def test_子代理行格式不对就不认(self):
        for line in ("子代理：dev-opus", "子代理：dev-opus · 特高", "子代理 dev-opus · 中", "子代理：dev opus · 中"):
            self.context(self.submit(f"【派活单】T\n{line}\n1. 目标", sid="x"))
            self.assertEqual(self.state("x"), {"role": "subagent", "task": "T"}, line)
        self.context(self.submit("【派活单】T\n子代理:dev-fable·高", sid="y"))
        self.assertEqual(self.state("y")["agent"], "dev-fable")
        self.assertEqual(self.state("y")["effort"], "高")

    def test_包装形式_认成子代理(self):
        ctx = self.context(self.submit(wrapped("【派活单】状态栏显示型号\n子代理：dev-sonnet · 高\n1. 目标")))
        self.assertIn("这个窗口是子代理", ctx)
        self.assertEqual(self.state("s1"), {"role": "subagent", "task": "状态栏显示型号",
                                            "agent": "dev-sonnet", "effort": "高"})

    def test_包装形式_Task和派活单之间有空行也认(self):
        self.context(self.submit(f"{HEAD}\n背景\n\nTask:\n\n  \n【派活单】T2\n子代理：dev-haiku · 低"))
        self.assertEqual(self.state("s1"), {"role": "subagent", "task": "T2", "agent": "dev-haiku", "effort": "低"})

    def test_包装形式_快修(self):
        ctx = self.context(self.submit(wrapped("【快修】按钮文字改一下")))
        self.assertIn("对接窗口", ctx)
        self.assertEqual(self.state("s1"), {"role": "kuaixiu"})

    def test_包装里Task下一行不是派活单_不切换(self):
        for text in (wrapped("帮我看下这个【派活单】"), wrapped("修个 bug\n【派活单】T"),
                     f"{HEAD}\n背景\nTask: 【派活单】T", f"{HEAD}\n背景\n【派活单】T"):
            self.assertEqual(self.submit(text), "", text)
            self.assertEqual(self.state_or_empty(), {}, text)

    def test_正文中间出现派活单_不切换(self):
        self.set_state("s1", role="lead")
        for text in ("请看这张：\n【派活单】T\n子代理：dev-opus · 中",
                     "Task:\n【派活单】T",
                     "前言\nBackground (from the session that started this one):\nTask:\n【派活单】T"):
            self.submit(text)
            self.assertEqual(self.state("s1"), {"role": "lead"}, text)

    def test_save_session传None就删掉这个键(self):
        common.save_session("s9", role="lead", agent="dev-opus")
        self.assertEqual(common.save_session("s9", agent=None), {"role": "lead"})
        self.assertEqual(self.state("s9"), {"role": "lead"})


if __name__ == "__main__":
    unittest.main()
