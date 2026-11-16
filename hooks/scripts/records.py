"""agent-dev-team 审核记录：哪些「审核单元」通过了 Codex 审核，每个分支失败了几轮。

一个审核单元 = 一次通过的审核，存成 reviewed.jsonl 里的一行：
{"tip": 分支顶端提交号, "commits": [基准到顶端这一段的全部提交号], "base": 基准提交号, "at": 时间}
审核看的是这一整段的最终结果，所以一个单元只证明「整段一起上」没问题，不证明其中某个提交单独上没问题。
守卫怎么用这些单元，见 guard.uncovered。

旧格式的 passed 文件（一行一个提交号）已经作废：不读、不写，留着也不出错。
"""
import json
import os
import re
import time

from common import state_dir

UNITS_FILE = "reviewed.jsonl"
SHA_RE = re.compile(r"^[0-9a-f]{40,64}$")


def _path(cwd, name):
    d = state_dir(cwd)
    return os.path.join(d, name) if d else None


def units(cwd):
    """读出全部审核单元：[{"tip": 提交号, "commits": 提交号集合}]。读不懂的行跳过，不算通过。"""
    p = _path(cwd, UNITS_FILE)
    out = []
    if not p or not os.path.exists(p):
        return out
    with open(p, encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                u = json.loads(line)
            except ValueError:
                continue
            if not isinstance(u, dict):
                continue
            tip, commits = u.get("tip"), u.get("commits")
            if not isinstance(tip, str) or not isinstance(commits, list) or tip not in commits:
                continue
            if all(isinstance(c, str) and SHA_RE.match(c) for c in commits):
                out.append({"tip": tip, "commits": set(commits)})
    return out


def add_unit(cwd, tip, commits, base=""):
    """记一个审核单元。commits 是这一段的全部提交号，必须包含 tip。"""
    commits = list(commits)
    if tip not in commits:
        raise ValueError("审核单元的提交里必须有顶端提交")
    p = _path(cwd, UNITS_FILE)
    if not p:
        return
    line = json.dumps({"tip": tip, "commits": commits, "base": base,
                       "at": time.strftime("%Y-%m-%dT%H:%M:%S%z")})
    with open(p, "a", encoding="utf-8") as f:
        try:
            import fcntl
            fcntl.flock(f, fcntl.LOCK_EX)  # 几个审核同时写也不会把两行搅在一起
        except Exception:
            pass  # 没有 fcntl（比如 Windows）就不加锁
        f.write(line + "\n")


def has_legacy(cwd):
    """仓库里还留着旧格式的 passed 文件（已作废）。"""
    p = _path(cwd, "passed")
    return bool(p and os.path.exists(p))


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
