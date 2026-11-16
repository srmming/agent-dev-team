#!/usr/bin/env python3
"""verify.py 的断言检查：任务分支动了已有测试（删断言、改断言、删测试文件、加跳过标记）就不通过。
全部用临时 git 仓库。运行：python3 -m unittest discover -s tests -v
"""
import os
import subprocess
import sys
import tempfile
import unittest

SCRIPT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "hooks", "scripts", "verify.py"))

OLD_PY = """import unittest


class T(unittest.TestCase):
    def test_a(self):
        self.assertEqual(1 + 1, 2)

    def test_b(self):
        assert "x" in "xy"
"""

OLD_JS = """test('a', () => {
  expect(add(1, 1)).toBe(2);
});
"""


def sh(cwd, *args):
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 失败：{r.stderr}")
    return r.stdout.strip()


class VerifyAssertions(unittest.TestCase):
    """main 上有 tests/test_m.py、web/sum.test.js 两个已有测试；每个用例在 feat 分支上改一下再验收。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = self.tmp.name
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith("AGENT_DEV_TEAM") and not k.startswith("GIT_")}
        sh(self.repo, "init", "-q", "-b", "main")
        sh(self.repo, "config", "user.name", "t")
        sh(self.repo, "config", "user.email", "t@t")
        sh(self.repo, "config", "commit.gpgsign", "false")
        self.write("src/m.py", "v1\n")
        self.write("tests/test_m.py", OLD_PY)
        self.write("web/sum.test.js", OLD_JS)
        self.commit()
        sh(self.repo, "checkout", "-q", "-b", "feat")

    def write(self, rel, text):
        p = os.path.join(self.repo, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write(text)

    def commit(self):
        sh(self.repo, "add", "-A")
        sh(self.repo, "commit", "-q", "-m", "c")

    def change(self, rel, text):
        self.write(rel, text)
        self.commit()

    def v(self, *args):
        r = subprocess.run([sys.executable, SCRIPT, "--branch", "feat", "--cwd", self.repo, *args],
                           cwd=self.repo, capture_output=True, text=True, env=self.env, timeout=60)
        return r.returncode, r.stdout + r.stderr

    def assert_fail(self, *want):
        code, out = self.v("--cmd", "touch ran")
        self.assertEqual(code, 1, out)
        self.assertEqual(out.splitlines()[0], "验收：不通过")
        self.assertIn("动了已有测试", out)
        self.assertIn("--allow-test-change", out)
        for w in want:
            self.assertIn(w, out)
        # 查出问题就不跑完成标准命令
        self.assertFalse(os.path.exists(os.path.join(self.repo, "ran")))
        # 带 --allow-test-change 放行，并说明跳过了检查
        code, out = self.v("--cmd", "true", "--allow-test-change")
        self.assertEqual(code, 0, out)
        self.assertEqual(out.splitlines()[0], "验收：通过")
        self.assertIn("已跳过断言检查", out)

    def assert_pass(self):
        code, out = self.v("--cmd", "true")
        self.assertEqual(code, 0, out)
        self.assertEqual(out.splitlines()[0], "验收：通过")
        self.assertNotIn("已跳过断言检查", out)

    # ---- 不通过 ----

    def test_删掉一条断言(self):
        self.change("tests/test_m.py", OLD_PY.replace('        assert "x" in "xy"\n', "        pass\n"))
        self.assert_fail('tests/test_m.py:9', 'assert "x" in "xy"')

    def test_改掉一条断言(self):
        self.change("tests/test_m.py", OLD_PY.replace("self.assertEqual(1 + 1, 2)", "self.assertEqual(1 + 1, 3)"))
        self.assert_fail("tests/test_m.py:6", "self.assertEqual(1 + 1, 2)")

    def test_改掉JS里的expect(self):
        self.change("web/sum.test.js", OLD_JS.replace("toBe(2)", "toBeDefined()"))
        self.assert_fail("web/sum.test.js:2")

    def test_删掉已有测试文件(self):
        os.remove(os.path.join(self.repo, "tests/test_m.py"))
        self.commit()
        self.assert_fail("tests/test_m.py：已有测试文件被删")

    def test_新加跳过标记(self):
        for rel, text in (
            ("tests/test_m.py", OLD_PY.replace("    def test_a", "    @unittest.skip('先跳过')\n    def test_a")),
            ("tests/test_m.py", OLD_PY.replace("    def test_b", "    @pytest.mark.skip\n    def test_b")),
            ("web/sum.test.js", OLD_JS.replace("test('a'", "test.skip('a'")),
            ("web/sum.test.js", OLD_JS.replace("test('a'", "xit('a'")),
        ):
            with self.subTest(rel=rel, text=text):
                sh(self.repo, "reset", "-q", "--hard", "main")
                self.change(rel, text)
                self.assert_fail("新加了跳过标记")

    def test_最多列10条(self):
        sh(self.repo, "checkout", "-q", "main")
        self.change("tests/test_m.py", OLD_PY + "".join(f"assert {i} == {i}\n" for i in range(15)))
        sh(self.repo, "checkout", "-q", "-B", "feat")
        self.change("tests/test_m.py", "")  # 17 条断言全删
        code, out = self.v()
        self.assertEqual(code, 1, out)
        self.assertEqual(out.count("断言被删或被改"), 10)
        self.assertIn("……另有 7 条", out)

    # ---- 通过 ----

    def test_只新增断言(self):
        self.change("tests/test_m.py", OLD_PY + "\n    def test_c(self):\n        self.assertTrue(True)\n")
        self.assert_pass()

    def test_新增测试文件随便写(self):
        self.change("tests/test_new.py", "import unittest\n\n\n@unittest.skip('x')\nclass N(unittest.TestCase):\n"
                    "    def test_x(self):\n        assert 1\n")
        self.assert_pass()

    def test_断言原样挪位置(self):
        moved = OLD_PY.replace('        assert "x" in "xy"\n', "        pass\n").replace(
            "        self.assertEqual(1 + 1, 2)\n", '        self.assertEqual(1 + 1, 2)\n            assert "x" in "xy"\n')
        self.change("tests/test_m.py", moved)
        self.assert_pass()

    def test_改非断言行和非测试文件(self):
        self.change("tests/test_m.py", OLD_PY.replace("class T(", "class Renamed("))
        self.change("src/m.py", "v2\n")
        self.assert_pass()

    def test_带放行参数且没动测试_也说明跳过了(self):
        self.change("src/m.py", "v2\n")
        code, out = self.v("--allow-test-change")
        self.assertEqual(code, 0, out)
        self.assertIn("已跳过断言检查", out)

    # ---- Codex 第 1 轮审核：多行断言、常见跳过写法、注释误当断言 ----

    def base(self, rel, text):
        """把 main 上的已有测试换成 text，feat 重新从 main 拉出来。"""
        sh(self.repo, "checkout", "-q", "main")
        self.write(rel, text)
        sh(self.repo, "add", "-A")
        sh(self.repo, "commit", "-q", "--allow-empty", "-m", "base")
        sh(self.repo, "checkout", "-q", "-B", "feat")

    MULTI_PY = OLD_PY + """
    def test_c(self):
        self.assertEqual(
            price(2),
            100,
        )
