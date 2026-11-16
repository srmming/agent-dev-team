#!/usr/bin/env python3
"""0.2.2 整体审核第 1 轮的返工：
- verify.py：常见语言的测试文件名和跳过写法要认得出；旧文件读不出来要报「验收：出错」，不能当成空文件放过
- deploy_check.py：测试查询不完整时，「测试」分项写「未确认」，不写「通过」
- qingli 技能：清理前先提交基线，每批通过就提交，失败才撤回这一批
运行：python3 -m unittest tests.test_fix_022 -v
"""
import contextlib
import io
import os
import re
import subprocess
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import test_deploy_022 as td  # noqa: E402
import test_verify_assertions as tva  # noqa: E402
from test_deploy_022 import DEP, NO_RUN, NO_STATUS, OK_RUN, OK_STATUS  # noqa: E402
from test_scripts import deploy_check, sh  # noqa: E402
from test_verify_assertions import OLD_PY  # noqa: E402

import verify  # noqa: E402

ROOT = os.path.abspath(os.path.join(HERE, ".."))


def own_tests_only(cls):
    """继承来的基类只拿它的辅助方法，基类的测试不在这里重跑一遍。"""
    for base in cls.__mro__[1:]:
        for name in list(vars(base)):
            if name.startswith("test") and name not in vars(cls):
                setattr(cls, name, None)
    return cls


class TestFileNames(unittest.TestCase):
    """测试文件名：Python、JS/TS、Go、Ruby（RSpec、minitest）、Java/Kotlin 的常见约定。"""

    def test_认得出的测试文件(self):
        for p in (
            "pkg/account_test.go",               # Go
            "spec/models/account_spec.rb",       # RSpec：spec 目录
            "lib/account_spec.rb",               # RSpec：文件名
            "test/models/account_test.rb",       # minitest
            "test/test_account.rb",              # minitest 旧写法
            "src/AccountTest.java",              # JUnit
            "src/AccountTests.java",
            "src/TestAccount.java",
            "src/main/AccountTest.kt",           # Kotlin
            "src/main/AccountTests.kt",
            "app/spec/helpers/x.rb",
            "web/account.spec.ts",               # JS/TS
            "web/account.test.tsx",
            "web/__tests__/account.js",
            "tests/test_a.py",                   # Python
            "pkg/a_test.py",
            "pkg/test_a.py",
        ):
            with self.subTest(p):
                self.assertTrue(verify.is_test_file(p), p)

    def test_普通代码不算测试文件(self):
        for p in (
            "src/latest.js", "src/contest.py", "src/Contest.java", "src/Latest.kt", "src/attestation.go",
            "lib/protest.rb", "src/testing_utils.py", "docs/spec-notes.md", "src/inspect.py",
            "src/specs_helper.py", "src/test_helper.sh", "README.md", "src/Account.java",
        ):
            with self.subTest(p):
                self.assertFalse(verify.is_test_file(p), p)


