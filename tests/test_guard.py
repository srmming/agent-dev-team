#!/usr/bin/env python3
"""守卫的命令行检查测试。运行：python3 tests/test_guard.py"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "hooks", "scripts"))
from guard import check_lead_bash  # noqa: E402


class LeadBash(unittest.TestCase):
    def ok(self, cmd):
        self.assertIsNone(check_lead_bash(cmd), cmd)

    def blocked(self, cmd, word=None):
        reason = check_lead_bash(cmd)
        self.assertIsNotNone(reason, cmd)
        if word:
            self.assertIn(word, reason, cmd)

    def test_原有的只读命令照常放行(self):
        self.ok("ls -la")
        self.ok("git status --short")
        self.ok("git -C /x diff main...HEAD")
        self.ok("cat a.txt | head -20")
        self.ok("ls && git log --oneline")
        self.ok("FOO=1 npm run check")

    def test_原有的拦截照常拦(self):
        self.blocked("ps", "`ps`")
        self.blocked("rm -rf x", "`rm`")
        self.blocked("git commit -m x", "git commit")
        self.blocked("git checkout main", "git checkout")
        self.blocked("ls && rm x", "`rm`")
        self.blocked("ls\nrm x", "`rm`")
        self.blocked("find . -name x -delete", "find")
        self.blocked("find . -exec rm {} +", "find")

    def test_重定向写文件被拦_丢弃输出放行(self):
        self.blocked("echo hi > a.txt", "重定向")
        self.blocked("echo hi >> a.txt", "重定向")
        self.blocked("ls &> out.log", "重定向")
        self.ok("ls 2>/dev/null")
        self.ok("ls > /dev/null 2>&1")
        self.ok("git status 2>&1")

    def test_引号里的符号不算操作符(self):
        # 以前会把搜索词里的 | 当管道拆开，把 status 当成命令拦掉
        self.ok(r"grep -n -e 'ui.status\|status line' reference.md | head -40")
        self.ok('grep -n "a|b;c" f.txt')
        self.ok("grep '>' f.txt")
        self.ok('python3 -c "print(1 > 0)"')
        self.ok('echo "exit=$?"')

    def test_引号外的命令替换和括号里的命令也要检查(self):
        self.blocked("echo $(rm -rf x)", "`rm`")
        self.blocked("(cd /x && rm y)", "`rm`")
        self.ok("(cd /x && ls)")

    def test_引号没闭合直接拦(self):
        self.blocked("echo 'abc", "引号")
        self.blocked('echo "abc', "引号")

    def test_claude只放行插件的只读检查(self):
        self.ok("claude plugin validate /x/lead-status")
        self.ok('claude plugin test /x/lead-status; echo "exit=$?"')
        self.ok("claude plugin list")
        self.ok("claude plugin marketplace list")
        self.ok("claude plugin --help")
        self.ok("claude plugin marketplace --help")
        self.ok("claude --version")
        self.blocked("claude -p 'edit files'", "claude")
        self.blocked("claude", "claude")
        self.blocked("claude plugin install a@b", "claude")
        self.blocked("claude plugin uninstall a@b", "claude")
        self.blocked("claude plugin marketplace add /x", "claude")

    def test_rtk包装按被包装的命令检查(self):
        self.ok("rtk find /x -maxdepth 6 -type d -name shuijiao")
        self.ok("rtk ls -la")
        self.blocked("rtk find . -delete", "find")
        self.blocked("rtk rm x", "`rm`")
        self.blocked("rtk", "`rtk`")


if __name__ == "__main__":
    unittest.main(verbosity=2)
