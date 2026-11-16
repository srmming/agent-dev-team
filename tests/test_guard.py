#!/usr/bin/env python3
"""守卫测试：负责人的命令行黑名单、各角色的放行与拦截、上传关、半自动。
全部用临时目录和临时 git 仓库；状态目录用 AGENT_DEV_TEAM_HOME 指到临时目录，不碰真实的 ~/.claude。
运行：python3 -m unittest discover -s tests -v
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

SCRIPTS = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "hooks", "scripts"))
sys.path.insert(0, SCRIPTS)
import records  # noqa: E402
from guard import check_lead_bash, has_ship  # noqa: E402

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
    """上传类命令按词判断，不用整串正则。"""

    def test_是上传命令(self):
        for cmd in (
            "git push",
            "git push origin main",
            "git -c a=b push",
            "git -C /x push",
            "git -C /x -c a=b --no-pager merge feat",
            "git merge feat",
            "gh pr create --fill",
            "gh pr merge 12 --squash",
            "cd /x && git push",
            "ls; git push",
            "FOO=1 git push",
            "rtk git push",
            "echo $(git push)",
            'echo "$(git push)"',
            "/usr/bin/git push",
            "if true; then git push; fi",
        ):
            self.assertTrue(has_ship(cmd), cmd)

    def test_不是上传命令(self):
        for cmd in (
            'grep "git push" f',
            "grep 'git merge' -r .",
            "echo git push",
            'git commit -m "之后再 git push"',
            "git log --grep push",
            "git log --merges",
            "git status",
            "git merge-base a b",
            "git pushx",
            "git config push.default simple",
            "git -c push.default=current status",
            "gh pr view 12",
            "gh pr list",
            "gh repo create x",
            "cat <<'EOF'\ngit push\nEOF",
            "ls # git push",
            "echo 'abc",  # 拆不了的命令不拦子代理
        ):
            self.assertFalse(has_ship(cmd), cmd)


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

    def passed(self, repo, *shas):
        records.add_passed(repo, shas)

    def write(self, path, text=""):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)

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
             "ls && git push origin main", 'echo "$(git push)"']

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
                                  "git log --grep push", "gh pr view 1", "echo 'abc"]:
            self.allow(repo, cmd, agent_id="a1")
        for tool, inp in self.edits(repo):
            self.allow(repo, inp, tool=tool, agent_id="a1")

    def test_subagent_窗口子代理_按状态文件认(self):
        repo = self.unreviewed_repo()
        self.set_state(role="subagent")
        self.deny(repo, "git -c a=b push", "子代理不能 push")
        self.deny(repo, "git merge feat", "子代理不能 push")
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
                    "git revert abc", "git merge --abort"]:
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
        self.allow(plain, "git push")  # 不在仓库里，上传关查不了，放行
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


class UploadGateTest(GuardCase):
    """上传关：只对负责人，只查审核通过记录。"""

    def setUp(self):
        super().setUp()
        self.repo = self.make_repo_with_remote()
        self.review_py = os.path.join(SCRIPTS, "review.py")

    def test_merge_提交全在通过记录里才放行(self):
        a = self.commit(self.repo, "feat")
        b = self.commit(self.repo, "feat")
        msg = self.deny(self.repo, "git merge feat", "还没通过 Codex 审核")
        self.assertIn("有 2 个提交", msg)
        self.assertTrue(os.path.isabs(self.review_py))
        self.assertIn(f"python3 {self.review_py} --branch feat --base main --cwd {self.repo}", msg)
        self.passed(self.repo, a)
        self.assertIn("有 1 个提交", self.deny(self.repo, "git merge --no-ff feat -m 'merge feat'"))
        self.passed(self.repo, b)
        self.allow(self.repo, "git merge feat")
        self.allow(self.repo, "git merge --no-ff feat -m 'merge feat'")

    def test_merge_用C或cd指到别的仓库也查得到(self):
        self.commit(self.repo, "feat")
        plain = self.make_dir("plain")
        self.deny(plain, f"git -C {self.repo} merge feat", "审核")
        self.deny(plain, f"cd {self.repo} && git merge feat", "审核")
        self.deny(self.repo, "ls; FOO=1 rtk git -c a=b merge feat", "审核")
        self.deny(self.repo, 'echo "$(git merge feat)"', "审核")

    def test_merge_没有分支参数或分支不存在_放行(self):
        self.commit(self.repo, "feat")
        self.allow(self.repo, "git merge --abort")
        self.allow(self.repo, "git merge --continue")
        self.allow(self.repo, "git merge 不存在的分支")

    def test_push_提交全在通过记录里才放行(self):
        self.allow(self.repo, "git push")  # 没有要上传的提交
        a = self.commit(self.repo)
        b = self.commit(self.repo)
        msg = self.deny(self.repo, "git push", "还没通过 Codex 审核")
        self.assertIn(f"python3 {self.review_py} --branch main --base origin/main --cwd {self.repo}", msg)
        self.deny(self.repo, "git -c a=b push origin main", "审核")
        self.passed(self.repo, a)
        self.deny(self.repo, "git push", "有 1 个提交")
        self.passed(self.repo, b)
        self.allow(self.repo, "git push")
        self.allow(self.repo, "git push origin main")

    def test_push_合并提交不用审(self):
        a = self.commit(self.repo, "feat")
        self.passed(self.repo, a)
        self.git(self.repo, "merge", "-q", "--no-ff", "feat", "-m", "merge feat")
        self.allow(self.repo, "git push")

    def test_push_指定任务分支时查那个分支(self):
        a = self.commit(self.repo, "feat")
        # 当前在 main 上（没有新提交），但要推的是没审核的 feat
        msg = self.deny(self.repo, "git push -u origin feat", "审核")
        self.assertIn("--branch feat --base origin/main", msg)
        self.deny(self.repo, "git push origin feat:feat", "审核")
        self.allow(self.repo, "git push origin :feat")  # 删远端分支
        self.allow(self.repo, "git push origin --delete feat")
        self.passed(self.repo, a)
        self.allow(self.repo, "git push -u origin feat")

    def test_没有上游也没有远端分支_查不了就放行(self):
        local = self.make_repo("local")
        self.commit(local)
        self.allow(local, "git push")
        self.allow(local, "gh pr create --fill")
        # 有远端但还没推过任何东西（第一次推送）
        bare = os.path.join(self.tmp, "empty.git")
        self.git(self.tmp, "init", "-q", "--bare", bare)
        self.git(local, "remote", "add", "origin", bare)
        self.allow(local, "git push -u origin main")

    def test_gh_pr_create和merge(self):
        a = self.commit(self.repo)
        self.deny(self.repo, "gh pr create --fill", "审核")
        self.deny(self.repo, "gh pr merge 12 --squash", "审核")
        self.allow(self.repo, "gh pr view 12")
        self.passed(self.repo, a)
        self.allow(self.repo, "gh pr create --fill")
        self.allow(self.repo, "gh pr merge 12 --squash")
        b = self.commit(self.repo, "feat")
        self.deny(self.repo, "gh pr create --head feat --base main --title x --body y", "--branch feat")
        self.passed(self.repo, b)
        self.allow(self.repo, "gh pr create --head feat --base main --title x --body y")

    def test_CODEX关掉时跳过上传关(self):
        self.commit(self.repo)
        self.commit(self.repo, "feat")
        off = {"AGENT_DEV_TEAM_CODEX": "off"}
        for cmd in ("git push", "git merge feat", "gh pr create --fill", "gh pr merge 1"):
            self.deny(self.repo, cmd, "审核")
            self.allow(self.repo, cmd, env=off)
        # 关的只是上传关，写的命令照拦
        self.deny(self.repo, "rm x", "`rm`", env=off)

    def test_守卫自己不跑Codex_也不写通过记录(self):
        self.commit(self.repo)
        marker = os.path.join(self.tmp, "codex-ran")
        env = {"AGENT_DEV_TEAM_CODEX_CMD": f"touch {marker}"}
        self.deny(self.repo, "git push", "审核", env=env)
        self.assertFalse(os.path.exists(marker))
        self.assertEqual(records.passed_commits(self.repo), set())
        self.deny(self.repo, "git push", "审核", env=env)  # 再来一次还是拦，不会自己变通过

    def test_通过记录按仓库存_工作副本共用(self):
        a = self.commit(self.repo, "feat")
        wt = os.path.join(self.tmp, "wt")
        self.git(self.repo, "worktree", "add", "-q", wt, "feat")
        self.passed(wt, a)
        self.allow(self.repo, "git merge feat")


class SemiAutoTest(GuardCase):
    """半自动：有远端时不直接合并进默认分支、不直接推默认分支。"""

    def setUp(self):
        super().setUp()
        self.repo = self.make_repo_with_remote()
        self.feat = self.commit(self.repo, "feat")
        self.passed(self.repo, self.feat)
        self.set_state(auto="semi")

    def test_拦合并进默认分支(self):
        msg = self.deny(self.repo, "git merge feat", "半自动模式")
        self.assertIn("开 PR", msg)
        self.deny(self.repo, "git merge --no-ff feat", "半自动模式")
        self.allow(self.repo, "git merge --abort")

    def test_拦在默认分支上推送(self):
        self.deny(self.repo, "git push", "半自动模式")
        self.deny(self.repo, "git push origin main", "半自动模式")
        self.deny(self.repo, "git push origin HEAD", "半自动模式")
        self.deny(self.repo, "git push origin feat:main", "半自动模式")
        self.deny(self.repo, "git push origin HEAD:refs/heads/main", "半自动模式")
        self.deny(self.repo, "git push --all", "半自动模式")

    def test_推任务分支和开PR放行(self):
        self.allow(self.repo, "git push -u origin feat")
        self.allow(self.repo, "git push origin feat:feat")
        self.allow(self.repo, "git push --tags")
        self.allow(self.repo, "gh pr create --head feat --base main --fill")

    def test_开PR仍要过上传关(self):
        self.commit(self.repo, "feat")
        self.deny(self.repo, "gh pr create --head feat --fill", "还没通过 Codex 审核")
        self.deny(self.repo, "git push -u origin feat", "还没通过 Codex 审核")

    def test_在任务分支的工作副本里合并和推送不受限(self):
        other = self.commit(self.repo, "other")
        self.passed(self.repo, other)
        wt = os.path.join(self.tmp, "wt")
        self.git(self.repo, "worktree", "add", "-q", wt, "feat")
        self.allow(wt, "git merge other")
        self.allow(wt, "git push -u origin feat")
        self.allow(self.repo, f"git -C {wt} merge other")

    def test_CODEX关掉不影响半自动(self):
        self.deny(self.repo, "git merge feat", "半自动模式", env={"AGENT_DEV_TEAM_CODEX": "off"})

    def test_没有远端的本地仓库不受限(self):
        local = self.make_repo("local")
        sha = self.commit(local, "feat")
        self.deny(local, "git merge feat", "还没通过 Codex 审核")
        self.passed(local, sha)
        self.allow(local, "git merge feat")
        self.allow(local, "git push")

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
                    "gh api -X PUT repos/o/r/pulls/1/merge", "bash -c 'gh pr merge 1'"):
            self.deny(self.repo, cmd, "用户自己点合并")
            self.deny(self.repo, cmd, "用户自己点合并", env={"AGENT_DEV_TEAM_CODEX": "off"})
        plain = self.make_dir("plain")
        self.deny(plain, "gh pr merge 1 -R o/r", "用户自己点合并")  # 不在仓库里也拦
        self.allow(self.repo, "gh pr view 123")
        self.allow(self.repo, "gh pr create --head feat --fill")

    def test_半自动_推送跟着上游推到主分支也拦(self):
        self.git(self.repo, "branch", "-q", "--set-upstream-to=origin/main", "feat")
        wt = os.path.join(self.tmp, "wt-semi")
        self.git(self.repo, "worktree", "add", "-q", wt, "feat")
        self.deny(wt, "git push", "不直接推 main")
        self.deny(self.repo, "git push --branches origin", "不直接推 main")


class WrapperBypassTest(GuardCase):
    """包装器、子 shell、bash -c 这类写法都要认出真正的命令（Codex 第 1 轮发现的回归）。"""

    SHIPS = [
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
        "bash -c 'git push'",
        'sh -lc "cd /x && git push"',
        "zsh -ec 'gh pr merge 1'",
        "eval 'git push'",
        "bash <<EOF\ngit push\nEOF",
        "sh -s <<'EOF'\ngit merge feat\nEOF",
        "bash <<< 'git push'",
        "find . -maxdepth 0 -exec git push \\;",
        "git --git-dir=/x/.git push",
        "git --work-tree /x merge feat",
        "! git push",
        "{ git push; }",
        "(git push)",
        "true && (cd /x; git push)",
        "echo `git push`",
        "x=$(git push)",
        "gh api -X PUT repos/o/r/pulls/1/merge",
        "gh api --method=POST repos/o/r/merges -f base=main -f head=x",
        "gh api repos/o/r/git/refs/heads/main -X PATCH -f sha=abc",
        "env FOO=1 bash -c 'env -u X rtk proxy git push'",
    ]
    NOT_SHIPS = [
        "python3 - <<EOF\ngit push\nEOF",
        "cat <<EOF\ngit push\nEOF",
        "git commit -F - <<'EOF'\n说明里提到 git push\nEOF",
        "bash script.sh",
        "bash -c 'echo git push'",
        "gh api repos/o/r/pulls/1",
        "gh api repos/o/r/pulls/1/merge",
        "env",
        "command -v git",
        "timeout 30 ls",
        "time -p ls",
    ]

    def test_子代理认得出包装后的上传命令(self):
        for cmd in self.SHIPS:
            self.assertTrue(has_ship(cmd), cmd)
        for cmd in self.NOT_SHIPS:
            self.assertFalse(has_ship(cmd), cmd)
        repo = self.make_repo()
        self.set_state(role="subagent")
        for cmd in ("env -u TEST git push origin main", "rtk proxy git push origin main", "bash -c 'git push'"):
            self.deny(repo, cmd, "子代理不能 push")
        self.set_state(role="kuaixiu")
        self.deny(repo, "timeout 30 git push", "对接窗口不能 push")

    def test_负责人黑名单也剥包装器_也查bash_c(self):
        for cmd, word in (("env -u X rm y", "`rm`"), ("timeout 5 rm x", "`rm`"), ("rtk proxy rm x", "`rm`"),
                          ("time -p cp a b", "`cp`"), ("bash -c 'rm x'", "`rm`"),
                          ("sh -c \"echo a > b\"", "重定向"), ("eval 'git commit -m x'", "git commit"),
                          ("bash <<EOF\nrm x\nEOF", "`rm`"), ("sudo -u bob touch x", "`touch`"),
                          ("nice -n 1 git add .", "git add")):
            reason = check_lead_bash(cmd)
            self.assertIsNotNone(reason, cmd)
            self.assertIn(word, reason, cmd)
        for cmd in ("bash -c 'ls -la'", "bash script.sh", "sh -c 'git status'", "python3 - <<EOF\nrm = 1\nEOF"):
            self.assertIsNone(check_lead_bash(cmd), cmd)


class UploadGateMoreTest(GuardCase):
    """上传关的其他写法：推全部分支、推标签、通配符、GIT_DIR、夹带改动的合并提交、看不懂的 cd。"""

    def setUp(self):
        super().setUp()
        self.repo = self.make_repo_with_remote()

    def test_包装器和bash_c里的上传也查审核(self):
        self.commit(self.repo)
        for cmd in ("env -u X git push", "rtk proxy git push", "bash -c 'git push'", "timeout 9 git push"):
            self.deny(self.repo, cmd, "还没通过 Codex 审核")

    def test_推全部分支_推标签_通配符(self):
        a = self.commit(self.repo, "feat")
        self.deny(self.repo, "git push --all origin", "审核")
        self.git(self.repo, "tag", "v1", a)
        self.deny(self.repo, "git push --tags", "审核")
        self.deny(self.repo, "git push origin 'refs/heads/*:refs/heads/*'", "通配符")
        self.passed(self.repo, a)
        self.allow(self.repo, "git push --all origin")
        self.allow(self.repo, "git push --tags")

    def test_GIT_DIR和git_dir指到仓库也查(self):
        self.commit(self.repo)
        plain = self.make_dir("plain")
        gitdir = os.path.join(self.repo, ".git")
        self.deny(plain, f"GIT_DIR={gitdir} git push", "审核")
        self.deny(plain, f"git --git-dir={gitdir} push", "审核")
        self.deny(plain, f"env GIT_DIR={gitdir} git push", "审核")

    def test_看不懂的cd后面的上传_拦下要求写绝对路径(self):
        self.commit(self.repo)
        plain = self.make_dir("plain")
        self.deny(plain, 'cd "$REPO" && git push', "git -C <绝对路径>")
        self.deny(plain, f"pushd {self.repo} && git push", "审核")

    def test_夹带改动的合并提交要审_干净的合并不用(self):
        a = self.commit(self.repo, "feat")
        self.passed(self.repo, a)
        # 手搓一个合并提交：父提交都审过，但内容多了一个文件
        self.write(os.path.join(self.repo, "evil.txt"), "x")
        self.git(self.repo, "add", "evil.txt")
        tree = self.git(self.repo, "write-tree")
        self.git(self.repo, "rm", "-q", "--cached", "evil.txt")
        os.remove(os.path.join(self.repo, "evil.txt"))
        m = self.git(self.repo, "commit-tree", tree, "-p", "main", "-p", "feat", "-m", "merge")
        self.git(self.repo, "update-ref", "refs/heads/evil", m)
        self.deny(self.repo, "git merge evil", "有 1 个提交")
        self.passed(self.repo, m)
        self.allow(self.repo, "git merge evil")
        # 干净的合并（两边自动合出来的）不用审
        self.git(self.repo, "merge", "-q", "--no-ff", "feat", "-m", "merge feat")
        self.allow(self.repo, "git push")

    def test_守卫一秒内完成(self):
        import time
        for _ in range(20):
            self.commit(self.repo)
        start = time.monotonic()
        self.deny(self.repo, "git push", "审核")
        self.assertLess(time.monotonic() - start, 2.0)  # 留余量给慢机器，正常远小于 1 秒


if __name__ == "__main__":
    unittest.main(verbosity=2)