class TestSkipMarks(unittest.TestCase):
    """跳过标记：新加这样的行要被认出来。"""

    def test_认得出的跳过写法(self):
        for line in (
            '    np = pytest.importorskip("numpy")',                 # pytest
            'pytest.importorskip("numpy", minversion="1")',
            '    t.Skip("flaky")',                                   # Go
            "\tt.SkipNow()",
            '\tt.Skipf("need %s", x)',
            '    b.Skip("slow")',
            '    suite.T().Skip("x")',
            '    skip "not yet"',                                    # RSpec / minitest：行首
            "    skip",
            "    skip('later')",
            "    skip if RUBY_VERSION < '3'",
            '    pending "later"',
            "    pending",
            '  xit "does x" do',                                     # RSpec
            '  xdescribe "x" do',
            '  xcontext "x" do',
            '  it "x", skip: true do',
            '  it "x", skip: "reason" do',
            "  fit('x', () => {",                                    # 只跑这一个，等于跳过别的
            "  fdescribe('x', () => {",
            "    @Disabled",                                         # JUnit 5
            '    @Disabled("flaky")',
            "    @DisabledOnOs(OS.WINDOWS)",
            "    @Ignore",                                           # JUnit 4 / Kotlin
            '    @Ignore("flaky")',
            "    Assumptions.assumeTrue(false);",
            "    assumeTrue(isLinux())",
        ):
            with self.subTest(line):
                self.assertTrue(verify.SKIP_RE.search(line), line)

    def test_原有的跳过写法还认得(self):
        for line in ("@unittest.skip('x')", "    @skip", "self.skipTest('x')", "@pytest.mark.skipif(x, reason='y')",
                     "@pytest.mark.xfail", "pytest.skip('x')", "it.skip('x', () => {})", "describe.only('x', f)",
                     "xit('x', f)", "raise unittest.SkipTest('x')", "@unittest.expectedFailure"):
            with self.subTest(line):
                self.assertTrue(verify.SKIP_RE.search(line), line)

    def test_普通代码不算跳过(self):
        for line in (
            "    skip = 3", "    skipped = []", "    skip_count += 1", "    skipping = True",
            "    # skipped items are listed below", "    pending = []", "    pending_items.append(x)",
            "    pending_count = len(q)", "    rows = items.Skip(3).ToList()", "    skip(n)",
            "    x = fit(model)", "    model.fit(X, y)", "    clf.fit('a', 1)", "    self.assertEqual(skip, 3)", "    self.pending = False",
            "    name := t.Name()", "    assumed = True", "    ignore_list = []", "    ignored = 1",
            "    describe('x', () => {", "    it('x', () => {",
        ):
            with self.subTest(line):
                self.assertIsNone(verify.SKIP_RE.search(line), line)


@own_tests_only
class VerifyNewRules(tva.VerifyAssertions):
    """用真仓库验收：新规则走完整流程。"""

    def test_Go测试被改_不通过(self):
        self.base("pkg/account_test.go", "func TestA(t *testing.T) {\n\tif f() != 1 {\n\t\tt.Fatal(1)\n\t}\n}\n")
        self.change("pkg/account_test.go", "func TestA(t *testing.T) {\n\tif f() != 1 {\n\t}\n}\n")
        self.assert_fail("pkg/account_test.go:3（原文件）删了或改了")

    def test_RSpec被改_不通过(self):
        self.base("spec/models/account_spec.rb", "describe A do\n  it 'x' do\n    expect(1).to eq(1)\n  end\nend\n")
        self.change("spec/models/account_spec.rb", "describe A do\n  it 'x' do\n  end\nend\n")
        self.assert_fail("spec/models/account_spec.rb:3（原文件）删了或改了")

    def test_已有Python测试里新加importorskip_不通过(self):
        self.change("tests/test_m.py", "import pytest\n" + OLD_PY + "\nnp = pytest.importorskip('numpy')\n")
        self.assert_fail("新加了跳过标记")

    def test_已有Go测试里新加Skip_不通过(self):
        self.base("pkg/account_test.go", "func TestA(t *testing.T) {\n\tf()\n}\n")
        self.change("pkg/account_test.go", "func TestA(t *testing.T) {\n\tt.Skip(\"later\")\n\tf()\n}\n")
        self.assert_fail("新加了跳过标记")

    def test_只是加行_通过(self):
        self.base("pkg/account_test.go", "func TestA(t *testing.T) {\n\tf()\n}\n")
        self.change("pkg/account_test.go", "func TestA(t *testing.T) {\n\tf()\n}\n\nfunc TestB(t *testing.T) {\n\tg()\n}\n")
        self.assert_pass()


