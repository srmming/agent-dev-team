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

    def units(self):
        """审核记录里的单元：[(顶端提交, 这一段的提交集合)]。"""
        return [(u["tip"], u["commits"]) for u in records.units(self.repo)]

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

    def test_把范围外的文件改名挪进范围_也算越界(self):
        sh(self.repo, "checkout", "-q", "-b", "mv", "main")
        os.makedirs(os.path.join(self.repo, "docs"), exist_ok=True)
        sh(self.repo, "mv", "src/a.py", "docs/a.py")
        self.commit("挪")
        sh(self.repo, "checkout", "-q", "main")
        code, out = self.run_script("verify.py", "--branch", "mv", "--cwd", self.repo, "--allow", "docs/*")
        self.assertEqual(code, 1, out)
        self.assertIn("src/a.py", out)

    def test_中文文件名照常匹配(self):
        sh(self.repo, "checkout", "-q", "-b", "zh", "main")
        self.write("docs/说明.md", "x\n")
        self.commit("中文")
        sh(self.repo, "checkout", "-q", "main")
        code, out = self.run_script("verify.py", "--branch", "zh", "--cwd", self.repo, "--allow", "docs/*")
        self.assertEqual(code, 0, out)

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
        # 记成一个审核单元：顶端是分支顶端，里面是这一段的全部提交
        self.assertEqual(self.units(), [(sh(self.repo, "rev-parse", "feat"), shas)])
        self.assertEqual(records.units(self.repo)[0]["fork"], sh(self.repo, "rev-parse", "main"))  # 起点是分叉点
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
        self.assertEqual(self.units(), [])

    def test_取最后一个结论(self):
        code, out = self.r(self.fake("both", "VERDICT: PASS\n后来又发现问题\nVERDICT: FAIL\n"))
        self.assertEqual(code, 1, out)

    def test_没有结论按不通过(self):
        code, out = self.r(self.fake("none", "我看完了\n"))
        self.assertEqual(code, 1, out)
        self.assertIn("没有给出结论", out)
        self.assertEqual(records.fails(self.repo, "feat"), 1)

    def script(self, name, code):
        p = os.path.join(self.tmp.name, name + ".py")
        with open(p, "w") as f:
            f.write(code)
        return f"{sys.executable} {p}"

    def test_审核期间分支上新加的提交不算通过(self):
        before = set(sh(self.repo, "rev-list", "main..feat").split())
        main_sha, feat_sha = sh(self.repo, "rev-parse", "main"), sh(self.repo, "rev-parse", "feat")
        log = os.path.join(self.tmp.name, "prompt.txt")
        # 假 Codex：审核途中子代理往 feat 上又加了一个提交，然后说通过
        cmd = self.script("sneak", f"""import subprocess, sys
open({log!r}, "w").write(sys.argv[-1])
g = lambda *a: subprocess.run(["git", *a], capture_output=True, text=True, check=True).stdout.strip()
tree = g("rev-parse", "feat^{{tree}}")
new = g("-c", "user.name=t", "-c", "user.email=t@t", "commit-tree", tree, "-p", "feat", "-m", "偷偷加的")
g("update-ref", "refs/heads/feat", new)
print("VERDICT: PASS")
""")
        code, out = self.r(cmd)
        self.assertEqual(code, 0, out)
        new = sh(self.repo, "rev-parse", "feat")
        self.assertNotEqual(new, feat_sha)
        # 单元的顶端是开跑前的分支顶端，新加的提交不在里面
        self.assertEqual(self.units(), [(feat_sha, before)])
        with open(log) as f:
            self.assertIn(f"git diff {main_sha}..{feat_sha}", f.read())

    def test_有提交但合起来没改文件_记为通过(self):
        sh(self.repo, "checkout", "-q", "-b", "empty")
        sh(self.repo, "commit", "-q", "--allow-empty", "-m", "空提交")
        self.write("src/a.py", "临时\n")
        self.commit("改")
        self.write("src/a.py", "v1\n")
        self.commit("改回来")
        sh(self.repo, "checkout", "-q", "main")
        code, out = self.r("/no/such/codex", branch="empty")  # 根本不用跑审核命令
        self.assertEqual(code, 0, out)
        self.assertEqual(out.splitlines()[0], "审核：没有改动")
        self.assertIn("3 个提交", out)
        self.assertIn("整段一起上传", out)
        # 三个提交记成一个单元，顶端是最后那个「改回来」；只带其中一部分上传，守卫照样拦（见 test_guard）
        self.assertEqual(self.units(), [(sh(self.repo, "rev-parse", "empty"),
                                         set(sh(self.repo, "rev-list", "main..empty").split()))])

    def test_只改了文件权限也算有改动_要过审核(self):
        sh(self.repo, "checkout", "-q", "-b", "mode")
        os.chmod(os.path.join(self.repo, "src/a.py"), 0o755)
        self.commit("加可执行权限")
        sh(self.repo, "checkout", "-q", "main")
        code, out = self.r(self.fake("fail", "VERDICT: FAIL\n"), branch="mode")
        self.assertEqual(code, 1, out)  # 没有被当成「没有改动」直接放过
        self.assertEqual(self.units(), [])

    def test_仓库配置改不了有没有改动的判断(self):
        sh(self.repo, "config", "diff.external", "true")  # 让 git diff 什么都不输出
        sh(self.repo, "config", "diff.ignoreSubmodules", "all")
        code, out = self.r(self.fake("fail", "VERDICT: FAIL\n"))
        self.assertEqual(code, 1, out)
        self.assertEqual(self.units(), [])

    def test_审核命令退出码非0_算出错_不计数也不通过(self):
        code, out = self.r(self.script("crash", "import sys\nprint('VERDICT: PASS')\nsys.exit(3)\n"))
        self.assertEqual(code, 2, out)
        self.assertTrue(out.startswith("审核：出错（不计入失败轮数）"), out)
        self.assertEqual(records.fails(self.repo, "feat"), 0)
        self.assertEqual(self.units(), [])

    def test_结论要单独成行_引用提示词那句不算(self):
        code, out = self.r(self.fake("echo", "最后单独一行输出结论：VERDICT: PASS 或 VERDICT: PASS\n"))
        self.assertEqual(code, 1, out)
        self.assertIn("没有给出结论", out)

    def test_结论必须整行精确匹配(self):
        # Codex 第 2 轮：先说不通过，后面引用了一句示例，以前会被当成通过
        code, out = self.r(self.fake("quote", "严重：有问题\nVERDICT: FAIL\n\n格式说明：\n> VERDICT: PASS 或 VERDICT: FAIL\n"))
        self.assertEqual(code, 1, out)
        self.assertEqual(out.splitlines()[0], "审核：不通过（第 1 轮）")
        self.assertNotIn("没有给出结论", out)
        self.assertEqual(self.units(), [])
        # 行里多了别的字都不算结论
        for i, text in enumerate(("**VERDICT: PASS**\n", "> VERDICT: PASS\n", "VERDICT: PASS。\n", "VERDICT: PASS 或 VERDICT: FAIL\n",
                                  "结论 VERDICT: PASS\n", "VERDICT: PASSED\n", "`VERDICT: PASS`\n", "VERDICT:PASS\n", "verdict: pass\n")):
            code, out = self.r(self.fake(f"odd{i}", "没问题\n" + text))
            self.assertEqual(code, 1, text)
            self.assertIn("没有给出结论", out, text)
        self.assertEqual(self.units(), [])
        # 首尾空白可以有；取最后一个整行结论
        code, out = self.r(self.fake("space", "VERDICT: FAIL\n后来确认没问题\n  VERDICT: PASS \t\r\n"))
        self.assertEqual(code, 0, out)
        self.assertEqual(out.splitlines()[0], "审核：通过")

    def test_结论只按换行切行_换页符这类不算换行(self):
        import review
        self.assertEqual(review.verdicts_in("VERDICT: FAIL\n说明\x0cVERDICT: PASS\x0c别的\n"), ["FAIL"])
        self.assertEqual(review.verdicts_in("引用\u2028VERDICT: PASS\u2028\nVERDICT: FAIL"), ["FAIL"])
        self.assertEqual(review.verdicts_in("VERDICT: FAIL\r\n\tVERDICT: PASS  \r\n"), ["FAIL", "PASS"])

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
        self.assertEqual([tip for tip, _ in self.units()], [sh(self.repo, "rev-parse", "feat")])

    def test_旧格式的passed文件作废_不读也不出错(self):
        old = os.path.join(sh(self.repo, "rev-parse", "--git-common-dir"), "agent-dev-team", "passed")
        old = old if os.path.isabs(old) else os.path.join(self.repo, old)
        os.makedirs(os.path.dirname(old), exist_ok=True)
        with open(old, "w") as f:
            f.write("\n".join(sh(self.repo, "rev-list", "main..feat").split()) + "\n不是提交号\n")
        self.assertEqual(self.units(), [])  # 旧记录里的提交不算通过
        self.assertTrue(records.has_legacy(self.repo))
        code, out = self.r(self.fake("pass", "VERDICT: PASS\n"))
        self.assertEqual(code, 0, out)
        self.assertEqual(len(self.units()), 1)

    def test_记录文件里读不懂的行跳过_不算通过(self):
        feat, main = sh(self.repo, "rev-parse", "feat"), sh(self.repo, "rev-parse", "main")
        records.add_unit(self.repo, feat, main, [feat])
        path = os.path.join(os.path.dirname(records._path(self.repo, "x")), records.UNITS_FILE)
        with open(path, "a") as f:
            f.write("不是 JSON\n[1, 2]\n{}\n")
            f.write(json.dumps({"tip": feat, "fork": main, "commits": []}) + "\n")              # 顶端不在提交里
            f.write(json.dumps({"tip": feat, "fork": main, "commits": [feat, "main"]}) + "\n")  # 不是提交号
            f.write(json.dumps({"tip": feat, "commits": [feat]}) + "\n")                        # 没有起点
            f.write(json.dumps({"tip": feat, "fork": "main", "commits": [feat]}) + "\n")        # 起点不是提交号
            f.write(json.dumps({"tip": 1, "fork": main, "commits": "x"}) + "\n")
        self.assertEqual(self.units(), [(feat, {feat})])
        self.assertEqual(records.units(self.repo)[0]["fork"], main)
        with self.assertRaises(ValueError):
            records.add_unit(self.repo, feat, main, [])  # 单元里必须有顶端提交
        with self.assertRaises(ValueError):
            records.add_unit(self.repo, feat, "", [feat])  # 必须有起点


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

    def test_命令写成列表_无法判断(self):
        self.write(".claude/agent-dev-team.json", json.dumps({"deploy_check": {"cmd": ["true"]}}))
        code, out = self.d()
        self.assertEqual(code, 3, out)
        self.assertEqual(out.splitlines()[0], "上线检查：无法判断")

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

    def fake_by_id(self, runs, deps, statuses):
        """statuses 按部署 id 给。"""
        def api(cwd, path):
            self.calls.append(path)
            if "/check-runs" in path:
                return runs
            if "/statuses" in path:
                return statuses.get(int(path.split("/deployments/")[1].split("/")[0]))
            if "/deployments" in path:
                return deps
            return None
        deploy_check.gh_api = api

    OK_RUNS = {"total_count": 1, "check_runs": [{"id": 1, "name": "ci", "status": "completed", "conclusion": "success"}]}

    def test_同一环境重新部署成功_只看最新一次(self):
        deps = [{"id": 8, "environment": "production", "created_at": "2026-11-16T10:05:00Z"},
                {"id": 7, "environment": "production", "created_at": "2026-11-16T10:00:00Z"},
                {"id": 5, "environment": "staging", "created_at": "2026-11-16T09:00:00Z"}]
        self.fake_by_id(self.OK_RUNS, deps, {8: [{"state": "success"}], 7: [{"state": "failure"}],
                                              5: [{"state": "success"}]})
        code, out = self.d()
        self.assertEqual(code, 0, out)
        self.assertFalse(any("/deployments/7/" in c for c in self.calls))
        # 反过来：最新一次失败就是失败
        self.fake_by_id(self.OK_RUNS, deps, {8: [{"state": "error"}], 7: [{"state": "success"}],
                                              5: [{"state": "success"}]})
        code, out = self.d()
        self.assertEqual(code, 1, out)
        self.assertIn("production", out)

    def test_同名检查重跑成功_只看最新一次(self):
        runs = {"total_count": 2, "check_runs": [
            {"id": 2, "name": "ci", "status": "completed", "conclusion": "success", "started_at": "2026-11-16T10:05:00Z"},
            {"id": 1, "name": "ci", "status": "completed", "conclusion": "failure", "started_at": "2026-11-16T10:00:00Z"}]}
        self.fake_by_id(runs, [], {})
        code, out = self.d()
        self.assertEqual(code, 0, out)

    def test_任何一项查询失败都不报通过(self):
        for runs, deps, statuses in (
                (self.OK_RUNS, None, {}),                                     # 部署查询失败
                (None, [{"id": 7, "environment": "p"}], {7: [{"state": "success"}]}),  # 检查查询失败
                (self.OK_RUNS, [{"id": 7, "environment": "p"}], {}),          # 部署状态查询失败
                ({"total_count": 150, "check_runs": self.OK_RUNS["check_runs"]}, [], {})):  # 没查全
            self.fake_by_id(runs, deps, statuses)
            code, out = self.d()
            self.assertEqual(code, 3, out)
            self.assertEqual(out.splitlines()[0], "上线检查：无法判断")
        # 查到确定的失败时，部分查询失败也报不通过
        self.fake_by_id({"check_runs": [{"id": 1, "name": "t", "status": "completed", "conclusion": "failure"}]},
                        None, {})
        self.assertEqual(self.d()[0], 1)

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

    def test_回滚普通提交_内容回到之前_新提交记成审核单元(self):
        bad = self.commit_change("src/a.py", "bad\n")
        code, out = self.rb("--commits", bad)
        self.assertEqual(code, 0, out)
        self.assertEqual(out.splitlines()[0], "回滚：完成")
        self.assertEqual(self.read("src/a.py"), "v1\n")
        new = sh(self.repo, "rev-parse", "HEAD")
        self.assertNotEqual(new, bad)
        # 撤回之后和 bad 之前一模一样：记一个单元，起点是 bad 之前的那个提交
        self.assertEqual(self.units(), [(new, {bad, new})])
        self.assertEqual(records.units(self.repo)[0]["fork"], sh(self.repo, "rev-parse", bad + "~1"))
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
        # 记成一个单元：起点是 c1 之前，顶端是最后一个回滚提交（要整批一起推）
        head = sh(self.repo, "rev-parse", "HEAD")
        self.assertEqual(self.units(), [(head, {c1, c2, head, sh(self.repo, "rev-parse", "HEAD~1")})])
        self.assertEqual(records.units(self.repo)[0]["fork"], sh(self.repo, "rev-parse", c1 + "~1"))

    def test_只撤回中间的一段_不记进审核记录(self):
        c1 = self.commit_change("src/a.py", "v3\n")
        self.commit_change("docs/later.md", "后来的\n")
        code, out = self.rb("--commits", c1)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.read("src/a.py"), "v1\n")
        self.assertIn("没有记进审核记录", out)  # 撤回后的内容以前从没出现过，是个没人审过的新状态
        self.assertEqual(self.units(), [])

    def test_提交号不存在失败(self):
        head = sh(self.repo, "rev-parse", "HEAD")
        code, out = self.rb("--commits", "deadbeef1234")
        self.assertEqual(code, 1, out)
        self.assertEqual(out.splitlines()[0], "回滚：失败")
        self.assertIn("deadbeef1234", out)
        self.assertEqual(sh(self.repo, "rev-parse", "HEAD"), head)

    def git_path(self, name):
        return os.path.join(self.repo, sh(self.repo, "rev-parse", "--git-path", name))

    def conflict(self, *args):
        """跑一条会冲突的 git 命令，留下没做完的操作。"""
        r = subprocess.run(["git", *args], cwd=self.repo, capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0, args)

    def test_已有没做完的操作时不动手_不中止用户的操作(self):
        # Codex 第 2 轮：以前 revert 失败后无条件 git revert --abort，会把用户原来的 revert 中止掉
        c1 = self.commit_change("src/a.py", "v3\n")
        c2 = self.commit_change("src/a.py", "v4\n")
        other = self.commit_change("docs/other.md", "x\n")
        cases = (
            ("REVERT_HEAD", ("revert", "--no-edit", c1), ("revert", "--abort"), "revert"),
            ("CHERRY_PICK_HEAD", ("cherry-pick", "feat~1"), ("cherry-pick", "--abort"), "cherry-pick"),
            ("MERGE_HEAD", ("merge", "--no-edit", "feat"), ("merge", "--abort"), "merge"),
            ("rebase-merge", ("rebase", "feat"), ("rebase", "--abort"), "rebase"),
        )
        for marker, start, abort, word in cases:
            head = sh(self.repo, "rev-parse", "HEAD")
            self.conflict(*start)
            self.assertTrue(os.path.exists(self.git_path(marker)), marker)
            mid = sh(self.repo, "rev-parse", "HEAD")
            code, out = self.rb("--commits", other)
            self.assertEqual(code, 1, out)
            self.assertEqual(out.splitlines()[0], "回滚：失败")
            self.assertIn("没做完的 " + word, out)
            self.assertIn("没有动手", out)
            self.assertTrue(os.path.exists(self.git_path(marker)), marker + " 被中止了")  # 用户的操作还在
            self.assertEqual(sh(self.repo, "rev-parse", "HEAD"), mid)  # 没有新的回滚提交
            self.assertEqual(self.units(), [])
            sh(self.repo, *abort)
            self.assertEqual(sh(self.repo, "rev-parse", "HEAD"), head)
        self.assertNotEqual(c2, c1)

    def test_自己这次撤回冲突了_中止自己的_前面做好的留着(self):
        c1 = self.commit_change("src/a.py", "v3\n")
        self.commit_change("src/a.py", "v4\n")
        other = self.commit_change("docs/other.md", "x\n")
        code, out = self.rb("--commits", other, c1)  # 撤回 c1 会和后面的 v4 冲突
        self.assertEqual(code, 1, out)
        self.assertEqual(out.splitlines()[0], "回滚：失败")
        self.assertIn(c1[:12], out)
        self.assertIn("已经做好的回滚提交", out)
        self.assertIn("没有记进审核记录", out)
        self.assertEqual(self.units(), [])  # 只撤回了一部分，停在没审核过的中间状态，不记
        self.assertFalse(os.path.exists(self.git_path("REVERT_HEAD")))
        self.assertFalse(os.path.exists(self.git_path("sequencer")))
        self.assertEqual(sh(self.repo, "status", "--porcelain"), "")
        self.assertFalse(os.path.exists(os.path.join(self.repo, "docs/other.md")))  # 第一个撤回做好了
        self.assertEqual(self.read("src/a.py"), "v4\n")

    def test_提交不在当前分支的历史里_不撤回(self):
        # 撤回别的分支上的提交，等于往当前分支加一段没审核过的改动
        head = sh(self.repo, "rev-parse", "HEAD")
        code, out = self.rb("--commits", "feat")
        self.assertEqual(code, 1, out)
        self.assertEqual(out.splitlines()[0], "回滚：失败")
        self.assertIn("不在当前分支的历史里", out)
        self.assertEqual(sh(self.repo, "rev-parse", "HEAD"), head)
        self.assertEqual(self.units(), [])

    def test_push到远端(self):
        bare = os.path.join(self.tmp.name, "remote.git")
        subprocess.run(["git", "init", "-q", "--bare", bare], check=True)
        sh(self.repo, "remote", "add", "origin", bare)
        bad = self.commit_change("src/a.py", "bad\n")
        sh(self.repo, "push", "-q", "-u", "origin", "main")
        code, out = self.rb("--commits", bad, "--push")
        self.assertEqual(code, 0, out)
        self.assertEqual(sh(bare, "rev-parse", "main"), sh(self.repo, "rev-parse", "HEAD"))

    def test_push前有没审核的本地提交就不推(self):
        bare = os.path.join(self.tmp.name, "remote.git")
        subprocess.run(["git", "init", "-q", "--bare", bare], check=True)
        sh(self.repo, "remote", "add", "origin", bare)
        bad = self.commit_change("src/a.py", "bad\n")
        sh(self.repo, "push", "-q", "-u", "origin", "main")
        self.commit_change("docs/y.md", "没审核的\n")  # 本地多了一个没审核的提交
        code, out = self.rb("--commits", bad, "--push")
        self.assertEqual(code, 1, out)
        self.assertEqual(out.splitlines()[0], "回滚：失败")
        self.assertIn("没通过审核", out)
        self.assertEqual(sh(bare, "rev-parse", "main"), bad)  # 远端没动
        self.assertEqual(self.read("src/a.py"), "v1\n")  # 回滚提交留在本地
        # 关掉审核关时照常推
        code, out = self.run_script("rollback.py", "--cwd", self.repo, "--commits", "HEAD", "--push",
                                    env=dict(self.env, AGENT_DEV_TEAM_CODEX="off"))
        self.assertEqual(code, 0, out)

    def test_push失败(self):
        bad = self.commit_change("src/a.py", "bad\n")
        code, out = self.rb("--commits", bad, "--push")  # 没有远端
        self.assertEqual(code, 1, out)
        self.assertEqual(out.splitlines()[0], "回滚：失败")
        self.assertIn("没有配置远端", out)
        self.assertIn("已经记进审核记录", out)
        self.assertEqual(len(self.units()), 1)
        self.assertEqual(self.read("src/a.py"), "v1\n")  # 回滚提交留在本地

    def test_push_仓库配置让守卫看不懂推到哪_不推(self):
        bare = os.path.join(self.tmp.name, "remote.git")
        subprocess.run(["git", "init", "-q", "--bare", bare], check=True)
        sh(self.repo, "remote", "add", "origin", bare)
        bad = self.commit_change("src/a.py", "bad\n")
        sh(self.repo, "push", "-q", "-u", "origin", "main")
        sh(self.repo, "config", "push.default", "matching")  # 会把所有同名分支都推上去
        code, out = self.rb("--commits", bad, "--push")
        self.assertEqual(code, 1, out)
        self.assertIn("push.default", out)
        self.assertEqual(sh(bare, "rev-parse", "main"), bad)  # 远端没动


class Help(unittest.TestCase):
    def test_四个脚本都有帮助(self):
        for name in ("verify.py", "review.py", "deploy_check.py", "rollback.py"):
            r = subprocess.run([sys.executable, os.path.join(SCRIPTS, name), "--help"],
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, name)
            self.assertIn("usage", r.stdout, name)


if __name__ == "__main__":
    unittest.main(verbosity=2)
