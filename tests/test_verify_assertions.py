#!/usr/bin/env python3
"""verify.py 的已有测试检查：已有测试文件只许加行。删了或改了原来的任何一行、删了已有测试文件、
新加跳过标记，都不通过。
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
        self.assertIn("如果派活单明确允许改这些测试，加 --allow-test-change 重跑", out)
        for w in want:
            self.assertIn(w, out)
        # 查出问题就不跑完成标准命令
        self.assertFalse(os.path.exists(os.path.join(self.repo, "ran")))
        # 带 --allow-test-change 放行，并说明跳过了检查
        code, out = self.v("--cmd", "true", "--allow-test-change")
        self.assertEqual(code, 0, out)
        self.assertEqual(out.splitlines()[0], "验收：通过")
        self.assertIn("已跳过已有测试检查（--allow-test-change）", out)

    def assert_pass(self):
        code, out = self.v("--cmd", "true")
        self.assertEqual(code, 0, out)
        self.assertEqual(out.splitlines()[0], "验收：通过")
        self.assertNotIn("已跳过", out)

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

    # ---- 不通过：删了或改了原来的行 ----

    def test_删掉一条断言(self):
        self.change("tests/test_m.py", OLD_PY.replace('        assert "x" in "xy"\n', "        pass\n"))
        self.assert_fail('tests/test_m.py:9（原文件）删了或改了：        assert "x" in "xy"')

    def test_改掉一条单行断言(self):
        self.change("tests/test_m.py", OLD_PY.replace("self.assertEqual(1 + 1, 2)", "self.assertEqual(1 + 1, 3)"))
        self.assert_fail("tests/test_m.py:6（原文件）删了或改了：        self.assertEqual(1 + 1, 2)")

    def test_改掉JS里的expect(self):
        self.change("web/sum.test.js", OLD_JS.replace("toBe(2)", "toBeDefined()"))
        self.assert_fail("web/sum.test.js:2（原文件）删了或改了：  expect(add(1, 1)).toBe(2);")

    def test_多行断言改了后续行的期望值(self):
        self.base("tests/test_m.py", self.MULTI_PY)
        self.change("tests/test_m.py", self.MULTI_PY.replace("100,", "200,"))
        self.assert_fail("tests/test_m.py:14（原文件）删了或改了：            100,")

    def test_多行expect改了对象里的期望值(self):
        self.base("web/sum.test.js", self.MULTI_JS)
        self.change("web/sum.test.js", self.MULTI_JS.replace("price: 100", "price: 200"))
        self.assert_fail("web/sum.test.js:7（原文件）删了或改了：    price: 100,")

    def test_删掉已有测试文件(self):
        os.remove(os.path.join(self.repo, "tests/test_m.py"))
        self.commit()
        self.assert_fail("tests/test_m.py：已有测试文件被删")

    def test_改已有测试里的注释和非断言行(self):
        old = OLD_PY.replace("        self.assertEqual(1 + 1, 2)\n",
                             "        # 价格算对了\n        self.assertEqual(1 + 1, 2)  # 求和\n")
        self.base("tests/test_m.py", old)
        for new, want in (
            (old.replace("# 价格算对了", "# 价格对"), "tests/test_m.py:6（原文件）删了或改了：        # 价格算对了"),
            (old.replace("        # 价格算对了\n", ""), "tests/test_m.py:6"),
            (old.replace("  # 求和", ""), "tests/test_m.py:7"),
            (old.replace("class T(", "class Renamed("), "tests/test_m.py:4"),
        ):
            with self.subTest(new=new):
                sh(self.repo, "reset", "-q", "--hard", "main")
                sh(self.repo, "clean", "-qfd")
                self.change("tests/test_m.py", new)
                self.assert_fail(want)

    def test_改JS注释(self):
        old = OLD_JS.replace("  expect(add(1, 1)).toBe(2);\n",
                             "  // 一加一\n  /*\n   * 等于二\n   */\n  expect(add(1, 1)).toBe(2);\n")
        self.base("web/sum.test.js", old)
        for new in (old.replace("// 一加一", "// 1+1"), old.replace("   * 等于二\n", "")):
            with self.subTest(new=new):
                sh(self.repo, "reset", "-q", "--hard", "main")
                sh(self.repo, "clean", "-qfd")
                self.change("web/sum.test.js", new)
                self.assert_fail("删了或改了")

    def test_只改缩进或空白(self):
        for new, want in (
            (OLD_PY.replace("        self.assertEqual(1 + 1, 2)", "            self.assertEqual(1 + 1, 2)"),
             "tests/test_m.py:6"),
            (OLD_PY.replace('"xy"\n', '"xy" \n'), "tests/test_m.py:9"),  # 最后一行行尾加空格
            (" " + OLD_PY, "tests/test_m.py:1"),  # 第一行前面加空格
            (OLD_PY.replace("\n\n\nclass", "\n\nclass"), "tests/test_m.py:"),  # 删一个空行
            (OLD_PY.replace("\n", "\r\n"), "tests/test_m.py:1"),  # 换行符改成 CRLF
        ):
            with self.subTest(new=new):
                sh(self.repo, "reset", "-q", "--hard", "main")
                sh(self.repo, "clean", "-qfd")
                self.change("tests/test_m.py", new)
                self.assert_fail(want)

    def test_最多列10条(self):
        self.base("tests/test_m.py", OLD_PY + "".join(f"assert {i} == {i}\n" for i in range(15)))
        self.change("tests/test_m.py", "")  # 原来 24 行全删
        code, out = self.v()
        self.assertEqual(code, 1, out)
        self.assertEqual(out.count("删了或改了"), 10)
        self.assertIn("……另有 14 条", out)

    def test_很长的行截到120字(self):
        line = "        assert " + "x" * 200
        self.base("tests/test_m.py", OLD_PY + line + "\n")
        self.change("tests/test_m.py", OLD_PY)
        code, out = self.v()
        self.assertEqual(code, 1, out)
        self.assertIn("删了或改了：" + line[:120] + "……\n", out)

    # ---- Codex 第 2 轮审核：解析器漏掉的两种改法 ----

    def test_多行字符串期望值把换行改成空格(self):
        old = OLD_PY.replace("    def test_b(self):\n",
                             '    def test_s(self):\n        self.assertEqual(actual, """a\nb""")\n\n    def test_b(self):\n')
        self.base("tests/test_m.py", old)
        self.change("tests/test_m.py", old.replace('"""a\nb"""', '"""a b"""'))
        self.assert_fail("tests/test_m.py:9", "tests/test_m.py:10")

    def test_JS正则里的反引号后面改期望值(self):
        old = OLD_JS + "\ntest('b', () => {\n  const s = text.replace(/`/g, \"\");\n  expect(price(s)).toBe(100);\n});\n"
        self.base("web/sum.test.js", old)
        self.change("web/sum.test.js", old.replace("toBe(100)", "toBe(200)"))
        self.assert_fail("web/sum.test.js:7（原文件）删了或改了：  expect(price(s)).toBe(100);")

    # ---- 不通过：新加跳过标记 ----

    def test_新加跳过标记(self):
        it_skip = "\nit.skip('c', () => {\n  expect(1).toBe(1);\n});\n"
        for rel, text in (
            ("tests/test_m.py", OLD_PY.replace("    def test_a", "    @unittest.skip('先跳过')\n    def test_a")),
            ("tests/test_m.py", OLD_PY.replace("import unittest\n", "import unittest\nfrom unittest import skip\n")
             .replace("    def test_a", "    @skip('先跳过')\n    def test_a")),
            ("tests/test_m.py", OLD_PY.replace("    def test_b", "    @pytest.mark.skip\n    def test_b")),
            ("tests/test_m.py", OLD_PY.replace("    def test_a", "    @skipIf(True, '先跳过')\n    def test_a")),
            ("tests/test_m.py", OLD_PY.replace("    def test_a", "    @skipUnless(False, '先跳过')\n    def test_a")),
            ("tests/test_m.py", OLD_PY.replace("    def test_a", "    @unittest.expectedFailure\n    def test_a")),
            ("tests/test_m.py", OLD_PY.replace("\n\nclass T", "\npytestmark = pytest.mark.skipif(True, reason='x')\n\nclass T")),
            ("tests/test_m.py", OLD_PY.replace("\n\nclass T", "\npytestmark = [\n    pytest.mark.skipif(True, reason='x'),\n]\n\nclass T")),
            ("tests/test_m.py", OLD_PY.replace("    def test_a(self):\n", "    def test_a(self):\n        raise unittest.SkipTest('x')\n")),
            ("web/sum.test.js", OLD_JS + it_skip),
            ("web/sum.test.js", OLD_JS + "\nxit('c', () => {});\n"),
        ):
            with self.subTest(rel=rel, text=text):
                sh(self.repo, "reset", "-q", "--hard", "main")
                sh(self.repo, "clean", "-qfd")  # 上一轮没查出来时留下的 ran
                self.change(rel, text)
                self.assert_fail("新加了跳过标记")

    def test_新加跳过标记_报的是新文件里的行号(self):
        self.change("tests/test_m.py", OLD_PY.replace("    def test_a", "    @unittest.skip('先跳过')\n    def test_a"))
        self.assert_fail("tests/test_m.py:5 新加了跳过标记：    @unittest.skip('先跳过')")

    # ---- 通过 ----

    def test_只往已有测试里加新测试(self):
        self.change("tests/test_m.py", OLD_PY + "\n    def test_c(self):\n        self.assertTrue(True)\n")
        self.change("web/sum.test.js", OLD_JS + "\ntest('b', () => {\n  expect(add(2, 2)).toBe(4);\n});\n")
        self.assert_pass()

    def test_新建测试文件随便写(self):
        self.change("tests/test_new.py", "import unittest\n\n\n@unittest.skip('x')\nclass N(unittest.TestCase):\n"
                    "    def test_x(self):\n        assert 1\n")
        self.change("web/new.test.js", "it.skip('x', () => {});\n")
        self.assert_pass()

    def test_已有的行原样挪位置(self):
        moved = OLD_PY.replace('        assert "x" in "xy"\n', "").replace(
            "        self.assertEqual(1 + 1, 2)\n", '        assert "x" in "xy"\n        self.assertEqual(1 + 1, 2)\n')
        self.change("tests/test_m.py", moved)
        self.assert_pass()

    def test_多行断言整段原样挪位置(self):
        self.base("tests/test_m.py", self.MULTI_PY)
        stmt = "        self.assertEqual(\n            price(2),\n            100,\n        )\n"
        moved = self.MULTI_PY.replace(stmt, "").replace(
            "        self.assertEqual(1 + 1, 2)\n", "        self.assertEqual(1 + 1, 2)\n" + stmt)
        self.change("tests/test_m.py", moved)
        self.assert_pass()

    def test_原来就有的跳过标记不动(self):
        old = OLD_PY.replace("import unittest\n", "import unittest\nfrom unittest import skip\n").replace(
            "    def test_a", "    @skip('原来就跳过')\n    def test_a")
        self.base("tests/test_m.py", old)
        self.base("web/sum.test.js", OLD_JS.replace("test('a'", "it.skip('a'"))
        self.change("tests/test_m.py", old + "\n    def test_c(self):\n        self.assertTrue(True)\n")
        self.change("web/sum.test.js", OLD_JS.replace("test('a'", "it.skip('a'") + "\ntest('b', () => {});\n")
        self.assert_pass()

    def test_原来就有的跳过标记原样挪位置_加无关的pytestmark(self):
        self.base("tests/test_m.py", OLD_PY.replace(
            "import unittest\n", "import unittest\nfrom unittest import skip\n").replace(
            "    def test_a", "    @skip('原来就跳过')\n    def test_a"))
        text = OLD_PY.replace("import unittest\n", "import unittest\nfrom unittest import skip\n").replace(
            "    def test_b", "    @skip('原来就跳过')\n    def test_b").replace(
            "\n\nclass T", "\npytestmark = pytest.mark.django_db\n\nclass T")
        self.change("tests/test_m.py", text)
        self.assert_pass()

    def test_改非测试文件(self):
        self.change("src/m.py", "v2\n")
        self.assert_pass()

    def test_带放行参数且没动测试_也说明跳过了(self):
        self.change("src/m.py", "v2\n")
        code, out = self.v("--allow-test-change")
        self.assertEqual(code, 0, out)
        self.assertIn("已跳过已有测试检查（--allow-test-change）", out)


if __name__ == "__main__":
    unittest.main()