@own_tests_only
class ReadFailure(tva.VerifyAssertions):
    """旧文件读不出来：不存在、空文件、读取失败是三回事。"""

    def inproc(self, fake_run=None, fake_git=None):
        """在本进程里跑 verify.main（这样才能让 git 调用出错），返回 (退出码, 输出)。"""
        argv = ["--branch", "feat", "--cwd", self.repo, "--cmd", "touch ran"]
        buf = io.StringIO()
        real_run, real_git = verify.subprocess.run, verify.git

        def run(cmd, *a, **k):
            if fake_run and cmd[:2] == ["git", "show"]:
                return fake_run(cmd, *a, **k)
            return real_run(cmd, *a, **k)

        def git(cwd, *args):
            if fake_git and args and args[0] in ("cat-file", "ls-tree", "--literal-pathspecs"):
                return fake_git(cwd, *args)
            return real_git(cwd, *args)
        with mock.patch.object(verify.subprocess, "run", run), mock.patch.object(verify, "git", git), \
                contextlib.redirect_stdout(buf):
            code = verify.main(argv)
        return code, buf.getvalue()

    def edit_old_test(self):
        """feat 上删掉一条断言：读得出旧文件时本来是「不通过」。"""
        self.change("tests/test_m.py", OLD_PY.replace('        assert "x" in "xy"\n', "        pass\n"))

    def assert_error(self, code, out):
        self.assertEqual(code, 2, out)
        self.assertEqual(out.splitlines()[0], "验收：出错", out)
        self.assertIn("tests/test_m.py", out)
        self.assertFalse(os.path.exists(os.path.join(self.repo, "ran")))

    def test_git_show超时_出错(self):
        self.edit_old_test()

        def boom(cmd, *a, **k):
            raise subprocess.TimeoutExpired(cmd, 15)
        self.assert_error(*self.inproc(fake_run=boom))

    def test_git_show异常_出错(self):
        self.edit_old_test()

        def boom(cmd, *a, **k):
            raise OSError("没有 git")
        self.assert_error(*self.inproc(fake_run=boom))

    def test_git_show非零_出错(self):
        self.edit_old_test()
        self.assert_error(*self.inproc(
            fake_run=lambda cmd, *a, **k: subprocess.CompletedProcess(cmd, 128, b"", b"fatal")))

    def test_查文件在不在失败_出错_不当成文件不存在(self):
        self.edit_old_test()
        self.assert_error(*self.inproc(fake_git=lambda cwd, *args: None))

    def test_新分支那边读不出来_也出错(self):
        self.edit_old_test()
        real_run = subprocess.run
        calls = []

        def second_fails(cmd, *a, **k):
            calls.append(cmd)
            if len(calls) == 2:  # 第 1 次读旧文件，第 2 次读分支上的文件
                raise subprocess.TimeoutExpired(cmd, 15)
            return real_run(cmd, *a, **k)
        self.assert_error(*self.inproc(fake_run=second_fails))

    def test_出错信息写明读不出来的文件和提交(self):
        self.edit_old_test()
        code, out = self.inproc(fake_run=lambda cmd, *a, **k: subprocess.CompletedProcess(cmd, 128, b"", b""))
        self.assertIn("读不出", out)

    def test_旧文件是空文件_不是读取失败(self):
        self.base("tests/test_empty.py", "")
        self.change("tests/test_empty.py", "def test_x():\n    assert True\n")
        self.assert_pass()
        code, out = self.inproc()
        self.assertEqual(code, 0, out)

    def test_空的旧测试文件被删_仍然不通过(self):
        self.base("tests/test_empty.py", "")
        sh(self.repo, "rm", "-q", "tests/test_empty.py")
        sh(self.repo, "commit", "-q", "-m", "rm")
        self.assert_fail("tests/test_empty.py：已有测试文件被删")

    def test_旧文件不存在_新建测试随便写(self):
        self.change("tests/test_new.py", "def test_x():\n    assert True\n")
        self.assert_pass()

    def test_文件名带通配符也只按字面查(self):
        self.base("tests/test_[a].py", "def test_x():\n    assert 1\n")
        self.change("tests/test_[a].py", "def test_x():\n    pass\n")
        self.assert_fail("tests/test_[a].py:2（原文件）删了或改了")


