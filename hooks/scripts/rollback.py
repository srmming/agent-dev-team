#!/usr/bin/env python3
"""agent-dev-team 回滚：按给的顺序 git revert 提交；撤回之后的内容和某个被撤回提交之前一模一样时，
记一个审核单元（起点是那个「之前」的提交）。可选接着推送。
只撤回代码，不碰数据库。输出第一行是结论。退出码：完成 0，失败 1。
"""
import argparse
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import git, repo_root, tail  # noqa: E402
import records  # noqa: E402

# 仓库里没做完的操作：<git 目录>下有这些文件或目录就是还在进行中
BUSY = (("REVERT_HEAD", "revert"), ("CHERRY_PICK_HEAD", "cherry-pick"), ("MERGE_HEAD", "merge"),
        ("rebase-merge", "rebase"), ("rebase-apply", "rebase 或 am"), ("sequencer", "revert 或 cherry-pick"))


def run_git(cwd, *args):
    """跑 git，返回 (退出码, 合并后的输出)。"""
    try:
        r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, errors="replace",
                           timeout=300)
    except Exception as e:  # noqa: BLE001
        return 1, str(e)
    return r.returncode, ((r.stdout or "") + (r.stderr or "")).strip()


def in_progress(cwd):
    """仓库里有没有没做完的 revert / cherry-pick / merge / rebase。有就返回它的名字，没有返回 None。"""
    for name, label in BUSY:
        p = git(cwd, "rev-parse", "--git-path", name)
        if p and os.path.exists(os.path.join(cwd, p)):
            return label
    return None


def fail(msg, done, recorded=False):
    print("回滚：失败")
    print(msg)
    if done:
        note = "已经记进审核记录" if recorded else "没有记进审核记录，推送前要先审核"
        print(f"已经做好的回滚提交（还在本地，{note}）：" + " ".join(s[:12] for s in done))
    return 1


def push_args(cwd):
    """回滚后推送用的参数。和守卫的上传关同一套规则：守卫看不懂的、有没审核提交的，这里也不推。
    返回 (不推的原因, None) 或 (None, 传给推送命令的参数)。审核关关掉时不查，照平常那样推。"""
    if os.environ.get("AGENT_DEV_TEAM_CODEX", "").lower() == "off":
        return None, ["--no-follow-tags"]
    import guard  # 放这里导入，回滚的其他部分不依赖它
    plan = guard.push_plan(cwd, ["--no-follow-tags"])  # 仓库开了 push.followTags 也能推：自己带上，不顺带推标签
    if isinstance(plan, str):
        return plan, None
    missing = guard.uncovered(cwd, f"{plan['base']}..{plan['tip']}")
    if missing is None:
        return "git 查不出要推哪些提交。", None
    if missing:
        return (f"要推的提交里有 {len(missing)} 个没通过审核（{' '.join(s[:12] for s in missing[:5])}）。"
                "先处理这些提交再推。"), None
    # 两边都写明：推什么、推到哪不受仓库配置影响
    return None, ["--no-follow-tags", plan["remote"], f"{plan['tip']}:refs/heads/{plan['target']}"]


def main(argv=None):
    ap = argparse.ArgumentParser(description="回滚：按顺序 git revert 给的提交（合并提交用 -m 1），只撤回代码。")
    ap.add_argument("--commits", nargs="+", required=True, help="要撤回的提交号，按给的顺序撤回")
    ap.add_argument("--cwd", default=".", help="仓库路径，默认当前目录")
    ap.add_argument("--push", action="store_true", help="回滚后接着 git push")
    a = ap.parse_args(argv)

    cwd = os.path.abspath(a.cwd)
    if not os.path.isdir(cwd) or not repo_root(cwd):
        return fail(f"{cwd} 不是 git 仓库", [])

    # 仓库里已经有没做完的操作时不动手：这里失败后要 git revert --abort，不能把用户原来的操作中止掉
    busy = in_progress(cwd)
    if busy:
        return fail(f"仓库里有没做完的 {busy}，回滚没有动手。先把它做完或中止（比如 git {busy.split()[0]} --abort），再重新回滚。", [])

    # 先把所有提交号都确认一遍，有一个不对就一个都不动
    targets = []
    for c in a.commits:
        sha = git(cwd, "rev-parse", "--verify", "--quiet", c + "^{commit}")
        if not sha:
            return fail(f"找不到提交 {c}", [])
        if git(cwd, "merge-base", "--is-ancestor", sha, "HEAD") is None:
            return fail(f"提交 {c} 不在当前分支的历史里，不能在这里撤回。", [])
        parents = (git(cwd, "rev-list", "--parents", "-n", "1", sha) or "").split()[1:]
        targets.append((sha, parents))

    done = []
    for sha, parents in targets:
        args = ["revert", "--no-edit"] + (["-m", "1"] if len(parents) > 1 else []) + [sha]
        code, out = run_git(cwd, *args)
        if code != 0:
            run_git(cwd, "revert", "--abort")  # 开头查过没有别的操作在进行，这里中止的只会是自己这次
            # 只撤回了一部分：停在一个没人审核过的中间状态，不记进审核记录
            return fail(f"撤回 {sha[:12]} 失败：\n{tail(out)}", done)
        done.append(git(cwd, "rev-parse", "HEAD"))
    # 撤回之后的内容，和某个被撤回提交之前的内容一模一样：等于回到了那个状态，记一个审核单元，
    # 起点就是那个「之前」的提交。那个状态本身通过了（比如早就推上去了），守卫才认这个单元。
    # 对不上（只撤回了中间的一段）就不记：这是个没人审过的新状态，推送前要先审核。
    tree = git(cwd, "rev-parse", "HEAD^{tree}")
    fork = next((ps[0] for _, ps in targets if ps and tree and git(cwd, "rev-parse", ps[0] + "^{tree}") == tree), None)
    if fork:
        records.add_unit(cwd, done[-1], fork, (git(cwd, "rev-list", f"{fork}..HEAD") or done[-1]).split(),
                         rollback=True)

    if a.push:
        # 守卫看不到脚本里的 git push，这里用守卫同一套规则自己查一遍
        reason, args = push_args(cwd)
        if reason:
            return fail("不推送：" + reason, done, recorded=bool(fork))
        code, out = run_git(cwd, "push", *args)
        if code != 0:
            return fail(f"git push 失败：\n{tail(out)}", done, recorded=bool(fork))

    print("回滚：完成" + ("（已推送）" if a.push else ""))
    print("回滚提交：" + " ".join(s[:12] for s in done))
    if not fork:
        print("注意：撤回之后的内容不等于任何一个被撤回提交之前的状态（只撤回了中间的一段），"
              "没有记进审核记录，推送前要先审核。")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # noqa: BLE001  脚本自己出错时给出明确结论，不让负责人当成别的结果
        print('回滚：失败' + "\n脚本自己出错：" + repr(e))
        sys.exit(1)
