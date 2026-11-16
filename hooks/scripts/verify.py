#!/usr/bin/env python3
"""agent-dev-team 验收：查子代理改动有没有越界，跑完成标准命令。
输出第一行是结论。退出码：通过 0，不通过 1，参数或 git 出错 2。
"""
import argparse
import fnmatch
import os
import re
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import TAIL, default_branch, git, repo_root, run_shell, tail  # noqa: E402

# 测试文件：在 tests/、test/、__tests__/ 目录下，或文件名像 test_*.py、*_test.py、*.test.*、*.spec.*
TEST_DIRS = {"tests", "test", "__tests__"}
TEST_NAMES = ("test_*.py", "*_test.py", "*.test.*", "*.spec.*")
# 断言行（Python 的 assert / self.assertXxx / assertRaises / pytest.raises，JS/TS 的 expect( / assert）
ASSERT_RE = re.compile(r"assert|pytest\.raises|expect\(")
# 跳过标记
SKIP_RE = re.compile(r"@unittest\.skip|\bskipIf\b|\bskipUnless\b|\.skipTest\(|@pytest\.mark\.(skip|xfail)"
                     r"|pytest\.skip\(|\.skip\(|\.only\(|\bxit\(|\bxtest\(|\bxdescribe\(")


def is_test_file(path):
    parts = path.split("/")
    return bool(TEST_DIRS & set(parts[:-1])) or any(fnmatch.fnmatch(parts[-1], p) for p in TEST_NAMES)


def file_lines(cwd, ref, path):
    """ref 里这个文件的各行；文件不存在返回 None，读不出来（比如不是文本）返回空列表。"""
    if git(cwd, "cat-file", "-e", f"{ref}:{path}") is None:
        return None
    text = git(cwd, "show", f"{ref}:{path}")
    return text.splitlines() if text is not None else []


def touched_tests(cwd, old, new, files):
    """查改动文件里「已有测试」被动过的地方：删了测试文件、删或改了断言、新加跳过标记。返回问题列表。"""
    found = []
    for f in files:
        if not is_test_file(f):
            continue
        before = file_lines(cwd, old, f)
        if before is None:  # 新建的测试文件随便写
            continue
        after = file_lines(cwd, new, f)
        if after is None:
            found.append(f"{f}：已有测试文件被删")
            continue
        # 删掉的断言行在新内容里找不到一模一样的行才算（去掉首尾空白比，原样挪位置不算）
        left = Counter(x.strip() for x in after)
        for i, line in enumerate(before, 1):
            key = line.strip()
            if not ASSERT_RE.search(key):
                continue
            if left[key] > 0:
                left[key] -= 1
            else:
                found.append(f"{f}:{i}（原文件）断言被删或被改：{key}")
        # 新加的跳过标记：新内容里比原来多出来的那几行
        had = Counter(x.strip() for x in before if SKIP_RE.search(x))
        for i, line in enumerate(after, 1):
            key = line.strip()
            if not SKIP_RE.search(key):
                continue
            if had[key] > 0:
                had[key] -= 1
            else:
                found.append(f"{f}:{i} 新加了跳过标记：{key}")
    return found


def main(argv=None):
    ap = argparse.ArgumentParser(description="验收：查改动范围、跑完成标准命令，只打印结论。")
    ap.add_argument("--branch", required=True, help="要验收的分支")
    ap.add_argument("--base", help="基准分支，默认是主分支")
    ap.add_argument("--cwd", default=".", help="工作副本路径，默认当前目录")
    ap.add_argument("--allow", action="append", default=[],
                    help="允许改的文件范围（通配符，相对仓库根），可写多个；不给就不查范围")
    ap.add_argument("--cmd", action="append", default=[], help="完成标准命令，可写多个，在 --cwd 里用 shell 跑")
    ap.add_argument("--timeout", type=int, default=900, help="每条命令的超时秒数，默认 900")
    ap.add_argument("--allow-test-change", action="store_true",
                    help="派活单明确允许改已有测试时用：跳过断言检查")
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

    reasons, notes = [], []
    if a.allow:
        outside = [f for f in files if not any(fnmatch.fnmatch(f, p) for p in a.allow)]
        if outside:
            shown = outside[:TAIL]
            more = f"\n……另有 {len(outside) - len(shown)} 个" if len(outside) > len(shown) else ""
            reasons.append("改了范围外的文件：\n" + "\n".join("  " + f for f in shown) + more)

    # 改已有测试的断言算不通过（防止为了让测试变绿去改测试）。基准和上面 diff 一样：两边的分叉点
    bad = []
    if a.allow_test_change:
        notes.append("已跳过断言检查（--allow-test-change）")
    elif files:
        fork = git(cwd, "merge-base", base, a.branch)
        if not fork:
            print(f"验收：出错\n找不到 {base} 和 {a.branch} 的分叉点")
            return 2
        bad = touched_tests(cwd, fork, a.branch, files)
        if bad:
            shown = bad[:10]
            more = f"\n……另有 {len(bad) - len(shown)} 条" if len(bad) > len(shown) else ""
            reasons.append("动了已有测试：\n" + "\n".join("  " + x for x in shown) + more
                           + "\n如果派活单明确允许改这些测试，加 --allow-test-change 重跑")

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

    # 命令按顺序跑，遇到第一条失败就停，省时间；动了已有测试就不白跑
    for c in [] if stale or bad else a.cmd:
        code, out = run_shell(c, cwd, a.timeout)
        if code == 0:
            continue
        why = f"超时（{a.timeout} 秒）" if code is None else f"退出码 {code}"
        body = tail(out)
        reasons.append(f"命令失败（{why}）：{c}" + ("\n--- 输出最后 30 行 ---\n" + body if body else ""))
        break

    if reasons:
        print("验收：不通过")
        print("\n".join(reasons + notes))
        return 1
    print("验收：通过")
    print(f"改了 {len(files)} 个文件")
    if notes:
        print("\n".join(notes))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # noqa: BLE001  脚本自己出错时给出明确结论，不让负责人当成别的结果
        print('验收：出错' + "\n脚本自己出错：" + repr(e))
        sys.exit(2)
