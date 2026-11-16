#!/usr/bin/env python3
"""agent-dev-team 验收：查子代理改动有没有越界，跑完成标准命令。
输出第一行是结论。退出码：通过 0，不通过 1，参数或 git 出错 2。
"""
import argparse
import fnmatch
import os
import signal
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import git, repo_root  # noqa: E402

TAIL = 30


def default_branch(cwd):
    """默认主分支：origin/HEAD 指向的分支，没有就找本地的 main、master。"""
    head = git(cwd, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
    if head:
        return head[len("origin/"):] if head.startswith("origin/") else head
    for b in ("main", "master"):
        if git(cwd, "rev-parse", "--verify", "--quiet", "refs/heads/" + b):
            return b
    return None


def run_shell(cmd, cwd, timeout):
    """用 shell 跑命令，返回 (退出码或 None 表示超时, 合并后的输出)。超时会杀掉整个进程组。"""
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
        return None, out or ""


def tail(text, n=TAIL):
    lines = text.rstrip().splitlines()
    return "\n".join(lines[-n:])


def main(argv=None):
    ap = argparse.ArgumentParser(description="验收：查改动范围、跑完成标准命令，只打印结论。")
    ap.add_argument("--branch", required=True, help="要验收的分支")
    ap.add_argument("--base", help="基准分支，默认是主分支")
    ap.add_argument("--cwd", default=".", help="工作副本路径，默认当前目录")
    ap.add_argument("--allow", action="append", default=[],
                    help="允许改的文件范围（通配符，相对仓库根），可写多个；不给就不查范围")
    ap.add_argument("--cmd", action="append", default=[], help="完成标准命令，可写多个，在 --cwd 里用 shell 跑")
    ap.add_argument("--timeout", type=int, default=900, help="每条命令的超时秒数，默认 900")
    a = ap.parse_args(argv)

    cwd = os.path.abspath(a.cwd)
    if not os.path.isdir(cwd) or not repo_root(cwd):
        print(f"验收：出错\n{cwd} 不是 git 仓库")
        return 2
    base = a.base or default_branch(cwd)
    if not base:
        print("验收：出错\n找不到主分支，请用 --base 指定")
        return 2
    for ref in (base, a.branch):
        if not git(cwd, "rev-parse", "--verify", "--quiet", ref + "^{commit}"):
            print(f"验收：出错\n找不到分支或提交 {ref}")
            return 2
    diff = git(cwd, "diff", "--name-only", f"{base}...{a.branch}")
    if diff is None:
        print(f"验收：出错\ngit diff {base}...{a.branch} 失败")
        return 2
    files = [f for f in diff.splitlines() if f]

    reasons = []
    if a.allow:
        outside = [f for f in files if not any(fnmatch.fnmatch(f, p) for p in a.allow)]
        if outside:
            shown = outside[:TAIL]
            more = f"\n……另有 {len(outside) - len(shown)} 个" if len(outside) > len(shown) else ""
            reasons.append("改了范围外的文件：\n" + "\n".join("  " + f for f in shown) + more)

    # 命令按顺序跑，遇到第一条失败就停，省时间
    for c in a.cmd:
        code, out = run_shell(c, cwd, a.timeout)
        if code == 0:
            continue
        why = f"超时（{a.timeout} 秒）" if code is None else f"退出码 {code}"
        body = tail(out)
        reasons.append(f"命令失败（{why}）：{c}" + ("\n--- 输出最后 30 行 ---\n" + body if body else ""))
        break

    if reasons:
        print("验收：不通过")
        print("\n".join(reasons))
        return 1
    print("验收：通过")
    print(f"改了 {len(files)} 个文件")
    return 0


if __name__ == "__main__":
    sys.exit(main())
