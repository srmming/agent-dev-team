#!/usr/bin/env python3
"""agent-dev-team 验收：查子代理改动有没有越界，跑完成标准命令。
输出第一行是结论。退出码：通过 0，不通过 1，参数或 git 出错 2。
"""
import argparse
import fnmatch
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import TAIL, default_branch, git, repo_root, run_shell, tail  # noqa: E402

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
    # --no-renames：改名时旧路径也算改动（不然把范围外的文件改名挪进范围里查不出来）；-z：中文文件名不被转义
    diff = git(cwd, "diff", "--name-only", "--no-renames", "-z", f"{base}...{a.branch}")
    if diff is None:
        print(f"验收：出错\ngit diff {base}...{a.branch} 失败")
        return 2
    files = [f for f in diff.split("\0") if f]

    reasons = []
    if a.allow:
        outside = [f for f in files if not any(fnmatch.fnmatch(f, p) for p in a.allow)]
        if outside:
            shown = outside[:TAIL]
            more = f"\n……另有 {len(outside) - len(shown)} 个" if len(outside) > len(shown) else ""
            reasons.append("改了范围外的文件：\n" + "\n".join("  " + f for f in shown) + more)

    # 命令在 --cwd 当前的代码上跑：要正好是被验收分支的顶端、没有没提交的改动，不然测的不是要验收的东西
    stale = None
    if a.cmd:
        head = git(cwd, "rev-parse", "--verify", "--quiet", "HEAD^{commit}")
        tip = git(cwd, "rev-parse", "--verify", "--quiet", a.branch + "^{commit}")
        dirty = git(cwd, "status", "--porcelain")
        if head != tip:
            stale = (f"{cwd} 当前的提交 {(head or '?')[:12]} 不是 {a.branch} 的顶端 {tip[:12]}，"
                     f"完成标准命令没跑。在 {a.branch} 的工作副本里验收。")
        elif dirty != "":
            stale = f"{cwd} 有没提交的改动（或者 git status 查不了），完成标准命令没跑。先提交或清掉再验收。"
        reasons += [stale] if stale else []

    # 命令按顺序跑，遇到第一条失败就停，省时间
    for c in [] if stale else a.cmd:
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
    try:
        sys.exit(main())
    except Exception as e:  # noqa: BLE001  脚本自己出错时给出明确结论，不让负责人当成别的结果
        print('验收：出错' + "\n脚本自己出错：" + repr(e))
        sys.exit(2)
