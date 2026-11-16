"""agent-dev-team 审核记录：哪些「审核单元」通过了 Codex 审核，每个分支失败了几轮。

一个审核单元 = 一次通过的审核，存成 reviewed.jsonl 里的一行：
{"tip": 顶端提交号, "fork": 起点提交号, "commits": [起点到顶端这一段的全部提交号], "at": 时间}
（rollback.py 记的单元多一个 "rollback": true）
起点是基准和分支的分叉点。审核看的是从起点到顶端这一整段的最终结果，所以一个单元只证明：
起点那个状态没问题的话，顶端那个状态也没问题。它不证明其中某个提交单独上没问题。
守卫怎么用这些单元，见 guard.uncovered。

旧格式的 passed 文件（一行一个提交号）已经作废：不读、不写，留着也不出错。
"""
import hashlib
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
    """读出全部审核单元：[{"tip": 提交号, "fork": 提交号, "commits": 提交号集合, "rollback": 是不是回滚记的}]。
    读不懂的行跳过，不算通过。"""
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
            tip, fork, commits = u.get("tip"), u.get("fork"), u.get("commits")
            if not isinstance(commits, list) or tip not in commits:
                continue
            if all(isinstance(c, str) and SHA_RE.match(c) for c in commits + [fork]):
                out.append({"tip": tip, "fork": fork, "commits": set(commits), "rollback": u.get("rollback") is True})
    return out


def add_unit(cwd, tip, fork, commits, rollback=False):
    """记一个审核单元：从 fork（起点）到 tip（顶端）。commits 是这一段（fork..tip）的全部提交号，必须包含 tip。
    rollback：这是 rollback.py 撤回提交之后记的。"""
    commits = list(commits)
    if tip not in commits or not fork:
        raise ValueError("审核单元要有起点，提交里要有顶端提交")
    p = _path(cwd, UNITS_FILE)
    if not p:
        return
    unit = {"tip": tip, "fork": fork, "commits": commits, "at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    if rollback:
        unit["rollback"] = True
    line = json.dumps(unit)
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


def branch_key(branch):
    """按分支存的文件名用的键：可读前缀 + 完整分支名的哈希。
    只把 / 换成 _ 会让 feat/a 和 feat_a 撞到一起，加上哈希就不会。"""
    prefix = re.sub(r"[^A-Za-z0-9_.-]", "_", branch)[:40]
    return prefix + "-" + hashlib.sha256(branch.encode("utf-8", "surrogatepass")).hexdigest()[:16]


def _fails_path(cwd, branch):
    return _path(cwd, "fails-" + branch_key(branch))


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


# ---------- 上一轮没通过的审核：下一轮只复审增量用 ----------
# last-<分支键>.json：{"fork": 起点, "tip": 上一轮审到的顶端, "issues": 上一轮 Codex 的输出全文,
#                      "brief": 上一轮任务说明的哈希, "key": 上一轮那次审核的缓存键}

def _last_path(cwd, branch):
    return _path(cwd, "last-" + branch_key(branch) + ".json")


def brief_hash(brief):
    """任务说明的哈希（没有说明也有一个值）。last 记录只存哈希，用来判断这一轮的说明和上一轮是不是同一份。"""
    return hashlib.sha256((brief or "").encode("utf-8", "surrogatepass")).hexdigest()


def save_last(cwd, branch, fork, tip, issues, brief="", key=None):
    p = _last_path(cwd, branch)
    if p:
        d = {"fork": fork, "tip": tip, "issues": issues, "brief": brief_hash(brief)}
        if key:
            d["key"] = key
        tmp = f"{p}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False)
        os.replace(tmp, p)


def load_last(cwd, branch):
    """上一轮没通过的那次审核。没有、读不懂返回 None。"""
    try:
        with open(_last_path(cwd, branch), encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        return None
    ok = isinstance(d, dict) and all(isinstance(d.get(k), str) and SHA_RE.match(d[k]) for k in ("fork", "tip"))
    return d if ok and isinstance(d.get("issues"), str) else None


def clear_last(cwd, branch):
    p = _last_path(cwd, branch)
    if p and os.path.exists(p):
        os.remove(p)


# ---------- 审过的结论：同一段改动不再调 Codex ----------
# cache/<键>.json：{"verdict": "PASS" 或 "FAIL", "report": 完整报告路径, "shown": 交给负责人的那段文字, ...}
# 键由 review.py 算：分叉点、顶端提交、提示词版本、审核命令、任务说明、审核方式（全量 / 增量）和实际审的范围一起取哈希。

def _cache_path(cwd, key):
    d = state_dir(cwd)
    if not d or not re.match(r"^[0-9a-f]{64}$", key):
        return None
    os.makedirs(os.path.join(d, "cache"), exist_ok=True)
    return os.path.join(d, "cache", key + ".json")


def load_review(cwd, key):
    """之前审过的结论。没有、读不懂返回 None。"""
    p = _cache_path(cwd, key)
    try:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        return None
    return d if isinstance(d, dict) and d.get("verdict") in ("PASS", "FAIL") and isinstance(d.get("shown"), str) else None


def save_review(cwd, key, rec):
    p = _cache_path(cwd, key)
    if p:
        tmp = f"{p}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(rec, f, ensure_ascii=False)
        os.replace(tmp, p)


def write_report(cwd, branch, n, key, text):
    """把完整报告写进状态目录 reports/<分支键>-<时间>-<缓存键前 12 位>-r<轮次>.md，返回路径（没有状态目录返回 None）。
    轮次会清零重来，不能单独当编号；加上时间和缓存键，再用「文件已存在就失败」的方式新建，
    不同的审核不会写到同一个文件，旧缓存指着的报告也不会被后来的审核覆盖。"""
    d = state_dir(cwd)
    if not d:
        return None
    os.makedirs(os.path.join(d, "reports"), exist_ok=True)
    stem = f"{branch_key(branch)}-{time.strftime('%Y%m%d-%H%M%S')}-{key[:12]}"
    for i in range(1, 1000):
        p = os.path.join(d, "reports", f"{stem}{'' if i == 1 else f'-{i}'}-r{n}.md")
        try:
            with open(p, "x", encoding="utf-8") as f:
                f.write(text)
            return p
        except FileExistsError:
            continue
    raise OSError("报告文件名用完了：" + stem)


def add_usage(cwd, row):
    """追加一行 Codex 用量到 usage.jsonl。"""
    p = _path(cwd, "usage.jsonl")
    if p:
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
