#!/usr/bin/env python3
"""agent-dev-team 审核：调 Codex 审一个分支的改动，通过就记一个审核单元（见 records.py）。
审核的是从基准到分支顶端这一整段的最终结果，所以通过只证明「整段一起上」没问题。
上一轮没通过、之后分支上只是加了提交（起点没变）时，只复审增量：确认上一轮的问题修好了，
再审上一轮以来的改动。增量通过 = 前面全量审过、问题都修了、这次的改动没有新的严重问题，
记的审核单元和全量通过一样（起点到顶端整段）。--full 强制全量。
输出第一行是结论。退出码：通过或没有改动 0，不通过 1，
出错（不是仓库、找不到分支、找不到审核命令、超时）2，出错不计入失败轮数。
"""
import argparse
import os
import shlex
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import default_branch, git, project_config, repo_root  # noqa: E402
import records  # noqa: E402

DEFAULT_CMD = "codex exec --sandbox read-only -c model_reasoning_effort=max -c service_tier=priority"
TIMEOUT = 1800
MAX_LINES = 60
ISSUES_MAX = 20000  # 上一轮的问题清单超过这么多字就不做增量复审（不截断，免得开头的问题丢掉），改全量
VERDICTS = {"VERDICT: PASS": "PASS", "VERDICT: FAIL": "FAIL"}


def verdicts_in(out):
    """输出里的结论：整行只有 VERDICT: PASS 或 VERDICT: FAIL（首尾可以有空白）才算，按出现顺序返回。
    行里还有别的字（引用提示词、加粗、举例）都不算。"""
    lines = [ln.strip(" \t\r") for ln in out.split("\n")]  # 只按换行切：别的分隔符（换页符等）不算一行的开头结尾
    return [VERDICTS[ln] for ln in lines if ln in VERDICTS]


def review_cmd(root):
    """审核命令：项目配置 codex_cmd > 环境变量 AGENT_DEV_TEAM_CODEX_CMD > 默认。"""
    cfg = project_config(root) if root else {}
    c = cfg.get("codex_cmd") if isinstance(cfg, dict) else None
    return c or os.environ.get("AGENT_DEV_TEAM_CODEX_CMD") or DEFAULT_CMD


STANDARD = (
    "检查：逻辑错误、会破坏现有功能的改动、安全问题、明显遗漏的测试。不要挑风格问题。\n"
    "判断标准：\n"
    "- 威胁模型：负责人和子代理都是配合的 AI，守卫防的是手误，不防刻意绕过（和 README「已知限制」一致）。\n"
    "- 「严重」只算：按 README 和技能教的用法，或者常见的普通写法，正常使用就会碰到，并且会导致下面任一后果："
    "结果错误、丢数据、没审过的代码进了主分支或上线、该回滚时不回滚、流程卡死。\n"
    "- 要刻意构造的命令或环境才会触发的（多层嵌套引号、符号链接拼路径、少见的 git 配置组合、别名等），"
    "算「一般」：照样列出来，但不挡合并。\n"
    "用中文按严重程度列出问题（严重 / 一般 / 建议），每条带文件和行号。\n"
    "有「严重」问题就不通过。最后单独一行输出结论：VERDICT: PASS 或 VERDICT: FAIL\n"
    "结论那一行只写这几个字，不加粗、不加引号、不加别的符号。"
)


def prompt_for(rng):
    return (
        "你是代码审核员，只审核，不修改任何文件。\n"
        f"在当前仓库运行 `git diff {rng}` 查看这次要合并/上传的改动，必要时阅读相关文件。\n" + STANDARD
    )


def prompt_incr(prev, tip, issues):
    """增量复审：上一轮审到 prev 没通过，之后加了修复到 tip。"""
    return (
        "你是代码审核员，只审核，不修改任何文件。\n"
        f"这是复审。这个分支上一轮审到 {prev}，没通过，问题清单在最后。之后分支上加了修复，现在到了 {tip}。"
        "不用把整个分支从头再审一遍，做两件事：\n"
        "1. 逐条确认上一轮列出的问题是否已修好，每条写「已修好」或「没修好」并说明。\n"
        f"2. 在当前仓库运行 `git diff {prev}..{tip}` 查看上一轮以来的改动，需要时阅读相关代码，"
        "按下面的标准判断有没有新的严重问题。\n"
        "上一轮的严重问题没修好，也算有严重问题。\n" + STANDARD + "\n\n上一轮的问题清单：\n" + issues
    )


