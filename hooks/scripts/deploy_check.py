#!/usr/bin/env python3
"""agent-dev-team 上线检查：推送后确认线上是不是正常。
1. 项目配置有 deploy_check.cmd：等一会儿后跑它，失败就重试。
2. 没配命令、远端是 GitHub、本机有 gh：查这个提交的检查结果和部署状态。
3. 都没有：无法判断。
输出第一行是结论。退出码：通过 0，不通过 1，无法判断 3。
"""
import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import git, project_config, repo_root  # noqa: E402

TAIL = 30
CMD_TIMEOUT = 300
CHECK_OK = {"success", "neutral", "skipped"}
DEPLOY_OK = {"success", "inactive"}
DEPLOY_BAD = {"failure", "error"}


def tail(text, n=TAIL):
    return "\n".join(text.rstrip().splitlines()[-n:])


def run_shell(cmd, cwd, timeout=CMD_TIMEOUT):
    p = subprocess.Popen(cmd, shell=True, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True, errors="replace", start_new_session=True)
    try:
        out, _ = p.communicate(timeout=timeout)
        return p.returncode, out or ""
    except subprocess.TimeoutExpired:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except OSError:
            pass
        out, _ = p.communicate()
        return None, (out or "") + f"\n（超时 {timeout} 秒）"


# ---- GitHub 查询。测试里会替换 have_gh 和 gh_api ----

def have_gh():
    return shutil.which("gh") is not None


def gh_api(cwd, path):
    """调 `gh api <path>`，返回解析后的 JSON；失败返回 None。"""
    try:
        r = subprocess.run(["gh", "api", path], cwd=cwd, capture_output=True, text=True, timeout=60)
    except Exception:
        return None
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout)
    except ValueError:
        return None


def github_repo(cwd):
    """远端 origin 是 GitHub 时返回 'owner/repo'，否则 None。"""
    url = git(cwd, "remote", "get-url", "origin") or ""
    m = re.search(r"github\.com[:/]+([^/]+)/([^/]+?)(?:\.git)?/?$", url)
    return f"{m.group(1)}/{m.group(2)}" if m else None


def github_status(cwd, repo, sha):
    """查一次。返回 (状态, 说明行)，状态是 ok / fail / running / none。"""
    """查一次。返回 (状态, 说明行)，状态是 ok / fail / running / none。
    同名检查、同一环境的部署可能有好几次（重跑、重新部署），每个只看最新一次。
    任何一项查询失败都不报通过：确定有失败报 fail，否则报 none（无法判断）。"""
    bad, running, unknown, seen = [], [], [], 0
    runs = gh_api(cwd, f"repos/{repo}/commits/{sha}/check-runs?per_page=100")
    if not isinstance(runs, dict) or not isinstance(runs.get("check_runs"), list):
        unknown.append("查不到检查结果（gh 查询失败）")
        runs = {"check_runs": []}
    elif isinstance(runs.get("total_count"), int) and runs["total_count"] > len(runs["check_runs"]):
        unknown.append(f"检查有 {runs['total_count']} 个，只查到 {len(runs['check_runs'])} 个")
    for c in _latest(runs["check_runs"], "name", "started_at"):
        seen += 1
        name = c.get("name", "?")
        if c.get("status") != "completed":
            running.append(f"检查 {name} 还在跑")
        elif c.get("conclusion") not in CHECK_OK:
            bad.append(f"检查 {name}：{c.get('conclusion')}")
    deps = gh_api(cwd, f"repos/{repo}/deployments?sha={sha}&per_page=100")
    if not isinstance(deps, list):
        unknown.append("查不到部署记录（gh 查询失败）")
        deps = []
    for d in _latest(deps, "environment", "created_at"):
        seen += 1
        env = d.get("environment", "?")
        sts = gh_api(cwd, f"repos/{repo}/deployments/{d.get('id')}/statuses?per_page=1")
        if not isinstance(sts, list):
            unknown.append(f"查不到部署 {env} 的状态（gh 查询失败）")
            continue
        state = sts[0].get("state") if sts and isinstance(sts[0], dict) else "pending"
        if state in DEPLOY_BAD:
            bad.append(f"部署 {env}：{state}")
        elif state not in DEPLOY_OK:
            running.append(f"部署 {env} 状态 {state}")
    if bad:
        return "fail", bad
    if unknown:
        return "none", unknown
    if running:
        return "running", running
    if seen:
        return "ok", []
    return "none", ["GitHub 上这个提交没有检查结果，也没有部署记录"]