@own_tests_only
class DeployTestUnsure(td.Deploy022):
    """check-runs 和 commit statuses 有一项没查全，「测试」分项就不能写「通过」。"""

    def run_raw(self, runs, status, deps=(), dep_state=None):
        def api(cwd, path):
            if "/check-runs" in path:
                return runs
            if f"/commits/{self.sha}/status" in path:
                return status
            if "/statuses" in path:
                return [{"state": dep_state}] if dep_state else []
            if "/deployments" in path:
                return list(deps)
            return None
        deploy_check.gh_api = api
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = deploy_check.main(["--cwd", self.repo, "--interval", "0", "--max-poll", "0"])
        return code, buf.getvalue()

    def test_check_runs查询失败_提交状态成功_测试未确认(self):
        code, out = self.run_raw(None, OK_STATUS)
        self.assertEqual(code, 3, out)
        lines = out.splitlines()
        self.assertEqual(lines[0], "上线检查：无法判断")
        self.assertIn("测试：未确认", lines)
        self.assertNotIn("测试：通过", lines)

    def test_check_runs分页没查全_测试未确认(self):
        part = {"total_count": 150, "check_runs": OK_RUN["check_runs"]}
        code, out = self.run_raw(part, OK_STATUS)
        self.assertEqual(code, 3, out)
        self.assertIn("测试：未确认", out.splitlines())

    def test_提交状态查询失败_check_runs成功_测试未确认(self):
        code, out = self.run_raw(OK_RUN, None)
        self.assertEqual(code, 3, out)
        self.assertIn("测试：未确认", out.splitlines())

    def test_提交状态分页没查全_测试未确认(self):
        part = {"state": "success", "total_count": 150, "statuses": OK_STATUS["statuses"]}
        code, out = self.run_raw(OK_RUN, part)
        self.assertEqual(code, 3, out)
        self.assertIn("测试：未确认", out.splitlines())

    def test_部署成功但测试没查全_不会通过_也不写测试通过(self):
        code, out = self.run_raw(None, OK_STATUS, DEP, "success")
        self.assertEqual(code, 3, out)
        self.assertEqual(out.splitlines()[0], "上线检查：无法判断")
        self.assertIn("测试：未确认", out.splitlines())
        self.assertIn("部署：成功", out.splitlines())

    def test_测试查询两项都失败_未确认(self):
        code, out = self.run_raw(None, None)
        self.assertEqual(code, 3, out)
        self.assertIn("测试：未确认", out.splitlines())

    def test_没查全但查到失败_仍然不通过(self):
        bad = {"total_count": 150, "check_runs": [{"id": 1, "name": "u", "status": "completed",
                                                    "conclusion": "failure"}]}
        code, out = self.run_raw(bad, OK_STATUS)
        self.assertEqual(code, 1, out)
        self.assertIn("测试：不通过", out.splitlines())

    def test_没查全又有还在跑的_写未确认(self):
        run = {"total_count": 150, "check_runs": [{"id": 1, "name": "u", "status": "in_progress"}]}
        code, out = self.run_raw(run, OK_STATUS)
        self.assertEqual(code, 3, out)
        self.assertIn("测试：未确认", out.splitlines())

    def test_两套都查全_行为不变(self):
        code, out = self.run_raw(OK_RUN, OK_STATUS, DEP, "success")
        self.assertEqual(code, 0, out)
        self.assertIn("测试：通过", out.splitlines())
        code, out = self.run_raw(NO_RUN, NO_STATUS)
        self.assertIn("测试：没有", out.splitlines())


class QingliSkill(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(ROOT, "skills", "qingli", "SKILL.md"), encoding="utf-8") as f:
            self.text = f.read()
        self.steps = {int(m.group(1)): m.group(2) for m in
                      re.finditer(r"^(\d)\. (.*?)(?=^\d\. |^## )", self.text, re.S | re.M)}

    def test_清理前先提交基线(self):
        s = self.steps[2]
        self.assertIn("基线", s)
        self.assertIn("提交", s)

    def test_每批通过就提交_失败才撤回这一批(self):
        s = self.steps[4]
        self.assertIn("git checkout -- ", s)
        self.assertLess(s.index("提交"), s.index("git checkout -- "), "要先说通过就提交，再说失败才撤回")
        self.assertIn("上一次提交", s)

    def test_提交那一步跟着改_不再是最后才提交一次(self):
        s = self.steps[5]
        self.assertNotIn("在当前分支提交", s)
        self.assertIn("没有", s)  # 确认没有遗留的改动


if __name__ == "__main__":
    unittest.main()
