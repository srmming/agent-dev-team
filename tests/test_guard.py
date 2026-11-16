#!/usr/bin/env python3
"""守卫测试：负责人的命令行黑名单、认上传动作、各角色的放行与拦截、审核单元、上传关、半自动。
全部用临时目录和临时 git 仓库；状态目录用 AGENT_DEV_TEAM_HOME 指到临时目录，不碰真实的 ~/.claude。
运行：python3 -m unittest discover -s tests -v
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

SCRIPTS = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "hooks", "scripts"))
sys.path.insert(0, SCRIPTS)
import records  # noqa: E402
from guard import check_lead_bash, may_ship  # noqa: E402

PREFIX = "【Agent 开发团队 拦截】"


class LeadBash(unittest.TestCase):
    """负责人的命令行：黑名单，只拦写的动作。"""

    def ok(self, *cmds):
        for cmd in cmds:
            self.assertIsNone(check_lead_bash(cmd), cmd)

    def blocked(self, cmd, word=None):
        reason = check_lead_bash(cmd)
        self.assertIsNotNone(reason, cmd)
        if word:
            self.assertIn(word, reason, cmd)

    def test_读和跑的命令一律放行(self):
        self.ok(
            "ls -la",
            "git status --short",
            "git -C /x diff main...HEAD",
            "cat a.txt | head -20",
            "ls && git log --oneline",
            "FOO=1 npm run check",
            "for f in a b; do cat $f; done",
            "sed -n 1,5p x",
            "sed -n '1,5p' x | sed -e 's/a/b/'",
            "awk '{print $1}' x",
            "awk -F: '$3 > 100 {print $1}' /etc/passwd",
            "python3 x.py",
            "python3 -m unittest discover -s tests -v",
            'grep "git push" x',
            "grep -rn 'rm -rf' .",
            "ps aux",
            "mkdir -p /tmp/x",
            "claude -p 'hi'",
            "claude plugin validate /x",
            "codex exec --sandbox read-only 'review'",
            "if [ -f x ]; then cat x; else echo no; fi",
            "while read l; do echo $l; done < x",
            "diff <(ls a) <(ls b)",
            "jq '.a | .b' x.json",
            "which rm",
            "command -v rm",
        )

    def test_不算写文件的重定向放行(self):
        self.ok(
            "echo a 2>&1",
            "cmd > /dev/null",
            "ls 2>/dev/null",
            "ls > /dev/null 2>&1",
            "ls &> /dev/null",
            "ls &>/dev/null &",
            "git status 2>&1 | head",
            "echo err >&2",
            "echo err 1>&2",
            "cmd 2>&-",
            "cmd >/dev/stderr",
            "cat < in.txt",
            "wc -l <<< 'a b'",
        )

    def test_git只拦改工作区的子命令(self):
        self.ok(
            "git merge feat/x",
            "git merge --no-ff feat/x -m 'merge'",
            "git push",
            "git push origin main",
            "git worktree add ../wt -b feat/x",
            "git worktree remove ../wt",
            "git branch -D feat/x",
            "git revert abc123",
            "git init",
            "git tag v1",
            "git fetch --all",
            "git pull --ff-only",
            "git log --grep commit",
            "git show HEAD:a.txt",
            "git --no-pager diff",
            "git remote -v",
        )
        for sub in ("commit", "add", "checkout", "switch", "reset", "restore", "rebase", "cherry-pick",
                    "stash", "apply", "am", "clean", "rm", "mv"):
            self.blocked(f"git {sub} x", f"git {sub}")
        self.blocked("git commit -m x", "git commit")
        self.blocked("git -C /x commit -m x", "git commit")
        self.blocked("git -c user.name=a -C /x --no-pager add .", "git add")
        self.blocked("git --git-dir /x/.git checkout main", "git checkout")
        self.blocked("/usr/bin/git reset --hard", "git reset")

    def test_重定向写文件被拦(self):
        self.blocked("echo hi > a.txt", "重定向")
        self.blocked("echo hi >> a.txt", "重定向")
        self.blocked("echo hi >a.txt", "重定向")
        self.blocked("ls &> out.log", "重定向")
        self.blocked("ls &>> out.log", "重定向")
        self.blocked("ls >| out.log", "重定向")
        self.blocked("ls >& out.log", "重定向")
        self.blocked("ls 2> err.log", "重定向")
        self.blocked("ls > /dev/null 2> err.log", "重定向")
        self.blocked("cat > a.py <<'EOF'\nprint(1)\nEOF", "重定向")
        self.blocked("for f in a b; do cat $f; done > all.txt", "重定向")
        self.blocked('echo x > "$TMP/a"', "重定向")

    def test_写文件的命令被拦(self):
        for name in ("rm", "mv", "cp", "tee", "touch", "truncate", "dd", "patch", "install", "ln", "chmod", "chown"):
            self.blocked(f"{name} a b", f"`{name}`")
        self.blocked("rm -rf x", "`rm`")
        self.blocked("/bin/rm x", "`rm`")
        self.blocked("ls && rm x", "`rm`")
        self.blocked("ls; rm x", "`rm`")
        self.blocked("ls || rm x", "`rm`")
        self.blocked("ls\nrm x", "`rm`")
        self.blocked("cat a | tee b", "`tee`")
        self.blocked("FOO=1 BAR=2 touch x", "`touch`")
        # 别的命令的参数里出现这些词不算
        self.ok("npm install", "pip install x", "brew install jq", "echo rm", "ls cp mv", "git log -- rm")

    def test_sed和perl带i被拦_不带的放行(self):
        self.blocked("sed -i 's/a/b/' x", "sed -i")
        self.blocked("sed -i.bak 's/a/b/' x", "sed -i")
        self.blocked("sed -i '' 's/a/b/' x", "sed -i")
        self.blocked("sed -ni 5p x", "sed -i")
        self.blocked("sed -E -i 's/a/b/' x", "sed -i")
        self.blocked("sed -e 's/a/b/' -i x", "sed -i")
        self.blocked("sed --in-place=.bak 's/a/b/' x", "sed -i")
        self.blocked("perl -i -pe 's/a/b/' x", "perl -i")
        self.blocked("perl -pi -e 's/a/b/' x", "perl -i")
        self.blocked("perl -pi.bak -e 's/a/b/' x", "perl -i")
        self.blocked("perl -0pi -e 's/a/b/' x", "perl -i")
        self.blocked("perl -pe 's/a/b/' -i x", "perl -i")
        self.ok(
            "sed -n 1,5p x",
            "sed -e 's/-i/x/' f",
            "sed -E 's/a/b/' f",
            "sed -ne '/x/p' f",
            "sed -f fix-i.sed f",
            "sed 's/a/b/' my-input.txt",
            "perl -ne 'print if /x/' f",
            "perl -Mwarnings -e 'print 1'",
            "perl -Ilib script.pl -i",
            "perl -lane 'print $F[0]' f",
            "perl -e 'print \"-i\"'",
        )

    def test_find带删除或执行被拦(self):
        self.blocked("find . -name x -delete", "find")
        self.blocked("find . -exec rm {} +", "find")
        self.blocked("find . -execdir cat {} \\;", "find")
        self.ok("find . -name '*.py' -type f", "find . -newer x -print | wc -l", "find . -name delete")

    def test_引号里的符号不算操作符(self):
        # 以前会把搜索词里的 | 当管道拆开
        self.ok(
            r"grep -n -e 'ui.status\|status line' reference.md | head -40",
            'grep -n "a|b;c" f.txt',
            "grep '>' f.txt",
            'python3 -c "print(1 > 0)"',
            'echo "exit=$?"',
            'echo "a > b"',
            "echo 'rm x; touch y'",
            "echo \"it's fine\"",
            "echo a\\>b",
            "echo $'it\\'s > ok'",
        )

    def test_命令替换和括号里的命令也要检查(self):
        self.blocked("echo $(rm -rf x)", "`rm`")
        self.blocked('echo "$(rm -rf x)"', "`rm`")
        self.blocked("echo `rm -rf x`", "`rm`")
        self.blocked('echo "`touch x`"', "`touch`")
        self.blocked('echo "$(echo "$(git commit -m x)")"', "git commit")
        self.blocked('echo "$(ls > out.txt)"', "重定向")
        self.blocked("(cd /x && rm y)", "`rm`")
        self.blocked("x=$(cat a | tee b)", "`tee`")
        self.ok(
            "(cd /x && ls)",
            'echo "$(git rev-parse HEAD)"',
            "echo `date`",
            "n=$(wc -l < x)",
            "echo '$(rm x)'",
            'echo "\\$(rm x)"',
        )

    def test_循环和条件里面的命令也要检查(self):
        self.blocked("for f in a b; do rm $f; done", "`rm`")
        self.blocked("if [ -f x ]; then touch y; fi", "`touch`")
        self.blocked("if true; then :; else git add .; fi", "git add")
        self.blocked("while read l; do echo $l; done < in > out", "重定向")
        self.blocked("! rm x", "`rm`")
        self.blocked("{ rm x; }", "`rm`")
        self.blocked("f() { rm x; }; f", "`rm`")
        self.blocked("case $a in x) rm y ;; esac", "`rm`")
        self.blocked("time cp a b", "`cp`")

    def test_算术_条件_注释_heredoc正文不误拦(self):
        self.ok(
            "echo $((1 > 0))",
            'echo "$((2 >> 1))"',
            "(( i > 3 )) && echo big",
            "[[ $a > $b ]] && echo yes",
            "if [[ $a < $b && -f x ]]; then echo y; fi",
            "ls # it's a comment > not a file",
            "ls\n# rm x\npwd",
            "python3 - <<'EOF'\nif 1 > 0:\n    print(\"it's ok\")\nEOF",
            "python3 - <<EOF\nrm = 1\nprint(rm > 0)\nEOF\nls",
            "cat <<-EOF | wc -l\n\ta > b\n\tEOF",
            "echo ${#arr[@]} $#",
        )
        # heredoc 结束之后的命令照常检查
        self.blocked("python3 - <<'EOF'\nprint(1)\nEOF\nrm x", "`rm`")
        self.blocked("[[ $a > $b ]] && echo yes > out.txt", "重定向")

    def test_引号或括号没闭合直接拦并提示改写(self):
        self.blocked("echo 'abc", "改写")
        self.blocked('echo "abc', "改写")
        self.blocked("echo `abc", "改写")
        self.blocked('echo "$(abc"', "改写")

    def test_包装器按被包装的命令检查(self):
        self.ok(
            "rtk find /x -maxdepth 6 -type d -name shuijiao",
            "rtk ls -la",
            "rtk",
            "env",
            "sudo ls",
            "ls | xargs grep rm",
            "ls | xargs -n 1 cat",
            "nohup python3 x.py &",
        )
        self.blocked("rtk find . -delete", "find")
        self.blocked("rtk rm x", "`rm`")
        self.blocked("rtk git commit -m x", "git commit")
        self.blocked("sudo rm x", "`rm`")
        self.blocked("env FOO=1 rm x", "`rm`")
        self.blocked("command rm x", "`rm`")
        self.blocked("find . -name '*.pyc' | xargs rm", "`rm`")
        self.blocked("ls | xargs -n 1 rm -f", "`rm`")
        self.blocked("ls | xargs -I{} mv {} /tmp", "`mv`")


class ShipWords(unittest.TestCase):
    """第一步：命令里有没有可能出现上传动作（子代理、对接窗口有就拦）。"""

    def ship(self, *cmds):
        for cmd in cmds:
            self.assertTrue(may_ship(cmd), cmd)

    def not_ship(self, *cmds):
        for cmd in cmds:
            self.assertFalse(may_ship(cmd), cmd)

    def test_是上传命令(self):
        self.ship(
            "git push",
            "git push origin main",
            "git -c a=b push",
            "git -C /x push",
            "git -C /x -c a=b --no-pager merge feat",
            "git merge feat",
            "gh pr create --fill",
            "gh pr merge 12 --squash",
            "gh -R o/r pr merge 1",
            "cd /x && git push",
            "ls; git push",
            "FOO=1 git push",
            "rtk git push",
            "echo $(git push)",
            'echo "$(git push)"',
            "/usr/bin/git push",
            "if true; then git push; fi",
            "git send-pack origin main",
            "git subtree push --prefix=a origin b",
            "git lfs push origin main",
        )

    def test_不是上传命令(self):
        self.not_ship(
            'grep "git push" f',
            "grep 'git merge' -r .",
            'git commit -m "之后再 git push"',
            "git log --grep push",
            "git log --merges",
            "git status",
            "git stash push -m x",
            "git merge-base a b",
            "git pushx",
            "git config push.default simple",
            "git -c push.default=current status",
            "git pull",
            "git fetch origin",
            "gh pr view 12",
            "gh pr list",
            "gh repo create x",
            "cat <<'EOF'\ngit push\nEOF",
            "cat <<EOF\ngit push\nEOF",
            "python3 - <<EOF\ngit push\nEOF",
            "git commit -F - <<'EOF'\n说明里提到 git push\nEOF",
            "ls # git push",
            "bash script.sh",
            "env",
            "command -v git",
            "timeout 30 ls",
            "time -p ls",
            "python3 review.py --branch feat/merge-fix --base main",
            "if [ -f merge.txt ]; then cat merge.txt; fi",
            "for f in *.py; do grep merge $f; done",
            "cd $dir && git status",
            "ls *.md | grep merge",
            '"$VENV/bin/python" tests/test_merge.py',
        )

    def test_git和gh出现在一段命令的任何位置都认_不靠包装器名单(self):
        self.ship(
            "env -u TEST git push origin main",
            "env -i PATH=/usr/bin git push",
            "env -S 'git push'",
            "env --unset=X -- git push",
            "rtk proxy git push origin main",
            "sudo -u bob git push",
            "timeout 30 git push",
            "timeout -s KILL -k 5 30 git push",
            "nice -n 5 git push",
            "time -p git push",
            "nohup git push &",
            "command git push",
            "exec git push",
            "stdbuf -o0 git push",
            "ls | xargs -n1 git push",
            "find . -maxdepth 0 -exec git push \\;",
            "git --git-dir=/x/.git push",
            "git --work-tree /x merge feat",
            "! git push",
            "{ git push; }",
            "(git push)",
            "true && (cd /x; git push)",
            "echo `git push`",
            "x=$(git push)",
            # 守卫没见过的包装器、zsh 的写法
            "strace -f git push",
            "some-new-wrapper --flag git merge feat",
            "repeat 1 git push",
            "noglob git push",
            "coproc git push",
            "op run -- gh pr create --fill",
            "jj git push",
            # 代价：命令名不是 git，但词里连着出现 git push 的也算
            "echo git push",
            "bash -c 'echo git push'",
        )

    def test_git的选项带不带值不用猜_大小写和git自带的程序也认(self):
        self.ship(
            "git --attr-source HEAD push",         # 守卫没记过的带值选项：后面的词两种都算
            "git --some-new-option value merge feat",
            "git -C /x --no-pager -c a=b push",
            "git -C push status",                  # 代价：目录正好叫 push 也算
            "GIT push",                            # macOS 的文件名不分大小写
            "Git merge feat",
            "/usr/bin/GIT push",
            "GH pr create --fill",
            "BASH -c 'git push'",
            '"$(git --exec-path)/git-push" origin main',
            "/usr/lib/git-core/git-merge feat",
            "git-send-pack origin main",
        )
        self.not_ship(
            "git --no-pager log --grep push",
            "git -C /x log -S merge",
            "git -C $DIR status",
            "git -C \"$REPO\" diff main...feat",
            "git -c core.pager=cat show merge.txt",
            "git --git-dir=/x/.git log push.txt",
        )

    def test_交给shell再跑一遍的文字也拆开查(self):
        self.ship(
            "bash -c 'git push'",
            'sh -lc "cd /x && git push"',
            "zsh -ec 'gh pr merge 1'",
            "eval 'git push'",
            "bash <<EOF\ngit push\nEOF",
            "sh -s <<'EOF'\ngit merge feat\nEOF",
            "bash <<< 'git push'",
            "env FOO=1 bash -c 'env -u X rtk proxy git push'",
            "strace sh -c 'git push'",
            "ls | xargs -I{} sh -c 'git push'",
            "trap 'git push' EXIT",
            "alias p='git push'\np",
            "emulate sh -c 'git push'",
            'bash -c "bash -c \\"bash -c \'git push\'\\""',
        )
        self.not_ship("bash -c 'ls -la'", "sh -c 'git status'", "eval 'ls'", "trap 'echo bye' EXIT", "trap - INT",
                      "alias ll='ls -la'")

    def test_gh_api只有明显只读的才放(self):
        self.ship(
            "gh api -X PUT repos/o/r/pulls/1/merge",
            "gh api --method=POST repos/o/r/merges -f base=main -f head=x",
            "gh api repos/o/r/git/refs/heads/main -X PATCH -f sha=abc",
            "gh api -XPOST repos/o/r/merges",
            "gh api repos/o/r/contents/a.txt -f message=m -f content=eA==",
            "gh api repos/o/r/issues --input body.json",
            "gh api graphql -f query='mutation { x }'",
            "gh api repos/o/r/pulls/1 --unknown-flag",
            "gh api $FLAGS repos/o/r/pulls/1",
        )
        self.not_ship(
            "gh api repos/o/r/pulls/1",
            "gh api repos/o/r/pulls/1/merge",
            "gh api -H 'Accept: application/vnd.github+json' repos/o/r/commits/abc/check-runs --jq '.total_count'",
            "gh api --paginate repos/o/r/deployments -q '.[].id'",
            "gh api -X GET search/issues -f q=repo:o/r",
            "gh api --method get repos/o/r/pulls",
        )

    def test_守卫和真shell拆法不一样的地方_按会执行算(self):
        # 这些写法在 bash / zsh 里都会真的跑 git push（逐条在真 shell 里确认过），以前的拆法会漏
        self.ship(
            "((git push); echo)",                 # 两层子 shell，不是算术
            "echo $((git push); true)",           # 命令替换套子 shell，不是算术
            "echo $((git push) )",
            'echo "$((git push); true)"',
            "echo $(( $(git push) ))",            # 算术里套命令替换
            "(( $(git push) ))",
            'echo "$(( $(git push) + 1 ))"',
            "echo `echo \\`git push\\``",         # 反引号里再套反引号
            "git $'pu\\x73h'",                    # $'...' 里用转义拼字
            "$'g\\x69t' push",
            "echo [[ && git push",                # 不在命令开头的 [[ 不是条件
            "cat <<EOF\n$(git push)\nEOF",        # 结束符没加引号的 heredoc 正文会展开
            "cat <<EOF\n`git push`\nEOF",
            "cat <<-EOF\n\t$(git push)\n\tEOF",
            "echo $(cat <<EOF)\ngit push\nEOF",    # zsh：括号里开的 heredoc，后面几行会被当命令执行
            "cat <(cat <<'EOF')\ngit push\nEOF",
            'echo "$(cat <<EOF)"\ngit push\nEOF',
            "echo `cat <<EOF`\ngit push\nEOF",
            "{git push}",                           # zsh：花括号不用空格
            "{git push;}",
            "() {git push}",
            "=git push",                            # zsh：=git 是 git 的完整路径
            "=gh pr create --fill",
        )
        self.not_ship(
            "cat <<'EOF'\n$(git push)\nEOF",      # 结束符加了引号：正文原样，不执行
            "cat <<\\EOF\n$(git push)\nEOF",
            "echo '$(git push)'",
            "echo $((1 + 2))",
            "(( i > 3 )) && echo big",
            "[[ $a > $b ]] && echo yes",
            "if [[ -n $x && -f y ]]; then echo merge; fi",
            "echo {a,b}.txt",
            "find . -name '*.py' -exec grep -l merge {} +",
            "echo ${x}merge",
            "f() { echo merge; }; f",
        )

    def test_看不懂的结构加上push或merge这类词_按有算(self):
        self.ship(
            "$GIT push",                          # 命令名是变量
            "env $GIT push origin main",
            "${GIT:-git} merge feat",
            "$GH pr create --fill",
            "{git,push}",                         # bash 的花括号展开
            "x='git push'; $x",
            "x='git push'; eval \"$x\"",
            "echo 'git push' | bash",             # 命令从标准输入喂给 shell
            "echo 'git push' | sudo sh -s",
            "bash <(echo git push)",
            "echo push | xargs git",              # 子命令从别处来
            "xargs -I% git % <<< push",
            "find push -maxdepth 0 -exec git {} \\;",
            "git${IFS}push",
            "echo 'git push",                     # 引号没闭合
            'echo "$(git push"',
            "x=push; git $x",                     # git 的子命令是变量：不管有没有这些词都算
            "git pu{s,}h",
            "git p?sh",
            "git $sub",
            "git -c alias.p=push p",              # 临时别名
            "gh pr $ACTION 1",
        )
        # 一层套一层套得太深，守卫不往下拆，按看不懂算
        deep = "git push"
        for _ in range(12):
            deep = "bash -c " + __import__("shlex").quote(deep)
        self.ship(deep)
        # 看不懂，但命令里没有 push / merge 这类词：不拦
        self.not_ship(
            "echo 'abc",
            "$PYTHON -m unittest",
            "echo hi | bash",
            "which git",
            "ls | xargs git",
        )


class GuardCase(unittest.TestCase):
    """把钩子输入从 stdin 喂给 guard.py，看退出码和 stderr。"""

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="adt-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = os.path.join(self.tmp, "state")
        self.user_home = os.path.join(self.tmp, "home")
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("AGENT_DEV_TEAM") and not k.startswith("GIT_")}
        env.update(
            AGENT_DEV_TEAM_HOME=self.home, HOME=self.user_home,
            GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1",
            GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.com",
            GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.com",
        )
        os.makedirs(self.user_home)
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.count = 0

    # ----- 搭环境 -----

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

    def make_repo_with_remote(self, name="repo"):
        """带远端的仓库：main 已经推上去并设好上游。"""
        repo = self.make_repo(name)
        bare = os.path.join(self.tmp, name + "-origin.git")
        self.git(self.tmp, "init", "-q", "--bare", "-b", "main", bare)
        self.git(repo, "remote", "add", "origin", bare)
        self.git(repo, "push", "-q", "-u", "origin", "main")
        return repo

    def commit(self, repo, branch=None):
        """在 repo 当前分支（或指定分支）上加一个提交，返回提交号。指定分支时不切走当前分支。"""
        self.count += 1
        if branch is None:
            self.git(repo, "commit", "-q", "--allow-empty", "-m", f"c{self.count}")
            return self.git(repo, "rev-parse", "HEAD")
        if subprocess.run(["git", "rev-parse", "--verify", "--quiet", "refs/heads/" + branch],
                          cwd=repo, capture_output=True).returncode != 0:
            self.git(repo, "branch", branch)
        tree = self.git(repo, "rev-parse", branch + "^{tree}")
        sha = self.git(repo, "commit-tree", tree, "-p", branch, "-m", f"c{self.count}")
        self.git(repo, "update-ref", "refs/heads/" + branch, sha)
        return sha

    def reviewed(self, repo, branch, base="main"):
        """像 review.py 通过时那样记一个审核单元：base..branch 这一段，顶端是 branch。返回这一段的提交号。"""
        tip = self.git(repo, "rev-parse", branch)
        shas = self.git(repo, "rev-list", f"{base}..{branch}").split()
        records.add_unit(repo, tip, shas, self.git(repo, "rev-parse", base))
        return shas

    def write(self, path, text=""):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)

    def change(self, repo, rel, text, msg="改"):
        """在 repo 当前分支上改一个文件并提交，返回提交号。"""
        self.write(os.path.join(repo, rel), text)
        self.git(repo, "add", "-A")
        self.git(repo, "commit", "-q", "-m", msg)
        return self.git(repo, "rev-parse", "HEAD")

    def set_state(self, sid="s1", **state):
        self.write(os.path.join(self.home, "sessions", sid + ".json"), json.dumps(state))

    # ----- 跑守卫 -----

    def guard_raw(self, stdin, env=None):
        full_env = dict(os.environ)
        full_env.update(env or {})
        return subprocess.run([sys.executable, os.path.join(SCRIPTS, "guard.py")],
                              input=stdin, capture_output=True, text=True, env=full_env)

    def guard(self, cwd, tool_input, tool="Bash", sid="s1", env=None, **extra):
        if isinstance(tool_input, str):
            tool_input = {"command": tool_input}
        data = {"session_id": sid, "cwd": cwd, "transcript_path": "/x/t.jsonl", "permission_mode": "default",
                "hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": tool_input}
        data.update(extra)
        return self.guard_raw(json.dumps(data), env=env)

    def allow(self, cwd, tool_input, **kw):
        r = self.guard(cwd, tool_input, **kw)
        self.assertEqual((r.returncode, r.stderr), (0, ""), tool_input)

    def deny(self, cwd, tool_input, word=None, **kw):
        r = self.guard(cwd, tool_input, **kw)
        self.assertEqual(r.returncode, 2, f"{tool_input}: {r.stderr}")
        self.assertTrue(r.stderr.startswith(PREFIX), r.stderr)
        if word:
            self.assertIn(word, r.stderr, tool_input)
        return r.stderr


class RolesTest(GuardCase):
    """各角色的放行与拦截。"""

    WRITES = ["rm x", "echo a > b.txt", "git commit -m x", "sed -i s/a/b/ x"]
    SHIPS = ["git push", "git -c a=b push", "git -C . merge feat", "gh pr create --fill", "gh pr merge 1",
             "ls && git push origin main", 'echo "$(git push)"', "strace git push", "x='git push'; $x"]

    def edits(self, repo):
        return [("Edit", {"file_path": os.path.join(repo, "a.py"), "old_string": "a", "new_string": "b"}),
                ("Write", {"file_path": os.path.join(repo, "b.py"), "content": "x"}),
                ("MultiEdit", {"file_path": "src/c.py", "edits": []}),
                ("NotebookEdit", {"notebook_path": os.path.join(repo, "n.ipynb"), "new_source": "x"})]

    def unreviewed_repo(self):
        """带远端的仓库，main 上有一个没审核的提交，还有一个没审核的 feat 分支。"""
        repo = self.make_repo_with_remote()
        self.commit(repo)
        self.commit(repo, "feat")
        return repo

    def test_none_环境变量关掉_全部放行(self):
        repo = self.unreviewed_repo()
        off = {"AGENT_DEV_TEAM": "off"}
        for cmd in self.WRITES + self.SHIPS:
            self.allow(repo, cmd, env=off)
            self.allow(repo, cmd, env=off, agent_id="a1")
        for tool, inp in self.edits(repo):
            self.allow(repo, inp, tool=tool, env=off)
        # 对照：不关的时候是拦的
        self.deny(repo, "rm x")
        self.deny(repo, "git push")

    def test_none_项目里有关闭文件_全部放行(self):
        repo = self.unreviewed_repo()
        self.write(os.path.join(repo, ".claude", "agent-dev-team-off"))
        for cmd in self.WRITES + self.SHIPS:
            self.allow(repo, cmd)
        for tool, inp in self.edits(repo):
            self.allow(repo, inp, tool=tool)

    def test_normal_普通对话_全部放行(self):
        repo = self.unreviewed_repo()
        self.set_state(role="normal", auto="semi")
        for cmd in self.WRITES + self.SHIPS:
            self.allow(repo, cmd)
        for tool, inp in self.edits(repo):
            self.allow(repo, inp, tool=tool)

    def test_normal_普通对话里开的后台子代理_也放行上传(self):
        repo = self.unreviewed_repo()
        self.set_state(role="normal")
        for cmd in self.SHIPS:
            self.allow(repo, cmd, agent_id="a1", agent_type="agent-dev-team:dev-opus")
        # 负责人模式里开的后台子代理照样拦
        self.set_state(role="lead")
        self.deny(repo, "git push", "子代理不能 push", agent_id="a1")

    def test_subagent_后台子代理_只拦上传(self):
        repo = self.unreviewed_repo()
        for cmd in self.SHIPS:
            self.deny(repo, cmd, "子代理不能 push", agent_id="a1", agent_type="agent-dev-team:dev-opus")
        for cmd in self.WRITES + ['grep "git push" f', "echo 'git merge x'", 'git commit -m "先不 git push"',
                                  "git log --grep push", "gh pr view 1", "echo 'abc", "git stash push",
                                  "git rebase main", "git pull --rebase"]:
            self.allow(repo, cmd, agent_id="a1")
        for tool, inp in self.edits(repo):
            self.allow(repo, inp, tool=tool, agent_id="a1")

    def test_subagent_审核通过了也不能上传(self):
        repo = self.unreviewed_repo()
        self.reviewed(repo, "feat")
        self.reviewed(repo, "main", "origin/main")
        self.allow(repo, "git merge feat")  # 负责人可以
        for cmd in ("git merge feat", "git push", "git push origin feat"):
            self.deny(repo, cmd, "子代理不能 push", agent_id="a1")

    def test_subagent_窗口子代理_按状态文件认(self):
        repo = self.unreviewed_repo()
        self.set_state(role="subagent")
        self.deny(repo, "git -c a=b push", "子代理不能 push")
        self.deny(repo, "git merge feat", "子代理不能 push")
        self.deny(repo, "echo 'git push", "保守判断")  # 拆不开，又出现了 push
        self.allow(repo, 'grep "git push" f')
        self.allow(repo, "rm x")
        self.allow(repo, self.edits(repo)[0][1], tool="Edit")

    def test_kuaixiu_对接窗口_只拦上传(self):
        repo = self.unreviewed_repo()
        self.set_state(role="kuaixiu")
        for cmd in self.SHIPS:
            self.deny(repo, cmd, "对接窗口不能 push")
        for cmd in self.WRITES + ['grep "git push" f']:
            self.allow(repo, cmd)
        for tool, inp in self.edits(repo):
            self.allow(repo, inp, tool=tool)
        # 旧做法：按分支名认
        old = self.make_repo("old", branch="kuaixiu/fix")
        self.deny(old, "git push", "对接窗口不能 push", sid="s2")
        self.allow(old, "git commit -m x", sid="s2")

    def test_lead_拦写的命令_其余放行(self):
        repo = self.make_repo()
        for cmd in self.WRITES:
            self.deny(repo, cmd, "子代理")
        self.deny(repo, "echo 'abc", "改写")
        for cmd in ["ls -la", "for f in a b; do cat $f; done", "sed -n 1,5p x", "awk '{print $1}' x", "python3 x.py",
                    'grep "git push" x', "echo a 2>&1", "cmd > /dev/null", "git worktree add ../wt -b feat",
                    "git revert abc", "git merge --abort", "git pull", "git fetch origin", "git branch -D feat",
                    "git log --grep merge", "$PYTHON -m unittest"]:
            self.allow(repo, cmd)

    def test_lead_不能用编辑工具改项目里的文件(self):
        repo = self.make_repo()
        for tool, inp in self.edits(repo):
            self.deny(repo, inp, "派给子代理", tool=tool)
        # 在子目录里用相对路径也一样
        sub = os.path.join(repo, "src")
        os.makedirs(sub)
        self.deny(sub, {"file_path": "../a.py", "content": "x"}, tool="Write")
        self.deny(sub, {"file_path": "d.py", "content": "x"}, tool="Write")

    def test_lead_项目外的文件放行(self):
        repo = self.make_repo()
        outside = self.make_dir("outside")
        for path in (os.path.join(outside, "note.md"),
                     os.path.join(self.home, "sessions", "s1.json"),
                     os.path.join(self.user_home, ".claude", "projects", "p", "memory", "MEMORY.md"),
                     "../outside/plan.md"):
            self.allow(repo, {"file_path": path, "content": "x"}, tool="Write")
        self.allow(repo, {"notebook_path": os.path.join(outside, "n.ipynb")}, tool="NotebookEdit")
        self.allow(repo, {"content": "没有路径"}, tool="Write")
        # 别的工具不管
        self.allow(repo, {"file_path": os.path.join(repo, "a.py")}, tool="Read")

    def test_lead_不在git仓库里仍然生效(self):
        plain = self.make_dir("plain")
        outside = self.make_dir("outside")
        self.deny(plain, "rm x", "`rm`")
        self.deny(plain, "echo a > b", "重定向")
        self.allow(plain, "ls -la")
        self.deny(plain, "git push", "不是 git 仓库")  # 不在仓库里却有上传命令：查不了就拦
        self.deny(plain, {"file_path": os.path.join(plain, "a.py"), "content": "x"}, tool="Write")
        self.deny(plain, {"file_path": "sub/a.py", "old_string": "a", "new_string": "b"}, tool="Edit")
        self.allow(plain, {"file_path": os.path.join(outside, "a.py"), "content": "x"}, tool="Write")
        self.allow(plain, {"file_path": "../outside/a.py", "content": "x"}, tool="Write")

    def test_lead_cwd是家目录时_状态目录和记忆仍然放行(self):
        home = self.user_home
        self.deny(home, {"file_path": os.path.join(home, "code", "a.py"), "content": "x"}, tool="Write")
        self.allow(home, {"file_path": os.path.join(home, ".claude", "projects", "p", "MEMORY.md"), "content": "x"},
                   tool="Write")
        self.allow(home, {"file_path": os.path.join(self.home, "sessions", "s1.json"), "content": "{}"}, tool="Write")
        # 项目本身就在 ~/.claude 里面时，照常拦
        inner = os.path.join(home, ".claude", "plugins", "p")
        os.makedirs(inner)
        self.deny(inner, {"file_path": os.path.join(inner, "a.py"), "content": "x"}, tool="Write")
        # cwd 正好包住状态目录时，状态目录里的文件放行，别的照拦
        self.allow(self.tmp, {"file_path": os.path.join(self.home, "sessions", "s1.json"), "content": "{}"},
                   tool="Write")
        self.deny(self.tmp, {"file_path": os.path.join(self.tmp, "a.py"), "content": "x"}, tool="Write")

    def test_输入坏了一律放行(self):
        repo = self.make_repo()
        for stdin in ("", "{坏的", "[]", '"x"', "null",
                      json.dumps({"cwd": repo, "tool_name": "Bash", "tool_input": "rm x"}),
                      json.dumps({"cwd": repo, "tool_name": "Bash", "tool_input": {"command": 123}}),
                      json.dumps({"cwd": repo, "tool_name": "Write", "tool_input": {"file_path": 5}}),
                      json.dumps({"cwd": os.path.join(self.tmp, "不存在"), "tool_name": "Bash",
                                  "tool_input": {"command": "ls"}})):
            r = self.guard_raw(stdin)
            self.assertEqual((r.returncode, r.stderr), (0, ""), stdin)

    def test_状态文件坏了按负责人处理_不报错(self):
        repo = self.make_repo()
        self.write(os.path.join(self.home, "sessions", "s1.json"), "{坏的")
        self.deny(repo, "rm x", "`rm`")
        self.allow(repo, "ls")


class ReviewUnitTest(GuardCase):
    """审核记录绑定「实际审核过的最终状态」。只带了某个审核单元的一部分就拦。"""

    def setUp(self):
        super().setUp()
        self.repo = self.make_repo_with_remote()
        self.review_py = os.path.join(SCRIPTS, "review.py")

    def test_整批审核不给中间提交放行(self):
        # Codex 第 2 轮：A 引入问题、B 撤销，整段审核通过。只上传 A 必须拦。
        self.git(self.repo, "checkout", "-q", "-b", "feat")
        a = self.change(self.repo, "evil.txt", "有问题\n", "A：引入")
        self.git(self.repo, "rm", "-q", "evil.txt")
        self.git(self.repo, "commit", "-q", "-m", "B：撤销")
        self.git(self.repo, "checkout", "-q", "main")
        self.git(self.repo, "branch", "only-a", a)
        self.assertEqual(len(self.reviewed(self.repo, "feat")), 2)  # 审核的是 A、B 一起的最终结果

        # 整段一起上：放行
        self.allow(self.repo, "git merge feat")
        self.allow(self.repo, "git push -u origin feat")
        self.allow(self.repo, "gh pr create --head feat")
        # 只带 A：合并、推送、开 PR 都拦
        msg = self.deny(self.repo, "git merge only-a", "有 1 个提交还没通过 Codex 审核")
        self.assertIn("不能只上传其中一部分", msg)
        self.deny(self.repo, "git push -u origin only-a", "还没通过 Codex 审核")
        self.deny(self.repo, "git push origin only-a:main", "还没通过 Codex 审核")
        self.deny(self.repo, "gh pr create --head only-a", "还没通过 Codex 审核")
        # 把 A 挪到主分支上再推：一样拦
        self.git(self.repo, "merge", "-q", "--ff-only", "only-a")
        self.deny(self.repo, "git push", "不能只上传其中一部分")
        self.deny(self.repo, "git push origin main", "还没通过 Codex 审核")
        # B 也合进来之后，整段都在：放行
        self.git(self.repo, "merge", "-q", "--ff-only", "feat")
        self.allow(self.repo, "git push")

    def test_单元顶端之后又加了提交_新加的要审(self):
        self.commit(self.repo, "feat")
        self.commit(self.repo, "feat")
        self.reviewed(self.repo, "feat")
        self.allow(self.repo, "git merge feat")
        self.commit(self.repo, "feat")
        msg = self.deny(self.repo, "git merge feat", "有 1 个提交")
        self.assertNotIn("不能只上传其中一部分", msg)
        self.assertIn(f"python3 {self.review_py} --branch feat --base main --cwd {self.repo}", msg)
        self.reviewed(self.repo, "feat")  # 重审：新单元包住三个提交
        self.allow(self.repo, "git merge feat")

    def test_正常流程_快进合并再推主分支(self):
        self.commit(self.repo, "feat")
        self.commit(self.repo, "feat")
        self.deny(self.repo, "git merge feat", "有 2 个提交")
        self.reviewed(self.repo, "feat")
        self.allow(self.repo, "git merge feat")
        self.git(self.repo, "merge", "-q", "--ff-only", "feat")
        self.allow(self.repo, "git push")
        self.allow(self.repo, "git push origin main")

    def test_正常流程_非快进合并再推主分支(self):
        self.git(self.repo, "checkout", "-q", "-b", "feat")
        self.change(self.repo, "feat.txt", "1\n")
        self.change(self.repo, "feat.txt", "2\n")
        self.git(self.repo, "checkout", "-q", "main")
        self.reviewed(self.repo, "feat")
        self.allow(self.repo, "git merge --no-ff feat -m 'merge feat'")
        self.git(self.repo, "merge", "-q", "--no-ff", "feat", "-m", "merge feat")
        self.allow(self.repo, "git push")  # 合并提交是两边自动合出来的，不用审

    def test_正常流程_多个任务分支先后合并再一起推(self):
        for name in ("t1", "t2", "t3"):  # 三个任务分支都从最初的 main 分出去，各改各的文件
            self.git(self.repo, "checkout", "-q", "-b", name, "origin/main")
            self.change(self.repo, name + ".txt", "1\n")
            self.change(self.repo, name + ".txt", "2\n")
        self.git(self.repo, "checkout", "-q", "main")
        for name in ("t1", "t2", "t3"):
            self.deny(self.repo, f"git merge {name}", "还没通过 Codex 审核")
            self.reviewed(self.repo, name)
            self.allow(self.repo, f"git merge {name}")
            self.git(self.repo, "merge", "-q", "--no-edit", name)  # 第一个快进，后两个是合并提交
        self.assertEqual(len(self.git(self.repo, "rev-list", "--merges", "origin/main..main").split()), 2)
        self.allow(self.repo, "git push")
        self.allow(self.repo, "git push origin main:main")

    def test_rebase之后提交号变了_要重审(self):
        self.git(self.repo, "checkout", "-q", "-b", "feat")
        self.change(self.repo, "feat.txt", "1\n")
        self.git(self.repo, "checkout", "-q", "main")
        self.reviewed(self.repo, "feat")
        self.allow(self.repo, "git merge feat")
        main_new = self.change(self.repo, "main.txt", "1\n")
        records.add_unit(self.repo, main_new, [main_new])
        self.git(self.repo, "rebase", "-q", "main", "feat")
        self.git(self.repo, "checkout", "-q", "main")
        self.deny(self.repo, "git merge feat", "有 1 个提交")
        self.reviewed(self.repo, "feat")
        self.allow(self.repo, "git merge feat")

    def test_夹带改动的合并提交要审_干净的合并不用(self):
        self.commit(self.repo, "feat")
        self.reviewed(self.repo, "feat")
        # 手搓一个合并提交：父提交都审过，但内容多了一个文件
        self.write(os.path.join(self.repo, "evil.txt"), "x")
        self.git(self.repo, "add", "evil.txt")
        tree = self.git(self.repo, "write-tree")
        self.git(self.repo, "rm", "-q", "--cached", "evil.txt")
        os.remove(os.path.join(self.repo, "evil.txt"))
        m = self.git(self.repo, "commit-tree", tree, "-p", "main", "-p", "feat", "-m", "merge")
        self.git(self.repo, "update-ref", "refs/heads/evil", m)
        self.deny(self.repo, "git merge evil", "有 1 个提交")
        self.reviewed(self.repo, "evil")
        self.allow(self.repo, "git merge evil")
        # 干净的合并（两边自动合出来的）不用审
        self.git(self.repo, "merge", "-q", "--no-ff", "feat", "-m", "merge feat")
        self.allow(self.repo, "git push")

    def test_手动解过冲突的合并提交要审(self):
        self.git(self.repo, "checkout", "-q", "-b", "feat")
        self.change(self.repo, "a.txt", "feat\n")
        self.git(self.repo, "checkout", "-q", "main")
        self.reviewed(self.repo, "feat")
        m = self.change(self.repo, "a.txt", "main\n")
        records.add_unit(self.repo, m, [m])
        subprocess.run(["git", "merge", "feat"], cwd=self.repo, capture_output=True)  # 冲突
        self.write(os.path.join(self.repo, "a.txt"), "解冲突时写进来的新内容\n")
        self.git(self.repo, "add", "-A")
        self.git(self.repo, "commit", "-q", "-m", "merge feat（解了冲突）")
        self.deny(self.repo, "git push", "有 1 个提交")

    def test_旧格式的passed文件作废(self):
        a = self.commit(self.repo)
        state = os.path.join(self.repo, ".git", "agent-dev-team")
        os.makedirs(state, exist_ok=True)
        with open(os.path.join(state, "passed"), "w") as f:
            f.write(a + "\n")
        msg = self.deny(self.repo, "git push", "有 1 个提交")
        self.assertIn("已经作废", msg)
        self.reviewed(self.repo, "main", "origin/main")
        self.allow(self.repo, "git push")

    def test_记录文件坏了_上传命令拦_普通命令不受影响(self):
        self.commit(self.repo)
        state = os.path.join(self.repo, ".git", "agent-dev-team")
        os.makedirs(os.path.join(state, records.UNITS_FILE))  # 记录文件变成了目录，读的时候会出错
        self.deny(self.repo, "git push", "自己出错了")
        self.deny(self.repo, "git merge main", "自己出错了")
        self.allow(self.repo, "ls -la")
        self.allow(self.repo, "git status")
        self.allow(self.repo, "git push", env={"AGENT_DEV_TEAM": "off"})

    def test_守卫自己不跑Codex_也不写审核记录(self):
        self.commit(self.repo)
        marker = os.path.join(self.tmp, "codex-ran")
        env = {"AGENT_DEV_TEAM_CODEX_CMD": f"touch {marker}"}
        self.deny(self.repo, "git push", "审核", env=env)
        self.assertFalse(os.path.exists(marker))
        self.assertEqual(records.units(self.repo), [])
        self.deny(self.repo, "git push", "审核", env=env)  # 再来一次还是拦，不会自己变通过

    def test_审核记录按仓库存_工作副本共用(self):
        self.commit(self.repo, "feat")
        wt = os.path.join(self.tmp, "wt")
        self.git(self.repo, "worktree", "add", "-q", wt, "feat")
        self.reviewed(wt, "feat")
        self.allow(self.repo, "git merge feat")

    def test_守卫一秒内完成(self):
        for _ in range(20):
            self.commit(self.repo)
        start = time.monotonic()
        self.deny(self.repo, "git push", "审核")
        self.assertLess(time.monotonic() - start, 2.0)  # 留余量给慢机器，正常远小于 1 秒


class UploadGateTest(GuardCase):
    """负责人的上传关：看不懂就拦。上传命令要单独一条、写成最简单的形式。"""

    def setUp(self):
        super().setUp()
        self.repo = self.make_repo_with_remote()
        self.review_py = os.path.join(SCRIPTS, "review.py")

    def all_reviewed(self):
        """main 上有一个新提交，feat 分支上有一个提交，都审核过了：下面被拦只可能是因为写法。"""
        self.commit(self.repo)
        self.commit(self.repo, "feat")
        self.reviewed(self.repo, "main", "origin/main")
        self.reviewed(self.repo, "feat")

    # ----- 合并 -----

    def test_merge_没审核拦_审核过放行(self):
        self.commit(self.repo, "feat")
        self.commit(self.repo, "feat")
        msg = self.deny(self.repo, "git merge feat", "还没通过 Codex 审核")
        self.assertIn("有 2 个提交", msg)
        self.assertTrue(os.path.isabs(self.review_py))
        self.assertIn(f"python3 {self.review_py} --branch feat --base main --cwd {self.repo}", msg)
        self.deny(self.repo, "git merge --no-ff feat -m 'merge feat'", "有 2 个提交")
        self.reviewed(self.repo, "feat")
        for cmd in ("git merge feat", "git merge --no-ff feat -m 'merge feat'", "git merge --no-ff --no-edit feat",
                    "git merge --ff-only -q feat", "git merge feat --message='合并 feat'", "rtk git merge feat",
                    "rtk proxy git merge feat", "git merge feat 2>&1", "git merge feat > /dev/null 2>&1",
                    "git merge -m '说明里有 --squash 和 ; 也没关系' feat"):
            self.allow(self.repo, cmd)

    def test_merge_用C指到别的仓库去那里查(self):
        self.commit(self.repo, "feat")
        plain = self.make_dir("plain")
        self.deny(plain, f"git -C {self.repo} merge feat", "还没通过 Codex 审核")
        self.reviewed(self.repo, "feat")
        self.allow(plain, f"git -C {self.repo} merge feat")
        self.allow(self.tmp, "git -C repo merge feat")  # 相对路径按钩子给的 cwd 算
        self.deny(plain, "git -C repo merge feat", "不是 git 仓库")
        self.deny(plain, f"git -C {self.tmp}/不存在 merge feat", "不是 git 仓库")

    def test_merge_中止放行_看不懂的写法一律拦(self):
        self.all_reviewed()
        self.allow(self.repo, "git merge --abort")
        self.allow(self.repo, "git merge --quit")
        for cmd, word in (
                ("git merge --continue", "不解决合并冲突"),
                ("git merge -s ours feat", "看不懂 git merge 的选项"),
                ("git merge -X theirs feat", "看不懂 git merge 的选项"),
                ("git merge --squash feat", "看不懂 git merge 的选项"),
                ("git merge --no-commit feat", "看不懂 git merge 的选项"),
                ("git merge --allow-unrelated-histories feat", "看不懂 git merge 的选项"),
                ("git merge -", "看不懂 git merge 的选项"),
                ("git merge feat main", "一次合并一个分支"),
                ("git merge", "一次合并一个分支"),
                ("git merge feat~0", "一次合并一个分支"),
                ("git merge feat^", "一次合并一个分支"),
                ("git merge 'feat x'", "一次合并一个分支"),
                ("git merge 不存在的分支", "找不到分支"),
        ):
            self.deny(self.repo, cmd, word)

    # ----- 推送 -----

    def test_push_没审核拦_审核过放行(self):
        self.allow(self.repo, "git push")  # 没有要上传的提交
        self.commit(self.repo)
        self.commit(self.repo)
        msg = self.deny(self.repo, "git push", "还没通过 Codex 审核")
        self.assertIn("有 2 个提交", msg)
        self.assertIn(f"python3 {self.review_py} --branch main --base refs/remotes/origin/main --cwd {self.repo}", msg)
        self.deny(self.repo, "git push origin main", "有 2 个提交")
        self.reviewed(self.repo, "main", "origin/main")
        for cmd in ("git push", "git push origin", "git push origin main", "git push origin main:main",
                    "git push origin HEAD", "git push origin HEAD:main", "git push origin main:refs/heads/main",
                    "git push -u origin main", "git push --set-upstream origin main", "git push -q",
                    "rtk git push", "rtk proxy git push origin main", "git push 2>&1",
                    "git push origin main > /dev/null 2>&1", f"git -C {self.repo} push",
                    "git push --dry-run origin main"):
            self.allow(self.repo, cmd)

    def test_push_本地建一个叫origin_main的分支骗不了守卫(self):
        a = self.commit(self.repo)
        self.git(self.repo, "branch", "origin/main", a)  # 本地分支 origin/main 指到没审核的提交上
        self.deny(self.repo, "git push", "有 1 个提交")
        self.deny(self.repo, "git push origin main", "有 1 个提交")
        self.deny(self.repo, "gh pr create --fill", "有 1 个提交")
        wt = os.path.join(self.tmp, "wt")
        self.git(self.repo, "worktree", "add", "-q", "-b", "feat", wt)
        self.deny(wt, "git push -u origin feat", "有 1 个提交")  # 远端没有 feat：从远端的主分支算起

    def test_push_指定任务分支时查那个分支(self):
        self.commit(self.repo, "feat")
        # 当前在 main 上（没有新提交），但要推的是没审核的 feat
        msg = self.deny(self.repo, "git push -u origin feat", "审核")
        self.assertIn("--branch feat --base refs/remotes/origin/main", msg)
        self.deny(self.repo, "git push origin feat:feat", "审核")
        self.deny(self.repo, "git push origin feat:main", "审核")
        self.reviewed(self.repo, "feat")
        self.allow(self.repo, "git push -u origin feat")
        self.allow(self.repo, "git push origin feat:feat-v2")  # 推成新的远端分支
        # 远端已经有这个分支时，从远端的这个分支算起
        self.git(self.repo, "push", "-q", "origin", "feat")
        self.allow(self.repo, "git push origin feat")
        self.commit(self.repo, "feat")
        self.deny(self.repo, "git push origin feat", "有 1 个提交")

    def test_push_单独的冒号_删除_通配符_多个分支_标签_一律拦(self):
        # Codex 第 2 轮：git push origin : 是推送所有同名分支，以前被当成删除跳过了
        self.all_reviewed()
        tag = self.git(self.repo, "rev-parse", "main")
        self.git(self.repo, "tag", "v1", tag)
        for cmd in ("git push origin :", "git push origin :feat", "git push origin +main", "git push origin +main:main",
                    "git push origin 'refs/heads/*:refs/heads/*'", "git push origin main feat",
                    "git push origin main:main feat:feat", "git push origin main:refs/tags/v2",
                    "git push origin main:", "git push origin main:a:b", "git push origin main~0:main",
                    "git push origin HEAD:HEAD"):
            self.deny(self.repo, cmd, "只认这几种写法")
        for cmd in ("git push origin v1", "git push origin v1:main", "git push origin refs/remotes/origin/main:main",
                    f"git push origin {tag}:main"):
            self.deny(self.repo, cmd, "本地没有分支")  # 只能推本地分支，标签、提交号、远端跟踪分支都不认
        for cmd in ("git push --all", "git push --all origin", "git push --mirror", "git push --tags",
                    "git push --branches origin", "git push origin --delete feat", "git push -d origin feat",
                    "git push --no-verify", "git push --repo=origin", "git push -o ci.skip", "git push --prune origin",
                    "git push --follow-tags", "git push --atomic origin main"):
            self.deny(self.repo, cmd, "看不懂 git push 的选项")
        # 没审核的时候更不行
        self.commit(self.repo, "feat")
        self.deny(self.repo, "git push origin :", "只认这几种写法")

    def test_push_强推一律拦(self):
        self.all_reviewed()
        for cmd in ("git push --force", "git push -f", "git push --force-with-lease", "git push -uf origin main",
                    "git push --force-with-lease=main origin main", "git push origin main --force",
                    "git push --force-if-includes origin main", "git push -fu origin feat"):
            self.deny(self.repo, cmd, "不能强推")

    def test_push_只能推到配置好的远端(self):
        self.all_reviewed()
        other = os.path.join(self.tmp, "other.git")
        self.git(self.tmp, "init", "-q", "--bare", other)
        for cmd in (f"git push {other} main", f"git push file://{other} main:main",
                    "git push https://example.com/x.git main", "git push nope main", "git push . feat:main"):
            self.deny(self.repo, cmd, "不是这个仓库配置的远端")

    def test_push_仓库配置让守卫看不懂推到哪_只放两边都写明的(self):
        self.all_reviewed()
        for key, val in (("push.default", "matching"), ("push.default", "upstream"), ("push.default", "nothing"),
                         ("remote.origin.push", "refs/heads/*:refs/heads/*"), ("remote.origin.mirror", "true"),
                         ("remote.pushDefault", "origin"), ("branch.main.pushRemote", "origin")):
            self.git(self.repo, "config", key, val)
            for cmd in ("git push", "git push origin", "git push origin main", "git push -u origin feat"):
                self.deny(self.repo, cmd, "仓库配置了")
            self.allow(self.repo, "git push origin main:main")
            self.git(self.repo, "config", "--unset", key)
        for val in ("simple", "current"):
            self.git(self.repo, "config", "push.default", val)
            self.allow(self.repo, "git push")
            self.allow(self.repo, "git push origin main")

    def test_push_不带分支时要有同名的上游(self):
        self.all_reviewed()
        wt = os.path.join(self.tmp, "wt")
        self.git(self.repo, "worktree", "add", "-q", wt, "feat")
        self.deny(wt, "git push", "没有同名的上游分支")       # 新分支还没有上游
        self.deny(wt, "git push origin", "没有同名的上游分支")
        self.allow(wt, "git push -u origin feat")
        self.git(wt, "branch", "-q", "--set-upstream-to=origin/main")
        self.deny(wt, "git push", "没有同名的上游分支")       # 上游是别的名字：推到哪要看配置
        self.git(wt, "push", "-q", "-u", "origin", "feat")
        self.allow(wt, "git push")
        self.allow(wt, "git push origin")
        self.git(wt, "remote", "add", "backup", self.git(wt, "remote", "get-url", "origin"))
        self.deny(wt, "git push backup", "没有同名的上游分支")  # 和上游不是同一个远端
        self.git(wt, "checkout", "-q", "--detach")
        self.deny(wt, "git push", "没有同名的上游分支")
        self.deny(wt, "git push origin HEAD", "只认这几种写法")
        self.allow(wt, "git push origin HEAD:feat")

    def test_push_没有远端_第一次推送_不在仓库里_都拦(self):
        local = self.make_repo("local")
        self.commit(local)
        self.deny(local, "git push", "没有配置远端")
        self.deny(local, "git push origin main", "没有配置远端")
        self.deny(local, "gh pr create --fill", "没有远端")
        # 有远端但还没推过任何东西（第一次推送）：查不出哪些提交是新的
        bare = os.path.join(self.tmp, "empty.git")
        self.git(self.tmp, "init", "-q", "--bare", bare)
        self.git(local, "remote", "add", "origin", bare)
        self.deny(local, "git push -u origin main", "第一次推送")
        self.deny(local, "git push origin main:main", "第一次推送")
        plain = self.make_dir("plain")
        self.deny(plain, "git push", "不是 git 仓库")
        self.deny(plain, "git merge feat", "不是 git 仓库")
        self.deny(plain, "gh pr create --fill", "不是 git 仓库")

    def test_守卫超时_上传命令拦_普通命令放行(self):
        # 钩子超时会被当成放行，所以守卫自己限时：查得太久时，可能是上传命令就拦
        self.commit(self.repo)
        slow = self.make_dir("slow-bin")
        real = shutil.which("git")
        self.write(os.path.join(slow, "git"),
                   f'#!/bin/sh\ncase " $* " in *" rev-list "*) sleep 8;; esac\nexec {real} "$@"\n')
        os.chmod(os.path.join(slow, "git"), 0o755)
        env = {"PATH": slow + os.pathsep + os.environ["PATH"], "AGENT_DEV_TEAM_GUARD_TIMEOUT": "1"}
        start = time.monotonic()
        self.deny(self.repo, "git push", "超时", env=env)
        self.assertLess(time.monotonic() - start, 6)
        self.allow(self.repo, "git status", env=env)
        self.deny(self.repo, "git push", "还没通过 Codex 审核")  # 对照：不慢的时候照常查

    def test_一长串eval不会把守卫拖慢(self):
        start = time.monotonic()
        self.deny(self.repo, "eval " * 60 + "git push")
        self.deny(self.repo, "eval " * 60 + "git push", "子代理不能 push", agent_id="a1")
        self.assertLess(time.monotonic() - start, 6)

    def test_git跑不了时_上传命令拦_普通命令放行(self):
        self.all_reviewed()
        no_git = {"PATH": self.make_dir("empty-bin")}
        self.deny(self.repo, "git push", env=no_git)
        self.deny(self.repo, "git merge feat", env=no_git)
        self.allow(self.repo, "ls -la", env=no_git)

    # ----- 上传命令要单独一条 -----

    def test_上传命令和别的东西写在一起_一律拦(self):
        # Codex 第 2 轮：(cd / && pwd); git push 实际还在原仓库推，以前守卫以为到了 / 就不查了
        self.all_reviewed()
        gitdir = os.path.join(self.repo, ".git")
        cmds = [
            "(cd / && pwd); git push",
            "cd / ; git push",
            f"cd {self.repo} && git push",
            f"pushd {self.repo} && git push",
            "pushd / ; popd ; git push",
            "(cd /) && git merge feat",
            f"git --git-dir={gitdir} push",
            f"git --git-dir {gitdir} --work-tree {self.repo} push",
            f"git --work-tree={self.repo} merge feat",
            f"GIT_DIR={gitdir} git push",
            f"GIT_WORK_TREE={self.repo} git merge feat",
            f"env GIT_DIR={gitdir} git push",
            f"env -C {self.repo} git push",
            f"git -C {self.repo} -C . push",
            "git -c push.default=matching push",
            "git --no-pager push",
            "env git push",
            "FOO=1 git push",
            "timeout 30 git push",
            "command git push",
            "/usr/bin/git push",
            "git push | tail -3",
            "git push && echo ok",
            "ls; git push",
            "git merge feat && git push",
            "git push; git push origin feat",
            "git push &",
            "git push\ngit status",
            "bash -c 'git push'",
            "eval 'git push'",
            '{ git push; }',
            "(git push)",
            "! git push",
            "if true; then git push; fi",
            'echo "$(git push)"',
            "git push origin $BRANCH",
            'git push origin "$(git branch --show-current)"',
            "git push origin `git branch --show-current`",
            "git push origin ma{in,in}",
            "git merge fea?",
            "git push <<< x",
            "gh pr create --fill && git push",
            "cd / && gh pr create --fill",
            "GH_REPO=o/r gh pr create --fill",
        ]
        for cwd in (self.repo, self.make_dir("plain")):  # 不在仓库里的目录也一样拦（不能因为「不在仓库」就跳过）
            for cmd in cmds:
                self.deny(cwd, cmd, "请单独运行")
        # 单独写就放行
        for cmd in ("git push", "git merge feat", "git push origin feat", "gh pr create --fill"):
            self.allow(self.repo, cmd)

    def test_看不懂的命令里出现push或merge_拦(self):
        self.all_reviewed()
        for cmd in ("x='git push'; $x", "echo 'git push' | bash", "$GIT_BIN merge-base a b", "echo push | xargs git"):
            self.deny(self.repo, cmd, "看不懂这条命令")
        for cmd in ("$GIT push", "git $SUB origin main", "x=push; git $x", "git -c alias.p=push p"):
            self.deny(self.repo, cmd)
        # 审核关关掉（又不是半自动）就没什么要查的
        off = {"AGENT_DEV_TEAM_CODEX": "off"}
        for cmd in ("x='git push'; $x", "$GIT push", "cd / && git push", "git push --force"):
            self.allow(self.repo, cmd, env=off)

    # ----- gh -----

    def test_gh_pr_create(self):
        a = self.commit(self.repo)
        self.deny(self.repo, "gh pr create --fill", "审核")
        self.allow(self.repo, "gh pr view 12")
        self.reviewed(self.repo, "main", "origin/main")
        for cmd in ("gh pr create --fill", "gh pr create --title 't' --body 'b: git push; rm -rf'", "gh pr create -t x -b y -d",
                    "gh pr create --base main --head main --fill", "gh pr create --title=x --body=y --draft",
                    "rtk gh pr create --fill"):
            self.allow(self.repo, cmd)
        self.commit(self.repo, "feat")
        msg = self.deny(self.repo, "gh pr create --head feat --base main --title x --body y", "--branch feat")
        self.assertIn("--base refs/remotes/origin/main", msg)
        self.deny(self.repo, "gh pr create -H feat", "审核")
        self.deny(self.repo, "gh pr create --head=feat", "审核")
        self.reviewed(self.repo, "feat")
        self.allow(self.repo, "gh pr create --head feat --base main --title x --body y")
        self.allow(self.repo, "gh pr create -H feat -B main -f")
        # 同一个选项写两次，gh 用后面那个，守卫也查后面那个
        self.commit(self.repo, "evil")
        self.deny(self.repo, "gh pr create --head feat -H evil", "--branch evil")
        self.deny(self.repo, "gh pr create -H feat --head=evil", "--branch evil")
        self.allow(self.repo, "gh pr create -H evil --head feat")
        for cmd, word in (("gh pr create --head feat -R o/r", "看不懂 gh pr create 的参数"),
                          ("gh pr create --head feat --repo o/r", "看不懂 gh pr create 的参数"),
                          ("gh pr create --head feat --web", "看不懂 gh pr create 的参数"),
                          ("gh pr create --head feat extra", "看不懂 gh pr create 的参数"),
                          ("gh pr create --head", "看不懂 gh pr create 的参数"),
                          ("gh pr create --head someone:feat", "head 分支"),
                          ("gh pr create --head 不存在", "本地没有分支"),
                          ("gh pr create --head feat --base 不存在", "本地没有远端分支")):
            self.deny(self.repo, cmd, word)
        self.assertTrue(a)

    def test_gh_pr_merge和gh_api的写操作_负责人不用(self):
        self.all_reviewed()
        for cmd in ("gh pr merge 12 --squash", "gh pr merge", "gh -R o/r pr merge 1"):
            self.deny(self.repo, cmd, "不用 gh pr merge")
        for cmd in ("gh api -X PUT repos/o/r/pulls/1/merge", "gh api repos/o/r/git/refs/heads/main -X PATCH -f sha=abc",
                    "gh api repos/o/r/contents/a.txt -f message=m", "gh api graphql -f query=x"):
            self.deny(self.repo, cmd, "gh api 的写操作")
        self.allow(self.repo, "gh api repos/o/r/pulls/1")
        self.allow(self.repo, "gh pr view 12")
        self.allow(self.repo, "gh pr close 12")

    # ----- 开关 -----

    def test_CODEX关掉时跳过上传关(self):
        self.commit(self.repo)
        self.commit(self.repo, "feat")
        off = {"AGENT_DEV_TEAM_CODEX": "off"}
        for cmd in ("git push", "git merge feat", "gh pr create --fill", "gh pr merge 1", "git push origin :"):
            self.deny(self.repo, cmd)
            self.allow(self.repo, cmd, env=off)
        # 关的只是上传关，写的命令照拦
        self.deny(self.repo, "rm x", "`rm`", env=off)


class SemiAutoTest(GuardCase):
    """半自动：有远端时不直接合并进主分支、不直接推主分支、不用 gh 合并 PR。"""

    def setUp(self):
        super().setUp()
        self.repo = self.make_repo_with_remote()
        self.commit(self.repo, "feat")
        self.reviewed(self.repo, "feat")
        self.set_state(auto="semi")

    def test_拦合并进主分支(self):
        msg = self.deny(self.repo, "git merge feat", "半自动模式")
        self.assertIn("开 PR", msg)
        self.deny(self.repo, "git merge --no-ff feat", "半自动模式")
        self.allow(self.repo, "git merge --abort")

    def test_拦推主分支(self):
        for cmd in ("git push", "git push origin", "git push origin main", "git push origin HEAD",
                    "git push origin feat:main", "git push origin HEAD:refs/heads/main", "git push -u origin main"):
            self.deny(self.repo, cmd, "半自动模式：不直接推 main")
        for cmd in ("git push --all", "git push --mirror", "git push origin :", "git push --force origin feat:main"):
            self.deny(self.repo, cmd)

    def test_推任务分支和开PR放行(self):
        self.allow(self.repo, "git push -u origin feat")
        self.allow(self.repo, "git push origin feat:feat")
        self.allow(self.repo, "gh pr create --head feat --base main --fill")
        self.allow(self.repo, "gh pr create --head feat")

    def test_开PR仍要过上传关(self):
        self.commit(self.repo, "feat")
        self.deny(self.repo, "gh pr create --head feat --fill", "还没通过 Codex 审核")
        self.deny(self.repo, "git push -u origin feat", "还没通过 Codex 审核")

    def test_在任务分支的工作副本里合并和推送不受限(self):
        self.commit(self.repo, "other")
        self.reviewed(self.repo, "other")
        wt = os.path.join(self.tmp, "wt")
        self.git(self.repo, "worktree", "add", "-q", wt, "feat")
        self.allow(wt, "git merge other")
        self.allow(wt, "git push -u origin feat")
        self.allow(self.repo, f"git -C {wt} merge other")
        self.deny(wt, f"git -C {self.repo} merge feat", "半自动模式")  # 那边的当前分支是主分支

    def test_CODEX关掉不影响半自动_写法的规矩也还在(self):
        off = {"AGENT_DEV_TEAM_CODEX": "off"}
        self.commit(self.repo, "feat")  # 没审核的提交：审核关关了就不查
        self.deny(self.repo, "git merge feat", "半自动模式", env=off)
        self.deny(self.repo, "git push origin feat:main", "半自动模式", env=off)
        self.allow(self.repo, "git push -u origin feat", env=off)
        self.deny(self.repo, "cd / && git push origin feat", "请单独运行", env=off)  # 不然看不出推到哪
        self.deny(self.repo, "git push --all", "看不懂 git push 的选项", env=off)

    def test_没有远端的本地仓库_合并不受限(self):
        local = self.make_repo("local")
        self.commit(local, "feat")
        self.deny(local, "git merge feat", "还没通过 Codex 审核")
        self.reviewed(local, "feat")
        self.allow(local, "git merge feat")

    def test_查不出主分支是哪个_拦(self):
        repo = self.make_repo("trunk-repo", branch="trunk")
        bare = os.path.join(self.tmp, "trunk.git")
        self.git(self.tmp, "init", "-q", "--bare", "-b", "trunk", bare)
        self.git(repo, "remote", "add", "origin", bare)
        self.git(repo, "push", "-q", "-u", "origin", "trunk")
        self.deny(repo, "git push origin trunk", "查不出这个仓库的主分支")
        self.git(repo, "remote", "set-head", "origin", "trunk")
        self.deny(repo, "git push origin trunk", "半自动模式：不直接推 trunk")

    def test_全自动不拦(self):
        self.set_state(auto="full")
        self.allow(self.repo, "git merge feat")
        self.allow(self.repo, "git push")

    def test_项目配置默认半自动(self):
        self.write(os.path.join(self.repo, ".claude", "agent-dev-team.json"), '{"default_auto": "semi"}')
        self.deny(self.repo, "git merge feat", "半自动模式", sid="s2")
        self.set_state(sid="s3", auto="full")
        self.allow(self.repo, "git merge feat", sid="s3")

    def test_只对负责人(self):
        self.set_state(role="normal", auto="semi")
        self.allow(self.repo, "git merge feat")
        self.allow(self.repo, "git push")

    def test_半自动下用gh合并PR一律拦(self):
        for cmd in ("gh pr merge 123 --squash", "gh pr merge", "rtk proxy gh pr merge 1",
                    "gh api -X PUT repos/o/r/pulls/1/merge", "bash -c 'gh pr merge 1'", "cd / && gh pr merge 1"):
            self.deny(self.repo, cmd, "用户自己点合并")
            self.deny(self.repo, cmd, "用户自己点合并", env={"AGENT_DEV_TEAM_CODEX": "off"})
        plain = self.make_dir("plain")
        self.deny(plain, "gh pr merge 1 -R o/r", "用户自己点合并")  # 不在仓库里也拦
        self.allow(self.repo, "gh pr view 123")
        self.allow(self.repo, "gh pr create --head feat --fill")

    def test_半自动_上游是主分支的任务分支_不带分支推也拦(self):
        self.git(self.repo, "branch", "-q", "--set-upstream-to=origin/main", "feat")
        wt = os.path.join(self.tmp, "wt-semi")
        self.git(self.repo, "worktree", "add", "-q", wt, "feat")
        self.deny(wt, "git push", "没有同名的上游分支")
        self.git(self.repo, "config", "push.default", "upstream")  # 这样 git push origin feat 会推到 main
        self.deny(wt, "git push origin feat", "仓库配置了")
        self.deny(wt, "git push origin feat:main", "半自动模式：不直接推 main")


class LeadSkillCommandsTest(GuardCase):
    """lead 技能里教负责人用的每一条上传命令，审核通过后都能正常放行；技能里不该再教守卫会拦的写法。"""

    def setUp(self):
        super().setUp()
        self.repo = self.make_repo_with_remote()
        self.git(self.repo, "remote", "set-head", "origin", "main")
        with open(os.path.join(SCRIPTS, "..", "..", "skills", "lead", "SKILL.md"), encoding="utf-8") as f:
            self.skill = f.read()
        self.git(self.repo, "checkout", "-q", "-b", "feat")
        self.change(self.repo, "feat.txt", "1\n")
        self.git(self.repo, "checkout", "-q", "main")

    def taught(self, *templates):
        for t in templates:
            self.assertIn(t, self.skill, t)

    def test_全自动_合并_推送(self):
        self.taught("`git merge <分支>`", "`git push`", "`git push -u origin <主分支>`", "`git merge --abort`")
        self.deny(self.repo, "git merge feat", "还没通过 Codex 审核")
        self.reviewed(self.repo, "feat")
        self.allow(self.repo, "git merge feat")
        self.git(self.repo, "merge", "-q", "--no-ff", "--no-edit", "feat")
        self.allow(self.repo, "git push")
        self.allow(self.repo, "git push -u origin main")
        self.allow(self.repo, "git merge --abort")
        for cmd in ("git worktree add -b task ../wt-task", "git worktree remove ../wt-task", "git branch -d feat",
                    "git pull", "git init"):
            self.allow(self.repo, cmd)

    def test_半自动_推任务分支_开PR_rebase后换名重开(self):
        self.taught("`git push -u origin <分支>`", "`gh pr create --head <分支>`", "`git pull`",
                    "`git branch <分支>-v2 <分支>`", "`git push -u origin <分支>-v2`",
                    "`gh pr create --head <分支>-v2`", "`gh pr close <旧 PR 号>`")
        self.set_state(auto="semi")
        self.deny(self.repo, "git push -u origin feat", "还没通过 Codex 审核")
        self.reviewed(self.repo, "feat")
        self.deny(self.repo, "git merge feat", "半自动模式")
        self.allow(self.repo, "git push -u origin feat")
        self.git(self.repo, "push", "-q", "-u", "origin", "feat")
        self.allow(self.repo, "gh pr create --head feat")
        self.allow(self.repo, "git pull")
        # 主分支前进了，子代理 rebase：提交号变了，重审后换个名字推上去重开 PR
        self.git(self.repo, "push", "-q", "origin", self.git(self.repo, "commit-tree", "main^{tree}", "-p", "main",
                                                             "-m", "别人的提交") + ":main")
        self.git(self.repo, "pull", "-q", "--ff-only")
        self.git(self.repo, "rebase", "-q", "main", "feat")
        self.git(self.repo, "checkout", "-q", "main")
        self.deny(self.repo, "git push -u origin feat", "还没通过 Codex 审核")
        self.reviewed(self.repo, "feat")
        self.deny(self.repo, "git push --force-with-lease origin feat", "不能强推")
        self.allow(self.repo, "git branch feat-v2 feat")
        self.git(self.repo, "branch", "feat-v2", "feat")
        self.allow(self.repo, "git push -u origin feat-v2")
        self.allow(self.repo, "gh pr create --head feat-v2")
        self.allow(self.repo, "gh pr close 1")

    def test_技能里不再教守卫会拦的写法(self):
        for bad in ("--force", "gh pr merge <", "&& git push", "&& git merge", "cd <"):
            self.assertNotIn(bad, self.skill.replace("不用 `gh pr merge`", ""), bad)
        self.assertIn("单独运行", self.skill)


class LeadBlacklistWrapTest(unittest.TestCase):
    """负责人的黑名单：剥掉包装器再查，bash -c 这类里面的命令也查。"""

    def test_负责人黑名单也剥包装器_也查bash_c(self):
        for cmd, word in (("env -u X rm y", "`rm`"), ("timeout 5 rm x", "`rm`"), ("rtk proxy rm x", "`rm`"),
                          ("time -p cp a b", "`cp`"), ("bash -c 'rm x'", "`rm`"),
                          ("sh -c \"echo a > b\"", "重定向"), ("eval 'git commit -m x'", "git commit"),
                          ("bash <<EOF\nrm x\nEOF", "`rm`"), ("sudo -u bob touch x", "`touch`"),
                          ("nice -n 1 git add .", "git add"), ("noglob rm x", "`rm`"),
                          ("echo [[ && rm x", "`rm`"), ("echo $(( $(rm x) ))", "`rm`"),
                          ("cat <<EOF\n$(rm x)\nEOF", "`rm`"), ("RM x", "`rm`"), ("BASH -c 'rm x'", "`rm`"),
                          ("{rm x}", "`rm`"), ("=rm x", "`rm`"), ("echo $(cat <<EOF)\nrm x\nEOF", "`rm`")):
            reason = check_lead_bash(cmd)
            self.assertIsNotNone(reason, cmd)
            self.assertIn(word, reason, cmd)
        for cmd in ("bash -c 'ls -la'", "bash script.sh", "sh -c 'git status'", "python3 - <<EOF\nrm = 1\nEOF",
                    "git push 2>&1", "ls 2>/dev/null", "echo 2 3"):
            self.assertIsNone(check_lead_bash(cmd), cmd)


if __name__ == "__main__":
    unittest.main(verbosity=2)
