#!/usr/bin/env python3
"""角色判断、发消息切换模式、mode.py、会话开始提示的测试。
全部用临时目录和临时 git 仓库；状态目录用 AGENT_DEV_TEAM_HOME 指到临时目录，不碰真实的 ~/.claude。
运行：python3 -m unittest discover -s tests -v
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

SCRIPTS = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "hooks", "scripts"))
PLUGIN = os.path.dirname(os.path.dirname(SCRIPTS))
sys.path.insert(0, SCRIPTS)
import common  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="adt-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = os.path.join(self.tmp, "state")
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("AGENT_DEV_TEAM") and not k.startswith("GIT_")}
        env.update(
            AGENT_DEV_TEAM_HOME=self.home, HOME=os.path.join(self.tmp, "home"),
            GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1",
            GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.com",
            GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.com",
        )
        os.makedirs(env["HOME"])
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def git(self, cwd, *args):
        return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()

    def make_dir(self, name):
        d = os.path.join(self.tmp, name)
        os.makedirs(d)
        return d

    def make_repo(self, name="repo", branch="main"):
        d = self.make_dir(name)
        self.git(d, "init", "-q", "-b", branch)
        self.git(d, "commit", "-q", "--allow-empty", "-m", "init")
        return d

    def write(self, path, text):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)

    def session_file(self, sid):
        return os.path.join(self.home, "sessions", sid + ".json")

    def set_state(self, sid, **state):
        self.write(self.session_file(sid), json.dumps(state, ensure_ascii=False))

    def state(self, sid):
        with open(self.session_file(sid), encoding="utf-8") as f:
            return json.load(f)

    def run_script(self, name, stdin="", *args, env=None):
        full_env = dict(os.environ)
        full_env.update(env or {})
        return subprocess.run([sys.executable, os.path.join(SCRIPTS, name), *args],
                              input=stdin, capture_output=True, text=True, env=full_env)


class RoleTest(Base):
    """规格 1.2：按顺序判断角色。"""

    def test_1_守卫关掉就是none_排在最前面(self):
        repo = self.make_repo()
        self.set_state("s1", role="lead")
        data = {"session_id": "s1", "cwd": repo, "agent_id": "a1"}
        with mock.patch.dict(os.environ, {"AGENT_DEV_TEAM": "off"}):
            self.assertEqual(common.role(data), "none")
        with mock.patch.dict(os.environ, {"AGENT_DEV_TEAM": "OFF"}):
            self.assertEqual(common.role(data), "none")
        self.assertEqual(common.role(data), "subagent")

    def test_1_项目根目录有关闭文件就是none(self):
        repo = self.make_repo()
        sub = os.path.join(repo, "src")
        os.makedirs(sub)
        self.write(os.path.join(repo, ".claude", "agent-dev-team-off"), "")
        self.assertEqual(common.role({"session_id": "s1", "cwd": sub}), "none")
        # 不在仓库里时，项目根目录就是 cwd
        plain = self.make_dir("plain")
        self.assertEqual(common.role({"session_id": "s1", "cwd": plain}), "lead")
        self.write(os.path.join(plain, ".claude", "agent-dev-team-off"), "")
        self.assertEqual(common.role({"session_id": "s1", "cwd": plain}), "none")

    def test_2_有agent_id就是子代理_比状态文件优先(self):
        repo = self.make_repo()
        self.set_state("s1", role="normal")
        self.assertEqual(common.role({"session_id": "s1", "cwd": repo, "agent_id": "a1"}), "subagent")

    def test_3_状态文件里有role就用它(self):
        repo = self.make_repo()
        for r in ("lead", "normal", "subagent", "kuaixiu"):
            self.set_state("s1", role=r)
            self.assertEqual(common.role({"session_id": "s1", "cwd": repo}), r)
        # 别的对话不受影响
        self.assertEqual(common.role({"session_id": "s2", "cwd": repo}), "lead")

    def test_3_状态文件比分支名优先(self):
        repo = self.make_repo(branch="kuaixiu/fix-typo")
        self.set_state("s1", role="normal")
        self.assertEqual(common.role({"session_id": "s1", "cwd": repo}), "normal")

    def test_4_旧做法_kuaixiu分支是对接窗口(self):
        repo = self.make_repo(branch="kuaixiu/fix-typo")
        self.assertEqual(common.role({"session_id": "s1", "cwd": repo}), "kuaixiu")
        # 状态文件里只有 auto、没有 role 时，仍按分支名认
        self.set_state("s1", auto="semi")
        self.assertEqual(common.role({"session_id": "s1", "cwd": repo}), "kuaixiu")

    def test_5_其余都是负责人_不在git仓库也是(self):
        repo = self.make_repo()
        plain = self.make_dir("plain")
        self.assertEqual(common.role({"session_id": "s1", "cwd": repo}), "lead")
        self.assertEqual(common.role({"session_id": "s1", "cwd": plain}), "lead")

    def test_5_状态文件坏了或role不认识_按负责人(self):
        repo = self.make_repo()
        self.write(self.session_file("s1"), "{坏的")
        self.assertEqual(common.role({"session_id": "s1", "cwd": repo}), "lead")
        self.write(self.session_file("s1"), "[1, 2]")
        self.assertEqual(common.role({"session_id": "s1", "cwd": repo}), "lead")
        self.set_state("s1", role="boss")
        self.assertEqual(common.role({"session_id": "s1", "cwd": repo}), "lead")

    def test_拿不到session_id时用unknown(self):
        repo = self.make_repo()
        self.set_state("unknown", role="normal")
        self.assertEqual(common.role({"cwd": repo}), "normal")
        self.assertEqual(common.role({"cwd": repo, "session_id": ""}), "normal")

    def test_session_id里的怪字符不会跑出状态目录(self):
        p = common.session_path("../../etc/passwd")
        self.assertEqual(os.path.dirname(p), os.path.join(self.home, "sessions"))


class AutoAndConfigTest(Base):
    def test_默认全自动(self):
        repo = self.make_repo()
        self.assertEqual(common.auto_mode({"session_id": "s1", "cwd": repo}), "full")

    def test_项目配置的default_auto(self):
        repo = self.make_repo()
        self.write(os.path.join(repo, ".claude", "agent-dev-team.json"), '{"default_auto": "semi"}')
        self.assertEqual(common.auto_mode({"session_id": "s1", "cwd": repo}), "semi")
        # 对话自己的设置优先
        self.set_state("s1", auto="full")
        self.assertEqual(common.auto_mode({"session_id": "s1", "cwd": repo}), "full")

    def test_项目配置不存在或坏了返回空(self):
        repo = self.make_repo()
        self.assertEqual(common.project_config(repo), {})
        self.write(os.path.join(repo, ".claude", "agent-dev-team.json"), "{坏的")
        self.assertEqual(common.project_config(repo), {})
        self.assertEqual(common.auto_mode({"session_id": "s1", "cwd": repo}), "full")
        self.write(os.path.join(repo, ".claude", "agent-dev-team.json"), '["x"]')
        self.assertEqual(common.project_config(repo), {})
        self.write(os.path.join(repo, ".claude", "agent-dev-team.json"), '{"default_auto": "乱写", "codex_cmd": "x"}')
        self.assertEqual(common.project_config(repo)["codex_cmd"], "x")
        self.assertEqual(common.auto_mode({"session_id": "s1", "cwd": repo}), "full")

    def test_保存时保留不认识的键(self):
        self.set_state("s1", role="lead", status="在派活", extra=[1])
        common.save_session("s1", auto="semi")
        self.assertEqual(self.state("s1"), {"role": "lead", "status": "在派活", "extra": [1], "auto": "semi"})

    def test_状态目录跟着环境变量走(self):
        self.assertEqual(common.state_home(), self.home)
        with mock.patch.dict(os.environ):
            del os.environ["AGENT_DEV_TEAM_HOME"]
            self.assertEqual(common.state_home(),
                             os.path.join(os.environ["HOME"], ".claude", "agent-dev-team"))


class PromptSubmitTest(Base):
    """规格 1.3：发消息切换模式。"""

    def submit(self, prompt, sid="s1", env=None, **extra):
        self.cwd = getattr(self, "cwd", None) or self.make_repo()
        data = {"session_id": sid, "cwd": self.cwd, "hook_event_name": "UserPromptSubmit",
                "transcript_path": "/x/t.jsonl", "permission_mode": "default", "prompt": prompt}
        data.update(extra)
        r = self.run_script("prompt_submit.py", json.dumps(data), env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stderr, "")
        return r.stdout

    def context(self, out):
        payload = json.loads(out)
        self.assertEqual(payload["hookSpecificOutput"]["hookEventName"], "UserPromptSubmit")
        ctx = payload["hookSpecificOutput"]["additionalContext"]
        self.assertLess(len(ctx), 10000)
        return ctx

    def test_派活单开头_切成子代理并注入守则全文(self):
        ctx = self.context(self.submit("【派活单】A1：守卫改黑名单\n\n## 1. 目标\n……"))
        self.assertEqual(self.state("s1")["role"], "subagent")
        self.assertIn("这个窗口是子代理，忽略之前的负责人提示", ctx)
        with open(os.path.join(PLUGIN, "agents", "RULES.md"), encoding="utf-8") as f:
            rules = f.read().strip()
        self.assertIn(rules, ctx)
        self.assertIn("20 行以内", rules)

    def test_快修开头_切成对接窗口(self):
        ctx = self.context(self.submit("  【快修】按钮文字改一下"))
        self.assertEqual(self.state("s1")["role"], "kuaixiu")
        self.assertIn("对接窗口", ctx)
        self.assertIn("不能 push", ctx)

    def test_负责人模式(self):
        self.set_state("s1", role="normal")
        ctx = self.context(self.submit("负责人模式"))
        self.assertEqual(self.state("s1")["role"], "lead")
        self.assertIn("模式已切换成「负责人模式」，用一句话告诉用户即可，不要做别的", ctx)

    def test_普通对话(self):
        ctx = self.context(self.submit("普通对话\n"))
        self.assertEqual(self.state("s1"), {"role": "normal"})
        self.assertIn("模式已切换成「普通对话」，用一句话告诉用户即可，不要做别的", ctx)

    def test_全自动和半自动_不动role_保留别的键(self):
        self.set_state("s1", status="在派活")
        ctx = self.context(self.submit(" 半自动 "))
        self.assertEqual(self.state("s1"), {"status": "在派活", "auto": "semi"})
        self.assertIn("模式已切换成「半自动」，用一句话告诉用户即可，不要做别的", ctx)
        ctx = self.context(self.submit("全自动"))
        self.assertEqual(self.state("s1"), {"status": "在派活", "auto": "full"})
        self.assertIn("模式已切换成「全自动」", ctx)

    def test_普通消息不动_不输出(self):
        self.set_state("s1", role="normal", auto="semi")
        for prompt in ("帮我看看负责人模式是什么", "半自动化测试怎么写", "负责人模式。", "请看这张【派活单】",
                       "切到普通对话", "", "   ", "全自动 半自动"):
            self.assertEqual(self.submit(prompt), "", prompt)
            self.assertEqual(self.state("s1"), {"role": "normal", "auto": "semi"}, prompt)
        # 没有状态文件的对话，普通消息也不会建文件
        self.assertEqual(self.submit("加个功能", sid="s2"), "")
        self.assertFalse(os.path.exists(self.session_file("s2")))

    def test_没有prompt或输入坏了_安静退出(self):
        self.cwd = self.make_repo()
        for stdin in ("", "{坏的", "[]", json.dumps({"session_id": "s1", "cwd": self.cwd}),
                      json.dumps({"session_id": "s1", "cwd": self.cwd, "prompt": 123})):
            r = self.run_script("prompt_submit.py", stdin)
            self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""), stdin)
        self.assertFalse(os.path.exists(os.path.join(self.home, "sessions")))

    def test_守卫关掉时不切换(self):
        self.assertEqual(self.submit("普通对话", env={"AGENT_DEV_TEAM": "off"}), "")
        self.assertFalse(os.path.exists(self.session_file("s1")))
        self.write(os.path.join(self.cwd, ".claude", "agent-dev-team-off"), "")
        self.assertEqual(self.submit("普通对话"), "")
        self.assertFalse(os.path.exists(self.session_file("s1")))

    def test_不在git仓库里也能切换(self):
        self.cwd = self.make_dir("plain")
        self.context(self.submit("普通对话"))
        self.assertEqual(self.state("s1"), {"role": "normal"})

    def test_没有session_id时记到unknown(self):
        self.cwd = self.make_repo()
        r = self.run_script("prompt_submit.py", json.dumps({"cwd": self.cwd, "prompt": "半自动"}))
        self.assertEqual(r.returncode, 0)
        self.assertEqual(self.state("unknown"), {"auto": "semi"})


class ModeCliTest(Base):
    def mode(self, *args):
        return self.run_script("mode.py", "", *args)

    def test_get_没有文件时给默认值(self):
        r = self.mode("get", "--session", "s1", "--cwd", self.make_dir("plain"))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout), {"role": "lead", "auto": "full"})
        self.assertFalse(os.path.exists(self.session_file("s1")))

    def test_get_默认的auto看项目配置(self):
        repo = self.make_repo()
        self.write(os.path.join(repo, ".claude", "agent-dev-team.json"), '{"default_auto": "semi"}')
        r = self.mode("get", "--session", "s1", "--cwd", repo)
        self.assertEqual(json.loads(r.stdout), {"role": "lead", "auto": "semi"})

    def test_set_再get(self):
        r = self.mode("set", "--session", "s1", "--role", "normal")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.state("s1"), {"role": "normal"})
        r = self.mode("set", "--session", "s1", "--auto", "semi", "--status", "等 Codex 审核")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.state("s1"), {"role": "normal", "auto": "semi", "status": "等 Codex 审核"})
        self.assertEqual(json.loads(r.stdout), self.state("s1"))
        r = self.mode("get", "--session", "s1")
        self.assertEqual(json.loads(r.stdout), {"role": "normal", "auto": "semi", "status": "等 Codex 审核"})
        for role in ("lead", "subagent", "kuaixiu"):
            self.assertEqual(self.mode("set", "--session", "s1", "--role", role).returncode, 0)
            self.assertEqual(self.state("s1")["role"], role)
        self.assertEqual(self.mode("set", "--session", "s1", "--auto", "full").returncode, 0)
        self.assertEqual(self.state("s1")["auto"], "full")

    def test_set_保留不认识的键(self):
        self.set_state("s1", role="lead", extra={"a": 1})
        self.mode("set", "--session", "s1", "--status", "x")
        self.assertEqual(self.state("s1"), {"role": "lead", "extra": {"a": 1}, "status": "x"})

    def test_set_之后角色判断跟着变(self):
        repo = self.make_repo()
        self.mode("set", "--session", "s1", "--role", "normal")
        self.assertEqual(common.role({"session_id": "s1", "cwd": repo}), "normal")

    def test_非法参数_退出码非0并说明_不写文件(self):
        cases = [
            (["set", "--session", "s1", "--role", "boss"], "--role"),
            (["set", "--session", "s1", "--auto", "half"], "--auto"),
            (["set", "--session", "s1"], "至少"),
            (["set", "--role", "lead"], "session"),
            (["get"], "session"),
            (["get", "--session", ""], "session"),
            (["delete", "--session", "s1"], "参数不对"),
            ([], "get 或 set"),
            (["get", "--session", "s1", "--bogus"], "参数不对"),
        ]
        for args, word in cases:
            r = self.mode(*args)
            self.assertNotEqual(r.returncode, 0, args)
            self.assertEqual(r.stdout, "", args)
            self.assertIn(word, r.stderr, args)
        self.assertFalse(os.path.exists(os.path.join(self.home, "sessions")))

    def test_help(self):
        r = self.mode("--help")
        self.assertEqual(r.returncode, 0)
        self.assertIn("get", r.stdout)
        self.assertEqual(self.mode("set", "--help").returncode, 0)


class SessionStartTest(Base):
    """规格第 4 节：会话开始按角色给提示。"""

    def start(self, cwd, sid="s1", env=None, **extra):
        data = {"session_id": sid, "cwd": cwd, "hook_event_name": "SessionStart", "source": "startup",
                "transcript_path": "/x/t.jsonl", "permission_mode": "default"}
        data.update(extra)
        r = self.run_script("session_start.py", json.dumps(data), env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def test_负责人_写明规矩_模式和五个脚本的绝对路径(self):
        out = self.start(self.make_repo())
        for word in ("负责人模式", "lead 技能", "AskUserQuestion", "只讨论", "全自动", "普通对话", "s1"):
            self.assertIn(word, out)
        self.assertNotIn("当前是「半自动」", out)
        for name in ("verify.py", "review.py", "deploy_check.py", "rollback.py", "mode.py"):
            path = os.path.join(SCRIPTS, name)
            self.assertTrue(os.path.isabs(path))
            self.assertIn(path, out)
        self.assertLess(len(out), 10000)

    def test_负责人_半自动(self):
        repo = self.make_repo()
        self.set_state("s1", auto="semi")
        self.assertIn("当前是「半自动」", self.start(repo))
        self.assertIn("当前是「全自动」", self.start(repo, sid="s2"))

    def test_不在git仓库里也是负责人(self):
        self.assertIn("负责人模式", self.start(self.make_dir("plain")))

    def test_普通对话(self):
        repo = self.make_repo()
        self.set_state("s1", role="normal")
        out = self.start(repo, source="resume")
        self.assertIn("普通对话", out)
        self.assertIn("可以直接改文件", out)
        self.assertIn("负责人模式", out)
        self.assertNotIn("verify.py", out)

    def test_对接窗口(self):
        repo = self.make_repo()
        self.set_state("s1", role="kuaixiu")
        out = self.start(repo)
        self.assertIn("对接窗口", out)
        self.assertIn("不能 push", out)
        self.assertIn("对接窗口", self.start(self.make_repo("old", branch="kuaixiu/x"), sid="s2"))

    def test_子代理(self):
        repo = self.make_repo()
        self.set_state("s1", role="subagent")
        out = self.start(repo, source="compact")
        self.assertIn("子代理", out)
        self.assertIn("不 push", out)
        self.assertIn("子代理", self.start(repo, sid="s2", agent_id="a1", agent_type="dev-opus"))

    def test_守卫关掉不输出(self):
        repo = self.make_repo()
        self.assertEqual(self.start(repo, env={"AGENT_DEV_TEAM": "off"}), "")
        self.write(os.path.join(repo, ".claude", "agent-dev-team-off"), "")
        self.assertEqual(self.start(repo), "")

    def test_输入坏了不报错(self):
        r = self.run_script("session_start.py", "{坏的", env={"AGENT_DEV_TEAM": "off"})
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""))
        r = self.run_script("session_start.py", "[]")
        self.assertEqual((r.returncode, r.stderr), (0, ""))


if __name__ == "__main__":
    unittest.main(verbosity=2)