def _latest(items, key, time_key):
    """按 key 分组，每组只留最新的一个（先比时间，再比 id）。"""
    best = {}
    for it in items:
        if not isinstance(it, dict):
            continue
        k = it.get(key, "?")
        rank = (str(it.get(time_key) or ""), it.get("id") if isinstance(it.get("id"), int) else 0)
        if k not in best or rank > best[k][0]:
            best[k] = (rank, it)
    return [it for _, it in best.values()]


def _num(value, default, cast):
    """项目配置里的数字：写错了（不是数字或是负数）就用默认值。"""
    try:
        n = cast(value)
    except (TypeError, ValueError):
        return default
    return n if n >= 0 else default


def check_by_cmd(cwd, dc, a):
    cmd = dc["cmd"]
    wait = a.wait if a.wait is not None else _num(dc.get("wait_seconds", 60), 60, float)
    interval = a.interval if a.interval is not None else _num(dc.get("interval_seconds", 30), 30, float)
    retries = a.retries if a.retries is not None else _num(dc.get("retries", 3), 3, int)
    if wait > 0:
        time.sleep(wait)
    out, code = "", None
    for i in range(retries + 1):
        if i and interval > 0:
            time.sleep(interval)
        code, out = run_shell(cmd, cwd)
        if code == 0:
            print("上线检查：通过")
            if i:
                print(f"第 {i + 1} 次才成功")
            return 0
    print("上线检查：不通过")
    print(f"检查命令试了 {retries + 1} 次都失败：{cmd}")
    body = tail(out)
    if body:
        print(body)
    return 1


def check_by_github(cwd, repo, sha, a):
    interval = a.interval if a.interval is not None else 15
    deadline = time.monotonic() + a.max_poll
    while True:
        state, lines = github_status(cwd, repo, sha)
        if state != "running" or time.monotonic() >= deadline:
            break
        time.sleep(interval)
    if state == "ok":
        print("上线检查：通过")
        print("GitHub 上的检查和部署都成功")
        return 0
    if state == "fail":
        print("上线检查：不通过")
        print("\n".join(lines[:TAIL]))
        return 1
    print("上线检查：无法判断")
    if state == "running":
        print(f"等了 {int(a.max_poll)} 秒还没跑完：")
    print("\n".join(lines[:TAIL]))
    return 3


def main(argv=None):
    ap = argparse.ArgumentParser(description="上线检查：推送后确认线上正常，只打印结论。")
    ap.add_argument("--cwd", default=".", help="仓库路径，默认当前目录")
    ap.add_argument("--sha", default="HEAD", help="要检查的提交，默认 HEAD")
    ap.add_argument("--wait", type=float, help="跑检查命令前先等几秒（覆盖项目配置 wait_seconds）")
    ap.add_argument("--interval", type=float, help="重试或轮询的间隔秒数（覆盖项目配置 interval_seconds）")
    ap.add_argument("--retries", type=int, help="检查命令失败后最多重试几次（覆盖项目配置 retries）")
    ap.add_argument("--max-poll", type=float, default=600, help="查 GitHub 时最长轮询秒数，默认 600")
    a = ap.parse_args(argv)

    cwd = os.path.abspath(a.cwd)
    root = repo_root(cwd) if os.path.isdir(cwd) else None
    if not root:
        print(f"上线检查：无法判断\n{cwd} 不是 git 仓库")
        return 3

    cfg = project_config(root)
    dc = cfg.get("deploy_check") if isinstance(cfg, dict) else None
    if isinstance(dc, dict) and dc.get("cmd"):
        if not isinstance(dc["cmd"], str):
            print("上线检查：无法判断\n项目配置里的 deploy_check.cmd 要写成一条命令文字")
            return 3
        return check_by_cmd(cwd, dc, a)

    repo = github_repo(cwd)
    if repo and have_gh():
        sha = git(cwd, "rev-parse", "--verify", "--quiet", a.sha + "^{commit}")
        if not sha:
            print(f"上线检查：无法判断\n找不到提交 {a.sha}")
            return 3
        return check_by_github(cwd, repo, sha, a)

    print("上线检查：无法判断")
    print("项目配置里没有 deploy_check.cmd，远端也不是 GitHub（或本机没有 gh）")
    return 3


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001  脚本自己出错时给出明确结论，不让负责人当成别的结果
        print('上线检查：无法判断' + "\n脚本自己出错：" + repr(e))
        sys.exit(3)
