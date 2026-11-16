#!/usr/bin/env python3
"""文字和实现对得上：钩子注册、版本号、技能长度、子代理守则、README 里的脚本参数和消息口令。
只读仓库里的文件，不碰状态目录。
运行：python3 -m unittest discover -s tests -v
"""
import json
import os
import re
import subprocess
import sys
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SCRIPTS = os.path.join(ROOT, "hooks", "scripts")
sys.path.insert(0, SCRIPTS)
import common  # noqa: E402
import prompt_submit  # noqa: E402


def read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as f:
        return f.read()


def help_text(script, *sub):
    r = subprocess.run([sys.executable, os.path.join(SCRIPTS, script), *sub, "--help"],
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return r.stdout


class Docs(unittest.TestCase):
    def test_三个钩子都注册了(self):
        hooks = json.loads(read("hooks", "hooks.json"))["hooks"]
        pre = hooks["PreToolUse"][0]
        self.assertEqual(set(pre["matcher"].split("|")), {"Edit", "Write", "MultiEdit", "NotebookEdit", "Bash"})
        self.assertIn("guard.py", pre["hooks"][0]["command"])
        self.assertEqual(pre["hooks"][0]["timeout"], 30)
        self.assertIn("session_start.py", hooks["SessionStart"][0]["hooks"][0]["command"])
        self.assertIn("prompt_submit.py", hooks["UserPromptSubmit"][0]["hooks"][0]["command"])
        for event in hooks.values():
            for entry in event:
                for h in entry["hooks"]:
                    name = re.search(r"hooks/scripts/(\w+\.py)", h["command"]).group(1)
                    self.assertTrue(os.path.exists(os.path.join(SCRIPTS, name)), name)

    def test_版本号都是0_2_2(self):
        self.assertEqual(json.loads(read(".claude-plugin", "plugin.json"))["version"], "0.2.3")
        for name in ("lead", "kuaixiu", "shouwei", "shuijiao", "qingli"):
            self.assertIn('version: "0.2.3"', read("skills", name, "SKILL.md"), name)

    def test_技能长度(self):
        limits = {"lead": 150, "kuaixiu": 50, "shouwei": 50, "shuijiao": 50}
        for name, n in limits.items():
            lines = read("skills", name, "SKILL.md").splitlines()
            self.assertLessEqual(len(lines), n, name)

    def test_agents目录里只有四个子代理_每个都有frontmatter(self):
        names = sorted(os.listdir(os.path.join(ROOT, "agents")))
        self.assertEqual(names, ["dev-fable.md", "dev-haiku.md", "dev-opus.md", "dev-sonnet.md"])
        for n in names:
            self.assertTrue(read("agents", n).startswith("---\n"), n)

    def test_四个子代理的守则和守则文件一致(self):
        self.assertEqual(common.RULES_FILE, os.path.join(ROOT, "hooks", "rules", "subagent.md"))
        rules = read("hooks", "rules", "subagent.md")
        block = rules[rules.index("你是负责人派出的开发子代理"):].strip()
        self.assertIn("20 行以内", block)
        self.assertIn("git rebase", block)
        for name in ("dev-haiku", "dev-sonnet", "dev-opus", "dev-fable"):
            self.assertIn(block, read("agents", name + ".md"), name)
        # 窗口子代理拿到的就是这份全文
        self.assertIn(block, common.rules_text())

    def test_README里写的脚本参数都真的有(self):
        readme = read("README.md")
        for script in ("verify.py", "review.py", "deploy_check.py", "rollback.py", "mode.py"):
            row = next(ln for ln in readme.splitlines() if ln.startswith(f"| {script} |"))
            flags = set(re.findall(r"(--[a-z][a-z-]*)", row))
            self.assertTrue(flags, script)
            helps = help_text(script) if script != "mode.py" else help_text(script, "get") + help_text(script, "set")
            for flag in flags:
                self.assertIn(flag, helps, f"{script} {flag}")

    def test_技能里写的脚本参数都真的有(self):
        helps = {s: help_text(s) for s in ("verify.py", "review.py", "deploy_check.py", "rollback.py")}
        for name in ("lead", "kuaixiu"):
            for line in read("skills", name, "SKILL.md").splitlines():
                for script, h in helps.items():
                    if f"<{script} 路径>" in line:
                        tail = line.split(f"<{script} 路径>", 1)[1].split("`")[0]
                        for flag in re.findall(r"(--[a-z][a-z-]*)", tail):
                            self.assertIn(flag, h, f"{name}: {line}")

    def test_负责人提示里给的脚本路径都存在(self):
        text = common.lead_text("full", "s1")
        for script in ("verify.py", "review.py", "deploy_check.py", "rollback.py", "mode.py"):
            path = os.path.join(SCRIPTS, script)
            self.assertIn(path, text)
            self.assertTrue(os.path.isabs(path) and os.path.exists(path))
        self.assertIn("s1", text)

    def test_消息口令和文字一致(self):
        self.assertEqual(set(prompt_submit.EXACT), {"负责人模式", "普通对话", "全自动", "半自动"})
        readme = read("README.md")
        for word in list(prompt_submit.EXACT) + ["【派活单】", "【快修】"]:
            self.assertIn(word, readme, word)
        self.assertIn("【派活单】", read("skills", "lead", "SKILL.md"))
        self.assertIn("【快修】", read("skills", "kuaixiu", "SKILL.md"))
        self.assertIn("负责人模式", common.NORMAL_TEXT)
        self.assertIn("普通对话", common.lead_text("full", "s1"))


if __name__ == "__main__":
    unittest.main()