"""
    MULTI_JS = OLD_JS + """
test('b', () => {
  expect(total()).toEqual({
    price: 100,
  });
});
"""

    def test_多行断言改了后续行的期望值(self):
        self.base("tests/test_m.py", self.MULTI_PY)
        self.change("tests/test_m.py", self.MULTI_PY.replace("100,", "200,"))
        self.assert_fail("tests/test_m.py:12", "self.assertEqual(")

    def test_多行expect改了对象里的期望值(self):
        self.base("web/sum.test.js", self.MULTI_JS)
        self.change("web/sum.test.js", self.MULTI_JS.replace("price: 100", "price: 200"))
        self.assert_fail("web/sum.test.js:6")

    def test_多行断言只改缩进和位置_不算(self):
        self.base("tests/test_m.py", self.MULTI_PY)
        stmt = "        self.assertEqual(\n            price(2),\n            100,\n        )\n"
        moved = self.MULTI_PY.replace(stmt, "        pass\n").replace(
            "        self.assertEqual(1 + 1, 2)\n",
            "        self.assertEqual(1 + 1, 2)\n        if True:\n"
            "            self.assertEqual(\n                price(2),\n                100,\n            )\n")
        self.change("tests/test_m.py", moved)
        self.assert_pass()

    def test_常见跳过写法(self):
        for text in (
            OLD_PY.replace("import unittest\n", "import unittest\nfrom unittest import skip\n").replace(
                "    def test_a", "    @skip('先跳过')\n    def test_a"),
            OLD_PY.replace("    def test_a", "    @skipIf(True, '先跳过')\n    def test_a"),
            OLD_PY.replace("    def test_a", "    @skipUnless(False, '先跳过')\n    def test_a"),
            OLD_PY.replace("    def test_a", "    @expectedFailure\n    def test_a"),
            OLD_PY.replace("    def test_a", "    @unittest.expectedFailure\n    def test_a"),
            OLD_PY.replace("\n\nclass T", "\npytestmark = pytest.mark.skipif(True, reason='x')\n\nclass T"),
            OLD_PY.replace("\n\nclass T", "\npytestmark = [\n    pytest.mark.skipif(True, reason='x'),\n]\n\nclass T"),
            OLD_PY.replace("    def test_a(self):\n", "    def test_a(self):\n        raise unittest.SkipTest('x')\n"),
        ):
            with self.subTest(text=text):
                sh(self.repo, "reset", "-q", "--hard", "main")
                sh(self.repo, "clean", "-qfd")  # 上一轮没查出来时留下的 ran
                self.change("tests/test_m.py", text)
                self.assert_fail("新加了跳过标记")

    def test_原来就有的跳过标记和无关的pytestmark_不算(self):
        self.base("tests/test_m.py", OLD_PY.replace(
            "import unittest\n", "import unittest\nfrom unittest import skip\n").replace(
            "    def test_a", "    @skip('原来就跳过')\n    def test_a"))
        text = OLD_PY.replace("import unittest\n", "import unittest\nfrom unittest import skip\n").replace(
            "    def test_b", "    @skip('原来就跳过')\n    def test_b").replace(
            "\n\nclass T", "\npytestmark = pytest.mark.django_db\n\nclass T")
        self.change("tests/test_m.py", text)
        self.assert_pass()

    def test_改或删注释里的assert_不算(self):
        old = OLD_PY.replace("        self.assertEqual(1 + 1, 2)\n",
                             "        # assert price is calculated\n"
                             "        self.assertEqual(1 + 1, 2)  # assert sum ok\n")
        old = old.replace("    def test_b(self):\n", '    def test_b(self):\n        """assert 一下包含关系"""\n')
        for new in (
            old.replace("# assert price is calculated", "# 价格算对了"),
            old.replace("        # assert price is calculated\n", ""),
            old.replace("# assert sum ok", "# 求和没问题"),
            old.replace('"""assert 一下包含关系"""', '"""查包含关系"""'),
        ):
            with self.subTest(new=new):
                self.base("tests/test_m.py", old)
                self.change("tests/test_m.py", new)
                self.assert_pass()

    def test_改或删JS注释里的expect_不算(self):
        old = OLD_JS.replace("  expect(add(1, 1)).toBe(2);\n",
                             "  // expect(add(2, 2)).toBe(4);\n"
                             "  /*\n   * expect(add(3, 3)).toBe(6);\n   */\n"
                             "  expect(add(1, 1)).toBe(2); // expect ok\n")
        for new in (
            old.replace("  // expect(add(2, 2)).toBe(4);\n", ""),
            old.replace("  /*\n   * expect(add(3, 3)).toBe(6);\n   */\n", ""),
            old.replace("// expect ok", "// 没问题"),
        ):
            with self.subTest(new=new):
                self.base("web/sum.test.js", old)
                self.change("web/sum.test.js", new)
                self.assert_pass()

    def test_字符串里的井号不当注释(self):
        old = OLD_PY.replace('assert "x" in "xy"', 'assert "#" in "a#b"')
        self.base("tests/test_m.py", old)
        self.change("tests/test_m.py", old.replace('"a#b"', '"a#c"'))
        self.assert_fail("tests/test_m.py:9", 'assert "#" in "a#b"')


if __name__ == "__main__":
    unittest.main()
