#!/usr/bin/env python3
"""rollback.py --push 在仓库开了 push.followTags 时也能推，且不会把本地标签推上去。"""
import os
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_scripts as ts  # noqa: E402  只借用 RepoCase 和 sh，不把它的测试再收一遍


class RollbackPushFollowTags(ts.RepoCase):
    def test_开了followTags也能推_标签不上远端(self):
        bare = os.path.join(self.tmp.name, "remote.git")
        subprocess.run(["git", "init", "-q", "--bare", bare], check=True)
        ts.sh(self.repo, "remote", "add", "origin", bare)
        ts.sh(self.repo, "config", "push.followTags", "true")
        bad = self.commit_change("src/a.py", "bad\n")
        ts.sh(self.repo, "push", "-q", "--no-follow-tags", "-u", "origin", "main")
        ts.sh(self.repo, "tag", "-a", "-m", "v", "v1.0", bad)  # 带注解的标签：followTags 会顺带推它
        code, out = self.run_script("rollback.py", "--cwd", self.repo, "--commits", bad, "--push")
        self.assertEqual(code, 0, out)
        self.assertEqual(ts.sh(bare, "rev-parse", "main"), ts.sh(self.repo, "rev-parse", "HEAD"))
        self.assertNotEqual(ts.sh(bare, "rev-parse", "main"), bad)
        self.assertEqual(ts.sh(bare, "tag", "--list"), "")

    def commit_change(self, rel, text):
        self.write(rel, text)
        return self.commit("change " + rel)


if __name__ == "__main__":
    unittest.main()
