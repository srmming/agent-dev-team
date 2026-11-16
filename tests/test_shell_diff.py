#!/usr/bin/env python3
"""对照真 shell：把一批写法交给真的 bash / zsh 跑（PATH 里放一个假的 git，只记下自己有没有被叫去 push / merge），
只要有一个 shell 真的执行了上传，守卫就必须认得出。防的是「守卫的拆法和真 shell 对不上」这一类漏洞。
全部在临时目录里跑，PATH 只有假 git 和系统自带的基本命令，不联网，不碰真实仓库
（GIT_DIR 还指到一个不存在的目录：万一哪条写法绕开 PATH 找到了真的 git，它也什么都做不了）。
运行：python3 -m unittest discover -s tests -v
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

SCRIPTS = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "hooks", "scripts"))
sys.path.insert(0, SCRIPTS)
from guard import may_ship, simple_upload  # noqa: E402

FAKE_GIT = """#!/bin/sh
case " $* " in *" push "*|*" merge "*|*" pr create "*) echo "$0 $*" >> "$MARK";; esac
exit 0
"""

# 每条都是一种把 git push 藏起来的写法。只用 shell 自己的语法和 echo / cat / env / xargs / find 这类基本命令。
TRICKS = [
    "git push",
    "git\tpush",
    "git \\\n push",
    'g"i"t pu\'\'sh',
    "\\git push",
    "'git' \"push\"",
    "git $'pu\\x73h'",
    "$'g\\x69t' push",
    "$'\\147it' push",
    "GIT push",
    "ls; git push",
    "true && git push",
    "false || git push",
    "true | git push",
    "git push &\nwait",
    "(git push)",
    "((git push); true)",
    "{ git push; }",
    "{git push}",
    "{git push;}",
    "! git push",
    "time git push",
    "if true; then git push; fi",
    "if true;then git push;fi",
    "for i in 1; do git push; done",
    "while true; do git push; break; done",
    "case x in x) git push;; esac",
    "case x in (x) git push;; esac",
    "f() { git push; }; f",
    "function f { git push; }; f",
    "f() git push; f",
    "repeat 1 git push",
    "noglob git push",
    "command git push",
    "builtin command git push",
    "exec git push",
    "env git push",
    "env -i PATH=$PATH MARK=$MARK git push",
    "env -u NOPE git push",
    "env -S 'git push'",
    "nohup git push",
    "nice git push",
    "xargs git push < /dev/null",
    "echo push | xargs git",
    "echo x | xargs -I% git push",
    "echo push | xargs -I% git %",
    "find . -maxdepth 0 -exec git push \\;",
    "FOO=1 git push",
    "echo $(git push)",
    'echo "$(git push)"',
    "echo `git push`",
    'echo "`git push`"',
    "echo `echo \\`git push\\``",
    "echo $((git push); true)",
    "echo $((git push) )",
    'echo "$((git push); true)"',
    "echo $((git push);(true))",
    "echo $(( $(git push; echo 1) + 1 ))",
    'echo "$(( $(git push; echo 1) + 1 ))"',
    "(( $(git push; echo 1) ))",
    ": ${x:=$(git push)}",
    'echo "${x:-$(git push)}"',
    "a[$(git push; echo 0)]=1",
    "[[ -n $(git push) ]]",
    '[ -n "`git push`" ]',
    "echo [[ && git push",
    "cat <(git push)",
    "cat <<EOF\n$(git push)\nEOF",
    "cat <<EOF\n`git push`\nEOF",
    "cat <<-EOF\n\t$(git push)\n\tEOF",
    "cat <<E\"O\"F\n$(git push)\nEOF\ntrue",
    "echo $(cat <<EOF)\ngit push\nEOF",
    'echo "$(cat <<EOF)"\ngit push\nEOF',
    "echo `cat <<EOF`\ngit push\nEOF",
    "cat <(cat <<EOF)\ngit push\nEOF",
    "(cat <<EOF)\ngit push\nEOF",
    "cat <<EOF; git push\nx\nEOF",
    "cat <<A <<B\nA\nB\ngit push",
    "bash -c 'git push'",
    "sh -c 'git push'",
    "sh -c \"sh -c 'git push'\"",
    "sh -c 'git \"$@\"' _ push",
    "eval 'git push'",
    "eval git\\ push",
    "eval eval eval git push",
    "sh <<EOF\ngit push\nEOF",
    "sh <<'EOF'\ngit push\nEOF",
    "sh -s <<EOF\ngit push\nEOF",
    "sh <<< 'git push'",
    "echo 'git push' | sh",
    "cat <<EOF | sh\ngit push\nEOF",
    "printf 'git push' | sh -s",
    "sh <(echo git push)",
    "x='git push'; eval \"$x\"",
    "x='git push'; sh -c \"$x\"",
    "g=git; $g push",
    "p=push; git $p",
    "git ${x:-push}",
    'git "$(echo push)"',
    "git `echo push`",
    "{git,push}",
    "git pu{sh,}",
    "git${IFS}push",
    "trap 'git push' EXIT",
    "alias p='git push'\np",
    "=git push",
    "env sh -c 'git push'",
    "echo a # x\ngit push",
    "echo 'a\nb'; git push",
    "git --no-pager push",
    "git -c a=b push",
    "git -C . push",
    "git --attr-source HEAD push",
    "git -C . merge feat",
    "gh pr create --fill",
    "env gh pr create --fill",
]

# 负责人的上传关只认「单独一条、最简单的写法」。这些带了别的东西的写法，真 shell 里 git 实际拿到的参数
# 可能和守卫看到的不一样（目录、分支、前一条命令改了仓库），所以都不能被当成简单写法。
NOT_SIMPLE = [
    "(cd / && pwd); git push",
    "cd / && git push",
    "git push origin $BRANCH",
    "git push origin `echo main`",
    'git push origin "$(echo main)"',
    "git push origin ma{in,ster}",
    "git push origin mai?",
    "git push | cat",
    "git push; git push origin evil",
    "git push && git push origin evil",
    "GIT_DIR=/x git push",
    "env GIT_DIR=/x git push",
    "git --git-dir=/x push",
    "git -c remote.origin.push=refs/heads/*:refs/heads/* push",
    "{git push}",
    "=git push",
    "git push <(echo x)",
    "git push origin main\ngit push origin evil",
]


class ShellDiff(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="adt-shell-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.bin = os.path.join(self.tmp, "bin")
        self.work = os.path.join(self.tmp, "work")
        os.makedirs(self.bin)
        os.makedirs(self.work)
        for name in ("git", "gh"):
            path = os.path.join(self.bin, name)
            with open(path, "w") as f:
                f.write(FAKE_GIT)
            os.chmod(path, 0o755)
        self.mark = os.path.join(self.tmp, "ran")
        self.shells = [(name, path, flags) for name, flags in (("bash", ["--norc", "--noprofile"]), ("zsh", ["-f"]))
                       for path in [shutil.which(name)] if path]

    def ran_in(self, shell, flags, script):
        """在真 shell 里跑一遍，返回假 git 有没有被叫去上传。"""
        if os.path.exists(self.mark):
            os.remove(self.mark)
        env = {"PATH": self.bin + ":/usr/bin:/bin", "HOME": self.work, "MARK": self.mark,
               "GIT_DIR": os.path.join(self.tmp, "no-such-repo")}
        try:
            subprocess.run([shell, *flags, "-c", script], cwd=self.work, env=env, stdin=subprocess.DEVNULL,
                           capture_output=True, timeout=20)
        except subprocess.TimeoutExpired:
            pass
        return os.path.exists(self.mark)

    def test_真shell会执行的上传_守卫都认得出(self):
        if not self.shells:
            self.skipTest("没有 bash 也没有 zsh")
        never = []
        for script in TRICKS:
            hit = [name for name, path, flags in self.shells if self.ran_in(path, flags, script)]
            if hit:
                self.assertTrue(may_ship(script), f"{'、'.join(hit)} 里真的执行了上传，守卫没认出来：{script!r}")
            else:
                never.append(script)
        # 清单里大部分写法应该至少在一个 shell 里真的跑起来，不然这个测试等于没测
        self.assertLess(len(never), len(TRICKS) // 4, never)

    def test_不是单独一条最简单写法的_都不算简单写法(self):
        for script in NOT_SIMPLE:
            self.assertTrue(may_ship(script), script)
            self.assertIsNone(simple_upload(script), script)
        # 对照：最简单的写法认得出
        self.assertEqual(simple_upload("git push origin main"), ("push", ["origin", "main"], None))
        self.assertEqual(simple_upload("rtk git -C /x merge feat 2>&1"), ("merge", ["feat"], "/x"))
        self.assertEqual(simple_upload("gh pr create --head feat"), ("pr-create", ["--head", "feat"], None))


if __name__ == "__main__":
    unittest.main(verbosity=2)
