#!/usr/bin/env python3
"""0.2.2：上线检查分开报告「测试」和「部署」。只有测试通过、没有部署记录不算部署成功。
运行：python3 -m unittest tests.test_deploy_022 -v
"""
import contextlib
import io
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_scripts import RepoCase, deploy_check, sh  # noqa: E402,F401

OK_RUN = {"total_count": 1, "check_runs": [{"id": 1, "name": "ci", "status": "completed", "conclusion": "success"}]}
BAD_RUN = {"total_count": 1, "check_runs": [{"id": 1, "name": "unit", "status": "completed", "conclusion": "failure"}]}
NO_RUN = {"total_count": 0, "check_runs": []}
NO_STATUS = {"state": "pending", "statuses": [], "total_count": 0}
OK_STATUS = {"state": "success", "total_count": 1, "statuses": [{"context": "ci/test", "state": "success"}]}
DEP = [{"id": 7, "environment": "production"}]


class Deploy022(RepoCase):
    def setUp(self):
        super().setUp()
        sh(self.repo, "remote", "add", "origin", "git@github.com:own/proj.git")
        self.sha = sh(self.repo, "rev-parse", "HEAD")
        orig = (deploy_check.have_gh, deploy_check.gh_api)
        self.addCleanup(lambda: setattr(deploy_check, "have_gh", orig[0]))
        self.addCleanup(lambda: setattr(deploy_check, "gh_api", orig[1]))
        deploy_check.have_gh = lambda: True

    def run_check(self, runs, deps, dep_state=None, commit_status=NO_STATUS):
        def api(cwd, path):
            if "/check-runs" in path:
                return runs
            if f"/commits/{self.sha}/status" in path:
                return commit_status
            if "/statuses" in path:
                return [{"state": dep_state}] if dep_state else []
            if "/deployments" in path:
                return deps
            return None
        deploy_check.gh_api = api
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = deploy_check.main(["--cwd", self.repo, "--interval", "0", "--max-poll", "0"])
        return code, buf.getvalue()

    def test_只有检查成功_没有部署记录_无法判断(self):
        code, out = self.run_check(OK_RUN, [])
        self.assertEqual(code, 3, out)
        lines = out.splitlines()
        self.assertEqual(lines[0], "上线检查：无法判断")
        self.assertIn("测试：通过", lines)
        self.assertIn("部署：没有记录", lines)
        self.assertIn("测试通过，部署未确认", out)
        self.assertNotIn("部署都成功", out)

    def test_只有提交状态成功_没有部署记录_无法判断(self):
        code, out = self.run_check(NO_RUN, [], commit_status=OK_STATUS)
        self.assertEqual(code, 3, out)
        self.assertEqual(out.splitlines()[0], "上线检查：无法判断")
        self.assertIn("测试通过，部署未确认", out)

    def test_有部署记录且成功_通过(self):
        code, out = self.run_check(OK_RUN, DEP, "success")
        self.assertEqual(code, 0, out)
        lines = out.splitlines()
        self.assertEqual(lines[0], "上线检查：通过")
        self.assertIn("测试：通过", lines)
        self.assertIn("部署：成功", lines)

    def test_没有测试但部署成功_通过(self):
        code, out = self.run_check(NO_RUN, DEP, "success")
        self.assertEqual(code, 0, out)
        self.assertIn("测试：没有", out.splitlines())
        self.assertIn("部署：成功", out.splitlines())

    def test_部署失败_不通过(self):
        for state in ("failure", "error"):
            code, out = self.run_check(OK_RUN, DEP, state)
            self.assertEqual(code, 1, out)
            lines = out.splitlines()
            self.assertEqual(lines[0], "上线检查：不通过")
            self.assertIn("测试：通过", lines)
            self.assertIn("部署：失败", lines)

    def test_检查失败_不通过(self):
        code, out = self.run_check(BAD_RUN, DEP, "success")
        self.assertEqual(code, 1, out)
        lines = out.splitlines()
        self.assertEqual(lines[0], "上线检查：不通过")
        self.assertIn("测试：不通过", lines)
        self.assertIn("部署：成功", lines)
        # 没有部署记录时检查失败也是不通过，不是无法判断
        code, out = self.run_check(BAD_RUN, [])
        self.assertEqual(code, 1, out)
        self.assertIn("部署：没有记录", out.splitlines())

    def test_提交状态失败_不通过(self):
        bad = {"state": "failure", "total_count": 1, "statuses": [{"context": "ci", "state": "failure"}]}
        code, out = self.run_check(OK_RUN, DEP, "success", commit_status=bad)
        self.assertEqual(code, 1, out)
        self.assertIn("测试：不通过", out.splitlines())

    def test_什么都没有_无法判断(self):
        code, out = self.run_check(NO_RUN, [])
        self.assertEqual(code, 3, out)
        self.assertEqual(out.splitlines()[0], "上线检查：无法判断")
        self.assertIn("测试：没有", out.splitlines())
        self.assertIn("部署：没有记录", out.splitlines())

    def test_配了上线后检查命令_含义不变(self):
        self.write(".claude/agent-dev-team.json", '{"deploy_check": {"cmd": "exit 0"}}')
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = deploy_check.main(["--cwd", self.repo, "--wait", "0", "--interval", "0", "--retries", "0"])
        self.assertEqual(code, 0)
        self.assertEqual(buf.getvalue().splitlines()[0], "上线检查：通过")


if __name__ == "__main__":
    unittest.main()
