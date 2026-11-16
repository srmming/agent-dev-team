#!/usr/bin/env python3
"""端到端：负责人对话里，守卫的上传关和 review.py 的审核记录连起来走一遍。
临时 git 仓库 + 临时远端；AGENT_DEV_TEAM_HOME 和 HOME 都指到临时目录；审核命令是假的，不调真实的 Codex。
守卫用真实的钩子输入格式从 stdin 喂。
运行：python3 -m unittest discover -s tests -v
"""
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest

SCRIPTS = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "hooks", "scripts"))
sys.path.insert(0, SCRIPTS)
import records  # noqa: E402

PREFIX = "【Agent 开发团队 拦截】"

FAKE_CODEX = r'''
import json, os, sys
here = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(here, "codex-calls.jsonl"), "a", encoding="utf-8") as f:
    f.write(json.dumps({"cwd": os.getcwd(), "args": sys.argv[1:]}, ensure_ascii=False) + "\n")
with open(os.path.join(here, "verdict.txt"), encoding="utf-8") as f:
    print(f.read())
'''


class LeadFlow(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="adt-e2e-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith("AGENT_DEV_TEAM") and not k.startswith("GIT_")}
        self.env.update(
            AGENT_DEV_TEAM_HOME=os.path.join(self.tmp, "state"), HOME=os.path.join(self.tmp, "home"),
            GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1",
            GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.com",
            GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.com",
        )
        os.makedirs(self.env["HOME"])
        # 假的审核命令：把调用记下来，打印 verdict.txt 的内容
        tools = os.path.join(self.tmp, "tools")
        os.makedirs(tools)
        self.fake = os.path.join(tools, "fake_codex.py")
        with open(self.fake, "w", encoding="utf-8") as f:
            f.write(FAKE_CODEX)
        self.verdict_file = os.path.join(tools, "verdict.txt")
        self.calls_file = os.path.join(tools, "codex-calls.jsonl")
        self.env["AGENT_DEV_TEAM_CODEX_CMD"] = f"{shlex.quote(sys.executable)} {shlex.quote(self.fake)}"

        # 主仓库 + 远端，main 已推上去
        self.repo = os.path.join(self.tmp, "proj")
        os.makedirs(self.repo)
        self.git(self.repo, "init", "-q", "-b", "main")
        self.write(self.repo, "app.py", "v1\n")
        self.git(self.repo, "add", "-A")
        self.git(self.repo, "commit", "-q", "-m", "init")
        bare = os.path.join(self.tmp, "origin.git")
        self.git(self.tmp, "init", "-q", "--bare", "-b", "main", bare)
        self.git(self.repo, "remote", "add", "origin", bare)
        self.git(self.repo, "push", "-q", "-u", "origin", "main")
        self.git(self.repo, "remote", "set-head", "origin", "main")

    # ----- 工具 -----

    def git(self, cwd, *args):
        r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, env=self.env)
        self.assertEqual(r.returncode, 0, f"git {' '.join(args)}: {r.stderr}")
        return r.stdout.strip()

    def write(self, cwd, rel, text):
        p = os.path.join(cwd, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)

    def task_branch(self, branch, n=2):
        """像子代理一样：建独立工作副本，在任务分支上提交 n 次。返回工作副本路径。"""
        wt = os.path.join(self.tmp, "wt-" + branch)
        self.git(self.repo, "worktree", "add", "-q", "-b", branch, wt)
        for i in range(n):
            self.write(wt, f"{branch}/f{i}.py", f"{i}\n")
            self.git(wt, "add", "-A")
            self.git(wt, "commit", "-q", "-m", f"{branch} {i}")
        return wt

    def guard(self, command, sid="lead-1"):
        """按 Claude Code 的 PreToolUse 钩子输入格式喂给守卫。"""
        data = {
            "session_id": sid,
            "transcript_path": os.path.join(self.tmp, "t.jsonl"),
            "cwd": self.repo,
            "permission_mode": "default",
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": command, "description": "x"},
            "tool_use_id": "toolu_1",
        }
        r = subprocess.run([sys.executable, os.path.join(SCRIPTS, "guard.py")], input=json.dumps(data),
                           capture_output=True, text=True, env=self.env, cwd=self.repo, timeout=30)
        return r.returncode, r.stderr

    def assert_blocked(self, command):
        code, err = self.guard(command)
        self.assertEqual(code, 2, f"{command} 应该被拦：{err}")
        self.assertTrue(err.startswith(PREFIX), err)
        self.assertIn("还没通过 Codex 审核", err)
        return err

    def assert_allowed(self, command):
        self.assertEqual(self.guard(command), (0, ""), command)

    def review_line(self, err):
        """拦截提示里给出的审核命令（完整绝对路径）。"""
        lines = [ln for ln in err.splitlines() if ln.startswith("python3 ")]
        self.assertEqual(len(lines), 1, err)
        args = shlex.split(lines[0])
        self.assertTrue(os.path.isabs(args[1]), lines[0])
        self.assertEqual(args[1], os.path.join(SCRIPTS, "review.py"))
        return args[1:]

    def run_review(self, args, verdict):
        with open(self.verdict_file, "w", encoding="utf-8") as f:
            f.write(verdict)
        r = subprocess.run([sys.executable, *args], capture_output=True, text=True, env=self.env,
                           cwd=self.repo, timeout=60)
        return r.returncode, r.stdout

    # ----- 流程 -----

    def test_没审核拦_审核通过放行_推送同样查(self):
        self.task_branch("feat")
        merge = "git merge --no-ff feat -m 'merge feat'"

        # 1. 没审核的任务分支：合并被拦，提示里给出审核命令
        err = self.assert_blocked(merge)
        self.assertIn("有 2 个提交", err)
        args = self.review_line(err)
        self.assertEqual(args[1:5], ["--branch", "feat", "--base", "main"])

        # 2. 照提示跑审核，假 Codex 说通过
        code, out = self.run_review(args, "没有严重问题。\nVERDICT: PASS\n")
        self.assertEqual((code, out.splitlines()[0]), (0, "审核：通过"), out)
        with open(self.calls_file, encoding="utf-8") as f:
            call = json.loads(f.readline())
        main_sha = self.git(self.repo, "rev-parse", "main")
        feat_sha = self.git(self.repo, "rev-parse", "feat")
        self.assertIn(f"git diff {main_sha}...{feat_sha}", call["args"][-1])  # 提示词是最后一个参数，用固定提交号
        self.assertIn("VERDICT: PASS", call["args"][-1])

        # 3. 同一条合并命令放行，真的合并
        self.assert_allowed(merge)
        self.git(self.repo, "merge", "-q", "--no-ff", "feat", "-m", "merge feat")

        # 4. 推送：要上传的都是审核过的提交（合并提交不用审）→ 放行
        self.assert_allowed("git push")

        # 5. 在主分支上直接加一个没审核的提交 → 推送被拦
        self.write(self.repo, "app.py", "v2\n")
        self.git(self.repo, "commit", "-q", "-am", "直接改")
        err = self.assert_blocked("git push")
        self.assertIn("有 1 个提交", err)
        self.assert_blocked("git push origin main")
        args = self.review_line(err)
        self.assertEqual(args[1:5], ["--branch", "main", "--base", "origin/main"])

        # 6. 审核过之后放行
        code, out = self.run_review(args, "VERDICT: PASS")
        self.assertEqual(code, 0, out)
        self.assert_allowed("git push")
        self.git(self.repo, "push", "-q")
        self.assert_allowed("git push")  # 推完没有要上传的提交

    def test_审核不通过_计数加一_仍然被拦(self):
        self.task_branch("feat2", n=1)
        merge = "git merge feat2"
        args = self.review_line(self.assert_blocked(merge))

        code, out = self.run_review(args, "严重：a.py 第 3 行会崩。\nVERDICT: FAIL\n")
        self.assertEqual(code, 1, out)
        self.assertEqual(out.splitlines()[0], "审核：不通过（第 1 轮）")
        self.assertIn("a.py 第 3 行会崩", out)
        self.assertEqual(records.fails(self.repo, "feat2"), 1)
        self.assert_blocked(merge)

        code, out = self.run_review(args, "VERDICT: FAIL")
        self.assertEqual(code, 1, out)
        self.assertEqual(out.splitlines()[0], "审核：不通过（第 2 轮）")
        self.assertIn("同一任务已 2 轮不过", out)
        self.assertEqual(records.fails(self.repo, "feat2"), 2)
        self.assert_blocked(merge)

        # 没给结论按不通过；审核命令出错不计入轮数
        code, out = self.run_review(args, "我看完了")
        self.assertEqual((code, out.splitlines()[0]), (1, "审核：不通过（第 3 轮）"), out)
        env_bad = dict(self.env, AGENT_DEV_TEAM_CODEX_CMD="不存在的审核命令-xyz")
        r = subprocess.run([sys.executable, *args], capture_output=True, text=True, env=env_bad, cwd=self.repo)
        self.assertEqual(r.returncode, 2, r.stdout)
        self.assertTrue(r.stdout.startswith("审核：出错（不计入失败轮数）"), r.stdout)
        self.assertEqual(records.fails(self.repo, "feat2"), 3)
        self.assert_blocked(merge)

        # 最后通过：计数清零，放行
        code, out = self.run_review(args, "VERDICT: PASS")
        self.assertEqual(code, 0, out)
        self.assertEqual(records.fails(self.repo, "feat2"), 0)
        self.assert_allowed(merge)

    def test_子代理改基准后重审_旧的通过记录不算新提交(self):
        """合并冲突时子代理用 git rebase 解决，提交号变了，要重新审核才能合并。"""
        wt = self.task_branch("feat3", n=1)
        args = self.review_line(self.assert_blocked("git merge feat3"))
        self.assertEqual(self.run_review(args, "VERDICT: PASS")[0], 0)
        self.assert_allowed("git merge feat3")
        # 主分支前进（审核过的提交），子代理在任务分支上 rebase
        self.write(self.repo, "app.py", "v3\n")
        self.git(self.repo, "commit", "-q", "-am", "主分支前进")
        records.add_passed(self.repo, [self.git(self.repo, "rev-parse", "HEAD")])
        self.git(wt, "rebase", "-q", "main")
        self.assert_blocked("git merge feat3")
        self.assertEqual(self.run_review(args, "VERDICT: PASS")[0], 0)
        self.assert_allowed("git merge feat3")


if __name__ == "__main__":
    unittest.main()