def main(argv=None):
    ap = argparse.ArgumentParser(description="审核：调 Codex 审一个分支的改动，只打印结论和问题清单。")
    ap.add_argument("--branch", required=True, help="要审核的分支")
    ap.add_argument("--base", help="基准分支，默认是主分支")
    ap.add_argument("--cwd", default=".", help="仓库或工作副本路径，默认当前目录")
    ap.add_argument("--full", action="store_true", help="强制全量审（默认：上一轮没通过、之后只加了提交时只复审增量）")
    ap.add_argument("--since", help="手动指定上一轮审到的顶端提交，和 --last-issues 一起用（上一轮没存记录时）")
    ap.add_argument("--last-issues", help="上一轮的问题清单文件，和 --since 一起用")
    a = ap.parse_args(argv)
    if bool(a.since) != bool(a.last_issues) or (a.full and a.since):
        print("审核：出错（不计入失败轮数）\n--since 和 --last-issues 要一起用，不能和 --full 一起用")
        return 2
    issues = None
    if a.last_issues:
        try:
            with open(a.last_issues, encoding="utf-8", errors="replace") as f:
                issues = f.read()
        except OSError as e:
            print(f"审核：出错（不计入失败轮数）\n读不了 --last-issues 文件：{e}")
            return 2

    cwd = os.path.abspath(a.cwd)
    root = repo_root(cwd) if os.path.isdir(cwd) else None
    if not root:
        print(f"审核：出错（不计入失败轮数）\n{cwd} 不是 git 仓库")
        return 2
    base = a.base or default_branch(cwd)
    if not base:
        print("审核：出错（不计入失败轮数）\n找不到主分支，请用 --base 指定")
        return 2
    # 开跑前把基准和分支固定成提交号：审核期间分支上再加的提交不会被算进这次通过
    shas = []
    for ref in (base, a.branch):
        sha = git(cwd, "rev-parse", "--verify", "--quiet", ref + "^{commit}")
        if not sha:
            print(f"审核：出错（不计入失败轮数）\n找不到分支或提交 {ref}")
            return 2
        shas.append(sha)
    base_sha, branch_sha = shas

    # 起点是基准和分支的分叉点。审的、记的都是从起点到分支顶端这一段
    fork = git(cwd, "merge-base", base_sha, branch_sha)
    commits = git(cwd, "rev-list", f"{fork}..{branch_sha}") if fork else None
    # 有没有改动直接比两棵文件树（起点的和分支顶端的），不看 git diff 的输出：diff 的输出会被仓库配置影响
    trees = [git(cwd, "rev-parse", "--verify", "--quiet", f"{c}^{{tree}}") for c in (fork, branch_sha)] if fork else []
    if commits is None or not fork or not all(trees):
        print(f"审核：出错（不计入失败轮数）\ngit 查不出 {base}...{a.branch} 的改动")
        return 2
    commits = commits.split()
    if trees[0] == trees[1]:
        if commits:
            # 有提交但合起来没改任何文件（空提交、改了又撤销）：没什么可审，记成一个审核单元，免得守卫一直拦。
            # 单元只证明「起点没问题的话顶端也没问题」：只上传其中一部分（只推「改了」不推「撤销」），
            # 或者起点本身是个没审过的状态，守卫照样拦。
            records.add_unit(cwd, branch_sha, fork, commits)
            records.clear_fails(cwd, a.branch)
            records.clear_last(cwd, a.branch)
            print(f"审核：没有改动\n{len(commits)} 个提交合起来没有改任何文件，已记为通过（要整段一起上传）。")
        else:
            print("审核：没有改动")
        return 0
    rng = f"{fork}..{branch_sha}"  # 和 base...branch 是同一段改动，写成两个确定的提交号

    # 增量复审的条件：有上一轮的记录（或手动给的 --since），起点没变，上一轮审到的顶端是现在顶端的祖先
    last, prev, why = None, None, ""
    if a.since:
        since = git(cwd, "rev-parse", "--verify", "--quiet", a.since + "^{commit}")
        if not since:
            print(f"审核：出错（不计入失败轮数）\n找不到提交 {a.since}")
            return 2
        last = {"fork": fork, "tip": since, "issues": issues}
    elif not a.full:
        last = records.load_last(cwd, a.branch)
    if last:
        if last["fork"] != fork or git(cwd, "merge-base", "--is-ancestor", fork, last["tip"]) is None:
            why = "起点变了：rebase 过，或者基准换了"
        elif git(cwd, "merge-base", "--is-ancestor", last["tip"], branch_sha) is None:
            why = "上一轮审到的提交不在这个分支现在的历史里"
        elif len(last["issues"]) > ISSUES_MAX:
            why = f"上一轮的问题清单太长（{len(last['issues'])} 字，超过 {ISSUES_MAX}），不截断"
        else:
            prev = last
    if prev:
        prompt, note = prompt_incr(prev["tip"], branch_sha, prev["issues"]), \
            f"增量复审：确认上一轮的问题，审了 {prev['tip'][:12]}..{branch_sha[:12]}"
    else:
        prompt, note = prompt_for(rng), (f"上一轮的记录用不上（{why}），这次全量审。" if why else "")

    cmd_text = review_cmd(root)
    try:
        cmd = shlex.split(cmd_text)
    except ValueError:
        cmd = []
    if not cmd:
        print(f"审核：出错（不计入失败轮数）\n审核命令写得不对：{cmd_text}")
        return 2
    try:
        r = subprocess.run(cmd + [prompt], cwd=cwd, capture_output=True, text=True,
                           errors="replace", timeout=TIMEOUT)
    except (FileNotFoundError, PermissionError):
        print(f"审核：出错（不计入失败轮数）\n找不到审核命令 `{cmd[0]}`。安装 Codex 命令行，"
              "或用项目配置 codex_cmd / 环境变量 AGENT_DEV_TEAM_CODEX_CMD 指定。")
        return 2
    except subprocess.TimeoutExpired:
        print(f"审核：出错（不计入失败轮数）\n审核超时（{TIMEOUT} 秒），稍后重试。")
        return 2

    if r.returncode != 0:
        # 审核命令自己出错（没登录、崩了）：不知道结论，不算通过也不计轮数
        err = ((r.stderr or "") + "\n" + (r.stdout or "")).strip().splitlines()
        print(f"审核：出错（不计入失败轮数）\n审核命令退出码 {r.returncode}，稍后重试。")
        if err:
            print("\n".join(err[-20:]))
        return 2

    out = r.stdout or ""
    verdicts = verdicts_in(out)  # 取最后一个整行结论
    if verdicts and verdicts[-1] == "PASS":
        records.add_unit(cwd, branch_sha, fork, commits)  # 开跑前固定的那一段：从分叉点到分支顶端（增量通过也一样）
        records.clear_fails(cwd, a.branch)
        records.clear_last(cwd, a.branch)
        print("审核：通过" + ("\n" + note if note else ""))
        return 0

    n = records.add_fail(cwd, a.branch)
    if verdicts:  # 拿到有效的 VERDICT: FAIL 才用这一轮覆盖记录（下一轮只复审增量用）；没有结论时原记录一字不动
        records.save_last(cwd, a.branch, fork, branch_sha, out)
    print(f"审核：不通过（第 {n} 轮）" + ("\n" + note if note else ""))
    if n >= 2:
        print(f"同一任务已 {n} 轮不过：负责人先自己读一次 diff，再决定重派还是挂起。")
    if not verdicts:
        print("审核没有给出结论，按不通过处理。")
    lines = out.strip().splitlines()
    print("\n".join(lines[-MAX_LINES:]) if lines else "（审核没有输出）")
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # noqa: BLE001  脚本自己出错时给出明确结论，不让负责人当成别的结果
        print('审核：出错（不计入失败轮数）' + "\n脚本自己出错：" + repr(e))
        sys.exit(2)
