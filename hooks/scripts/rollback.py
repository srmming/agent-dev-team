#!/usr/bin/env python3
"""agent-dev-team 回滚：按给的顺序 git revert 提交，新回滚提交记进通过记录，可选接着推送。
只撤回代码，不碰数据库。输出第一行是结论。退出码：完成 0，失败 1。
"""
import argparse
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import git, repo_root  # noqa: E402
import records  # noqa: E402

TAIL = 30


def run_git(cwd, *args):
    """跑 git，返回 (退出码, 合并后的输出)。"""
    try:
        r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, errors="replace",
                           timeout=300)
    except Exception as e:  # noqa: BLE001
        return 1, str(e)
    return r.returncode, ((r.stdout or "") + (r.stderr or "")).strip()


def tail(text, n=TAIL):
    return "\n".join(text.splitlines()[-n:])


def fail(msg, done):
    print("回滚：失败")
    print(msg)
    if done:
        print("已经做好的回滚提交（还在本地）：" + " ".join(s[:12] for s in done))
    return 1


def unreviewed_to_push(cwd):
    """git push 会推上去、又没通过审核的提交（和守卫的上传关同一个算法）。关掉审核关或查不了时返回 []。"""
    if os.environ.get("AGENT_DEV_TEAM_CODEX", "").lower() == "off":
        return []
    import guard  # 用守卫同一套算法；放这里导入，回滚的其他部分不依赖它
    base = guard.upload_base(cwd, "HEAD")
    if not base:
        return []  # 纯本地仓库或第一次推送，和守卫一样查不了
    missing = guard.unreviewed(cwd, f"{base}..HEAD", records.passed_commits(cwd))
    return ["（git 查不出要推的提交）"] if missing is None else missing


def main(argv=None):
    ap = argparse.ArgumentParser(description="回滚：按顺序 git revert 给的提交（合并提交用 -m 1），只撤回代码。")
    ap.add_argument("--commits", nargs="+", required=True, help="要撤回的提交号，按给的顺序撤回")
    ap.add_argument("--cwd", default=".", help="仓库路径，默认当前目录")
    ap.add_argument("--push", action="store_true", help="回滚后接着 git push")
    a = ap.parse_args(argv)

    cwd = os.path.abspath(a.cwd)
    if not os.path.isdir(cwd) or not repo_root(cwd):
        return fail(f"{cwd} 不是 git 仓库", [])

    # 先把所有提交号都确认一遍，有一个不对就一个都不动
    targets = []
    for c in a.commits:
        sha = git(cwd, "rev-parse", "--verify", "--quiet", c + "^{commit}")
        if not sha:
            return fail(f"找不到提交 {c}", [])
        parents = (git(cwd, "rev-list", "--parents", "-n", "1", sha) or "").split()
        targets.append((sha, len(parents) > 2))

    done = []
    for sha, is_merge in targets:
        args = ["revert", "--no-edit"] + (["-m", "1"] if is_merge else []) + [sha]
        code, out = run_git(cwd, *args)
        if code != 0:
            run_git(cwd, "revert", "--abort")
            if done:
                records.add_passed(cwd, done)
            return fail(f"撤回 {sha[:12]} 失败：\n{tail(out)}", done)
        done.append(git(cwd, "rev-parse", "HEAD"))
    # 回滚提交回到的是已审核过的状态，记进通过记录，推送时不用再审
    records.add_passed(cwd, done)

    if a.push:
        # 守卫看不到脚本里的 git push，这里自己查一遍：要推的提交里有没审核的就不推
        missing = unreviewed_to_push(cwd)
        if missing:
            return fail(f"不推送：要推的提交里有 {len(missing)} 个没通过审核（{' '.join(s[:12] for s in missing[:5])}）。"
                        "回滚提交已经在本地，先处理这些提交再推。", done)
        code, out = run_git(cwd, "push")
        if code != 0:
            return fail(f"git push 失败：\n{tail(out)}", done)

    print("回滚：完成" + ("（已推送）" if a.push else ""))
    print("回滚提交：" + " ".join(s[:12] for s in done))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001  脚本自己出错时给出明确结论，不让负责人当成别的结果
        print('回滚：失败' + "\n脚本自己出错：" + repr(e))
        sys.exit(1)
