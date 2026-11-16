#!/usr/bin/env python3
"""验收、审核、上线检查、回滚四个脚本的测试。全部用临时 git 仓库，不联网、不调真实的 Codex 和 gh。
运行：python3 -m unittest discover -s tests -v
"""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest

SCRIPTS = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "hooks", "scripts"))
sys.path.insert(0, SCRIPTS)
import deploy_check  # noqa: E402
import records  # noqa: E402


def sh(cwd, *args):
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 失败：{r.stderr}")
    return r.stdout.strip()


class RepoCase(unittest.TestCase):
    """每个测试一个临时仓库：main 上一个提交，feat 分支上改 src/a.py 和 docs/x.md。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = os.path.join(self.tmp.name, "repo")
        os.makedirs(self.repo)
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith("AGENT_DEV_TEAM") and not k.startswith("GIT_")}
        self.env["AGENT_DEV_TEAM_HOME"] = os.path.join(self.tmp.name, "home")
        sh(self.repo, "init", "-q", "-b", "main")
        sh(self.repo, "config", "user.name", "t")
        sh(self.repo, "config", "user.email", "t@t")
        sh(self.repo, "config", "commit.gpgsign", "false")
        self.write("src/a.py", "v1\n")
        self.commit("init")
        sh(self.repo, "checkout", "-q", "-b", "feat")
        self.write("src/a.py", "v2\n")
        self.commit("feat 1")
        self.write("docs/x.md", "doc\n")
        self.commit("feat 2")
        sh(self.repo, "checkout", "-q", "main")

    def write(self, rel, text):
        p = os.path.join(self.repo, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write(text)

    def read(self, rel):
        with open(os.path.join(self.repo, rel)) as f:
            return f.read()

    def commit(self, msg):
        sh(self.repo, "add", "-A")
        sh(self.repo, "commit", "-q", "-m", msg)
        return sh(self.repo, "rev-parse", "HEAD")

    def run_script(self, name, *args, env=None):
        r = subprocess.run([sys.executable, os.path.join(SCRIPTS, name), *args], cwd=self.repo,
                           capture_output=True, text=True, env=env or self.env, timeout=60)
        return r.returncode, r.stdout + r.stderr


class Verify(RepoCase):
    def v(self, *args):
        return self.run_script("verify.py", "--branch", "feat", "--cwd", self.repo, *args)

    def test_范围内通过(self):
        code, out = self.v("--allow", "src/*", "--allow", "docs/*", "--cmd", "true")
        self.assertEqual(code, 0, out)
        self.assertEqual(out.splitlines()[0], "验收：通过")
        self.assertIn("改了 2 个文件", out)

    def test_有越界文件不通过并列出(self):
        code, out = self.v("--allow", "src/*")
        self.assertEqual(code, 1, out)
        self.assertEqual(out.splitlines()[0], "验收：不通过")
        self.assertIn("docs/x.md", out)
        self.assertNotIn("src/a.py", out)

    def test_没给allow就不查范围(self):
        code, out = self.v()
        self.assertEqual(code, 0, out)

    def test_命令失败带输出尾部(self):
        cmd = "for i in $(seq 1 50); do echo line$i; done; exit 3"
        code, out = self.v("--cmd", "true", "--cmd", cmd)
        self.assertEqual(code, 1, out)
        self.assertEqual(out.splitlines()[0], "验收：不通过")
        self.assertIn("退出码 3", out)
        self.assertIn("line50", out)
        self.assertIn("line21", out)
        self.assertNotIn("line20\n", out)

    def test_命令超时不通过(self):
        code, out = self.v("--timeout", "1", "--cmd", "sleep 20")
        self.assertEqual(code, 1, out)
        self.assertIn("超时", out)

    def test_命令在cwd里跑(self):
        code, out = self.v("--cmd", "test -f src/a.py")
        self.assertEqual(code, 0, out)

    def test_参数或git出错退出2(self):
        code, out = self.run_script("verify.py", "--branch", "nope", "--cwd", self.repo)
        self.assertEqual(code, 2, out)
        code, out = self.run_script("verify.py", "--branch", "feat", "--cwd", self.tmp.name)
        self.assertEqual(code, 2, out)
        code, _ = self.run_script("verify.py")  # 少了 --branch
        self.assertEqual(code, 2)


FAKE = """import sys
sys.stdout.write({out!r})
"""


class Review(RepoCase):
    def fake(self, name, output):
        p = os.path.join(self.tmp.name, name + ".py")
        with open(p, "w") as f:
            f.write(FAKE.format(out=output))
        return f"{sys.executable} {p}"

    def r(self, codex_cmd, branch="feat"):
        env = dict(self.env, AGENT_DEV_TEAM_CODEX_CMD=codex_cmd)
        return self.run_script("review.py", "--branch", branch, "--cwd", self.repo, env=env)

    def test_通过记下全部提交并清掉失败计数(self):
        records.add_fail(self.repo, "feat")
        code, out = self.r(self.fake("pass", "没问题\nVERDICT: PASS\n"))
        self.assertEqual(code, 0, out)
        self.assertEqual(out.splitlines()[0], "审核：通过")
        shas = set(sh(self.repo, "rev-list", "main..feat").split())
        self.assertEqual(len(shas), 2)
        self.assertTrue(shas <= records.passed_commits(self.repo))
        self.assertEqual(records.fails(self.repo, "feat"), 0)

    def test_不通过计数加1_第2轮提示读diff(self):
        cmd = self.fake("fail", "严重：src/a.py:1 出错\nVERDICT: FAIL\n")
        code, out = self.r(cmd)
        self.assertEqual(code, 1, out)
        self.assertEqual(out.splitlines()[0], "审核：不通过（第 1 轮）")
        self.assertIn("src/a.py:1", out)
        self.assertNotIn("先自己读一次 diff", out)
        code, out = self.r(cmd)
        self.assertEqual(code, 1, out)
        self.assertEqual(out.splitlines()[0], "审核：不通过（第 2 轮）")
        self.assertIn("负责人先自己读一次 diff", out)
        self.assertEqual(records.fails(self.repo, "feat"), 2)
        self.assertEqual(records.passed_commits(self.repo), set())

    def test_取最后一个结论(self):
        code, out = self.r(self.fake("both", "VERDICT: PASS\n后来又发现问题\nVERDICT: FAIL\n"))
        self.assertEqual(code, 1, out)

    def test_没有结论按不通过(self):
        code, out = self.r(self.fake("none", "我看完了\n"))
        self.assertEqual(code, 1, out)
        self.assertIn("没有给出结论", out)
        self.assertEqual(records.fails(self.repo, "feat"), 1)

    def test_没有改动退出0(self):
        code, out = self.r(self.fake("pass", "VERDICT: PASS\n"), branch="main")
        self.assertEqual(code, 0, out)
        self.assertEqual(out.strip(), "审核：没有改动")

    def test_找不到命令退出2且不计数(self):
        code, out = self.r("/no/such/codex-cmd exec")
        self.assertEqual(code, 2, out)
        self.assertIn("找不到审核命令", out)
        self.assertEqual(records.fails(self.repo, "feat"), 0)

    def test_项目配置codex_cmd优先于环境变量(self):
        self.write(".claude/agent-dev-team.json",
                   json.dumps({"codex_cmd": self.fake("pass", "VERDICT: PASS\n")}))
        code, out = self.r(self.fake("fail", "VERDICT: FAIL\n"))
        self.assertEqual(code, 0, out)

    def test_在工作副本里跑_记录所有工作副本共用(self):
        wt = os.path.join(self.tmp.name, "wt")
        sh(self.repo, "worktree", "add", "-q", wt, "feat")
        env = dict(self.env, AGENT_DEV_TEAM_CODEX_CMD=self.fake("pass", "VERDICT: PASS\n"))
        code, out = self.run_script("review.py", "--branch", "feat", "--cwd", wt, env=env)
        self.assertEqual(code, 0, out)
        self.assertIn(sh(self.repo, "rev-parse", "feat"), records.passed_commits(self.repo))


class DeployCheckCmd(RepoCase):
    def config(self, cmd, retries=2):
        self.write(".claude/agent-dev-team.json", json.dumps(
            {"deploy_check": {"cmd": cmd, "wait_seconds": 999, "retries": retries, "interval_seconds": 999}}))

    def d(self, *args):
        return self.run_script("deploy_check.py", "--cwd", self.repo, "--wait", "0", "--interval", "0", *args)

    def test_命令成功(self):
        self.config("true")
        code, out = self.d()
        self.assertEqual(code, 0, out)
        self.assertEqual(out.splitlines()[0], "上线检查：通过")

    def test_重试后成功(self):
        counter = os.path.join(self.tmp.name, "n")
        # 第 3 次才成功
        self.config(f'n=$(cat {counter} 2>/dev/null || echo 0); n=$((n+1)); echo $n > {counter}; [ $n -ge 3 ]')
        code, out = self.d()
        self.assertEqual(code, 0, out)
        self.assertIn("第 3 次", out)

    def test_一直失败(self):
        self.config("echo 服务挂了; exit 1", retries=1)
        code, out = self.d()
        self.assertEqual(code, 1, out)
        self.assertEqual(out.splitlines()[0], "上线检查：不通过")
        self.assertIn("2 次", out)
        self.assertIn("服务挂了", out)

    def test_配置里的数字写错了_用默认值_不报错(self):
        self.write(".claude/agent-dev-team.json", json.dumps(
            {"deploy_check": {"cmd": "exit 1", "wait_seconds": "很久", "retries": "x", "interval_seconds": -5}}))
        self.assertEqual(deploy_check._num("很久", 60, float), 60)
        self.assertEqual(deploy_check._num(-5, 30, float), 30)
        self.assertEqual(deploy_check._num("2", 3, int), 2)
        # 等待和间隔用参数覆盖成 0；重试次数用默认的 3 次，一共跑 4 次
        code, out = self.d()
        self.assertEqual(code, 1, out)
        self.assertIn("试了 4 次", out)

    def test_什么都没配_远端不是GitHub_无法判断(self):
        sh(self.repo, "remote", "add", "origin", "https://gitlab.com/a/b.git")
        code, out = self.d()
        self.assertEqual(code, 3, out)
        self.assertEqual(out.splitlines()[0], "上线检查：无法判断")


class DeployCheckGitHub(RepoCase):
    def setUp(self):
        super().setUp()
        sh(self.repo, "remote", "add", "origin", "git@github.com:own/proj.git")
        self.sha = sh(self.repo, "rev-parse", "HEAD")
        self.calls = []
        orig = (deploy_check.have_gh, deploy_check.gh_api)
        self.addCleanup(lambda: setattr(deploy_check, "have_gh", orig[0]))
        self.addCleanup(lambda: setattr(deploy_check, "gh_api", orig[1]))
        deploy_check.have_gh = lambda: True

    def fake_api(self, runs, deps, statuses):
        def api(cwd, path):
            self.calls.append(path)
            self.assertIn("own/proj", path)
            if "/check-runs" in path:
                return runs
            if "/statuses" in path:
                return statuses
            if "/deployments" in path:
                return deps
            return None
        deploy_check.gh_api = api

    def d(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = deploy_check.main(["--cwd", self.repo, "--interval", "0", "--max-poll", "0"])
        return code, buf.getvalue()

    def test_全部成功(self):
        self.fake_api({"check_runs": [{"name": "ci", "status": "completed", "conclusion": "success"}]},
                      [{"id": 7, "environment": "production"}], [{"state": "success"}])
        code, out = self.d()
        self.assertEqual(code, 0, out)
        self.assertEqual(out.splitlines()[0], "上线检查：通过")
        self.assertTrue(any(self.sha in c for c in self.calls))

    def test_有失败(self):
        self.fake_api({"check_runs": [{"name": "ci", "status": "completed", "conclusion": "success"}]},
                      [{"id": 7, "environment": "production"}], [{"state": "failure"}])
        code, out = self.d()
        self.assertEqual(code, 1, out)
        self.assertEqual(out.splitlines()[0], "上线检查：不通过")
        self.assertIn("production", out)

    def test_检查失败(self):
        self.fake_api({"check_runs": [{"name": "test", "status": "completed", "conclusion": "failure"}]},
                      [], [])
        code, out = self.d()
        self.assertEqual(code, 1, out)
        self.assertIn("test", out)

    def test_什么都没有(self):
        self.fake_api({"check_runs": []}, [], [])
        code, out = self.d()
        self.assertEqual(code, 3, out)
        self.assertEqual(out.splitlines()[0], "上线检查：无法判断")

    def test_一直在跑超时无法判断(self):
        self.fake_api({"check_runs": [{"name": "ci", "status": "in_progress", "conclusion": None}]}, [], [])
        code, out = self.d()
        self.assertEqual(code, 3, out)
        self.assertIn("还没跑完", out)

    def test_没有gh时无法判断(self):
        deploy_check.have_gh = lambda: False
        self.fake_api(None, None, None)
        code, out = self.d()
        self.assertEqual(code, 3, out)
        self.assertEqual(self.calls, [])


class Rollback(RepoCase):
    def rb(self, *args):
        return self.run_script("rollback.py", "--cwd", self.repo, *args)

    def test_回滚普通提交_内容回到之前_新提交进通过记录(self):
        bad = self.commit_change("src/a.py", "bad\n")
        code, out = self.rb("--commits", bad)
        self.assertEqual(code, 0, out)
        self.assertEqual(out.splitlines()[0], "回滚：完成")
        self.assertEqual(self.read("src/a.py"), "v1\n")
        new = sh(self.repo, "rev-parse", "HEAD")
        self.assertNotEqual(new, bad)
        self.assertIn(new, records.passed_commits(self.repo))
        self.assertIn(new[:12], out)

    def commit_change(self, rel, text):
        self.write(rel, text)
        return self.commit("change " + rel)

    def test_回滚合并提交(self):
        sh(self.repo, "merge", "-q", "--no-ff", "--no-edit", "feat")
        merge = sh(self.repo, "rev-parse", "HEAD")
        self.assertEqual(self.read("src/a.py"), "v2\n")
        code, out = self.rb("--commits", merge)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.read("src/a.py"), "v1\n")
        self.assertFalse(os.path.exists(os.path.join(self.repo, "docs/x.md")))

    def test_按顺序回滚多个(self):
        c1 = self.commit_change("src/a.py", "v3\n")
        c2 = self.commit_change("src/a.py", "v4\n")
        code, out = self.rb("--commits", c2, c1)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.read("src/a.py"), "v1\n")
        self.assertEqual(len(out.splitlines()[1].split()), 2)

    def test_提交号不存在失败(self):
        head = sh(self.repo, "rev-parse", "HEAD")
        code, out = self.rb("--commits", "deadbeef1234")
        self.assertEqual(code, 1, out)
        self.assertEqual(out.splitlines()[0], "回滚：失败")
        self.assertIn("deadbeef1234", out)
        self.assertEqual(sh(self.repo, "rev-parse", "HEAD"), head)

    def test_push到远端(self):
        bare = os.path.join(self.tmp.name, "remote.git")
        subprocess.run(["git", "init", "-q", "--bare", bare], check=True)
        sh(self.repo, "remote", "add", "origin", bare)
        bad = self.commit_change("src/a.py", "bad\n")
        sh(self.repo, "push", "-q", "-u", "origin", "main")
        code, out = self.rb("--commits", bad, "--push")
        self.assertEqual(code, 0, out)
        self.assertEqual(sh(bare, "rev-parse", "main"), sh(self.repo, "rev-parse", "HEAD"))

    def test_push失败(self):
        bad = self.commit_change("src/a.py", "bad\n")
        code, out = self.rb("--commits", bad, "--push")  # 没有远端
        self.assertEqual(code, 1, out)
        self.assertEqual(out.splitlines()[0], "回滚：失败")
        self.assertIn("push", out)


class Help(unittest.TestCase):
    def test_四个脚本都有帮助(self):
        for name in ("verify.py", "review.py", "deploy_check.py", "rollback.py"):
            r = subprocess.run([sys.executable, os.path.join(SCRIPTS, name), "--help"],
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, name)
            self.assertIn("usage", r.stdout, name)


if __name__ == "__main__":
    unittest.main(verbosity=2)
