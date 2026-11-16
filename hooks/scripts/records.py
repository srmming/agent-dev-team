"""agent-dev-team 审核记录：哪些提交已经通过 Codex 审核，每个分支失败了几轮。"""
import os
import re

from common import state_dir


def _path(cwd, name):
    d = state_dir(cwd)
    return os.path.join(d, name) if d else None


def passed_commits(cwd):
    p = _path(cwd, "passed")
    if not p or not os.path.exists(p):
        return set()
    with open(p) as f:
        return set(f.read().split())


def add_passed(cwd, shas):
    p = _path(cwd, "passed")
    if not p:
        return
    with open(p, "a") as f:
        for s in shas:
            f.write(s + "\n")


def _fails_path(cwd, branch):
    return _path(cwd, "fails-" + re.sub(r"[^A-Za-z0-9_.-]", "_", branch))


def fails(cwd, branch):
    p = _fails_path(cwd, branch)
    if not p or not os.path.exists(p):
        return 0
    try:
        with open(p) as f:
            return int(f.read().strip() or 0)
    except ValueError:
        return 0


def add_fail(cwd, branch):
    n = fails(cwd, branch) + 1
    p = _fails_path(cwd, branch)
    if p:
        with open(p, "w") as f:
            f.write(str(n))
    return n


def clear_fails(cwd, branch):
    p = _fails_path(cwd, branch)
    if p and os.path.exists(p):
        os.remove(p)
