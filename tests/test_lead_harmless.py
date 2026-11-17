#!/usr/bin/env python3
"""负责人上传关（0.2.5）：只读查看、claude 会话管理、python3 -m unittest 带着 push / merge 字样时逐字放行；
选项不在固定白名单里、多条命令、别的程序一律照旧拦。
运行：python3 -m unittest discover -s tests -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_guard import GuardCase  # noqa: E402


class LeadHarmlessTest(GuardCase):
    def setUp(self):
        super().setUp()
        self.repo = self.make_repo_with_remote()
        self.commit(self.repo, "feat")  # 有没审核的提交：放行只可能是因为认出了不是上传

    def test_只读查看命令带这些字样_放行(self):
        for cmd in ("grep -rn push src", "grep -rni merge .", "grep -rn '--include=*.py' push hooks",
                    "grep -c merge README.md", "rtk grep push", "rtk proxy grep -rn push src",
                    "rg push", "rg -n -i merge hooks", "rg '--glob=*.md' push", "rg -S push --hidden",
                    "ls merge-notes", "ls -la merge-notes", "rtk ls merge-notes",
                    "cat tests/test_rollback_push.py", "cat -n tests/test_rollback_push.py",
                    "head -n 20 tests/test_rollback_push.py", "head -20 merge.md", "tail -5 push.log",
                    "tail -n 5 push.log", "wc -l tests/test_rollback_push.py",
                    "grep -rn 'push' src", "cat 'merge-notes/a.md'"):
            self.allow(self.repo, cmd)

    def test_claude会话管理_放行(self):
        for cmd in ("claude rm abc --discard-unpushed", "claude rm abc-123", "claude stop push-x",
                    "claude ls", "claude agents", "rtk claude rm abc --discard-unpushed"):
            self.allow(self.repo, cmd)

    def test_python3_m_unittest_放行(self):
        for cmd in ("python3 -m unittest tests.test_rollback_push", "python3 -m unittest -v tests.test_rollback_push",
                    "python3 -m unittest tests/test_rollback_push.py", "python3 -m unittest tests.test_a tests.test_push",
                    "python3 -m unittest discover -s tests -p test_rollback_push.py",
                    "python3 -m unittest -v discover -s tests -p '*push*.py'"):
            self.allow(self.repo, cmd)

    def test_仍然拦_别的程序和多条命令(self):
        for cmd in ("docker push img", "npm run merge", "bash -c 'git push'", "sh -c 'git push'",
                    "eval 'git push'", "env git push", "xargs git push", "echo push | xargs git",
                    "grep x; git push", "grep push src && git push", "grep push src | git push",
                    "ls merge-notes; git merge feat", "cat push.md > out.txt",
                    "find . -exec git push \\;", "find . -name '*push*'",
                    "python3 -c \"import os; os.system('git push')\"", "python3 -c 'import os' push",
                    "node -e 1 push", "perl -e 1 push", "ruby push.rb",
                    "git commit -m push", "git checkout feat/push-x", "git stash push"):
            self.deny(self.repo, cmd)

    def test_仍然拦_选项不在白名单里(self):
        # 读文件也拦：带了非白名单选项的 grep / rg / cat / head / tail / ls / wc
        for cmd in ("grep -f patterns push.txt", "grep -e push src", "grep --exclude-from=x push .",
                    "grep -rn --include='a b' push .", "rg --pre=./push-helper push src",
                    "rg --pre ./helper push", "rg '--pre-glob=*.gz' push", "rg -z push", "rg --search-zip push",
                    "rg -n --pre=cat push", "cat -v push.md", "cat --show-all push.md", "head -c 5 push.md",
                    "tail -f push.log", "tail -n x push.log", "ls --color=always merge-notes",
                    "wc --files0-from=push", "grep 'git push' README.md", "grep -rn push 'a b'",
                    "grep -rn push -- src", "cat -- push.md"):
            self.deny(self.repo, cmd, "请单独运行")

    def test_仍然拦_claude别的写法(self):
        for cmd in ("claude rm abc --discard-unpushed --force", "claude rm --discard-unpushed",
                    "claude rm -x push", "claude -p 'git push'", "claude push", "claude stop --push",
                    "claude ls push", "claude agents push", "claude rm a b --discard-unpushed"):
            self.deny(self.repo, cmd, "请单独运行")

    def test_仍然拦_unittest带额外参数(self):
        for cmd in ("python3 -m unittest -k push tests", "python3 -m unittest discover -s tests -t push",
                    "python3 -m unittest discover -s tests_push -p", "python3 -m unittest discover tests push",
                    "python3 -m unittest --buffer tests.test_push", "python3 -m unittest tests.test_push -c",
                    "python3 -m unittest -v -v tests.test_push", "python3 -m pytest tests/test_push.py",
                    "python3 -m unittest tests.test_push; git push", "python -m unittest tests.test_push",
                    "python3 -m unittest -c tests.test_push"):
            self.deny(self.repo, cmd, "请单独运行")


class LeadVerbatimBeatsScanTest(GuardCase):
    """白名单认过的命令，参数里有 git-push 这类字（拆命令扫描会当成上传），也照样放行；
    不在白名单里的同类写法照拦；子代理的规则不受影响。"""
    def setUp(self):
        super().setUp()
        self.repo = self.make_repo_with_remote()
        self.commit(self.repo, "feat")

    def test_参数里带git_push的白名单命令_放行(self):
        for cmd in ("rg git-push hooks", "cat tools/git-push", "grep -rn git-push .", "ls tools/git-push",
                    "head -n 3 tools/git-push", "wc -l tools/git-merge", "rtk grep -rn git-push .",
                    "rg gh-pr-merge hooks", "cat git-push.sh", "python3 -m unittest tests.test_git_push",
                    "claude stop git-push"):
            self.allow(self.repo, cmd)

    def test_不在白名单里的同类写法_照拦(self):
        for cmd in ("less tools/git-push", "sed -n 1p tools/git-push", "find . -name git-push",
                    "cat tools/git-push; git push", "cat tools/git-push && git push", "cat $(git-push)",
                    "cat tools/git-push | sh", "tail -f tools/git-push", "rg --pre=git-push x hooks",
                    "bash tools/git-push", "./tools/git-push", "env git-push"):
            self.deny(self.repo, cmd)

    def test_子代理和对接窗口_照旧按may_ship拦(self):
        for r in ("subagent", "kuaixiu"):
            self.set_state(role=r)
            for cmd in ("cat tools/git-push", "rg git-push hooks", "grep -rn git-push ."):
                self.deny(self.repo, cmd)


if __name__ == "__main__":
    unittest.main()
