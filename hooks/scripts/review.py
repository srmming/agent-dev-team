#!/usr/bin/env python3
"""agent-dev-team 审核：调 Codex 审一个分支的改动，通过就把提交记进通过记录。
输出第一行是结论。退出码：通过或没有改动 0，不通过 1，
出错（不是仓库、找不到分支、找不到审核命令、超时）2，出错不计入失败轮数。
"""
import argparse
import os
import re
import shlex
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import git, project_config, repo_root  # noqa: E402
import records  # noqa: E402

DEFAULT_CMD = "codex exec --sandbox read-only -c model_reasoning_effort=max"
TIMEOUT = 1800
MAX_LINES = 60


def default_branch(cwd):
    """默认主分支：origin/HEAD 指向的分支，没有就找本地的 main、master。"""
    head = git(cwd, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
    if head:
        return head[len("origin/"):] if head.startswith("origin/") else head
    for b in ("main", "master"):
        if git(cwd, "rev-parse", "--verify", "--quiet", "refs/heads/" + b):
            return b
    return None


def review_cmd(root):
    """审核命令：项目配置 codex_cmd > 环境变量 AGENT_DEV_TEAM_CODEX_CMD > 默认。"""
    cfg = project_config(root) if root else {}
    c = cfg.get("codex_cmd") if isinstance(cfg, dict) else None
    return c or os.environ.get("AGENT_DEV_TEAM_CODEX_CMD") or DEFAULT_CMD


def prompt_for(rng):
    return (
        "你是代码审核员，只审核，不修改任何文件。\n"
        f"在当前仓库运行 `git diff {rng}` 查看这次要合并/上传的改动，必要时阅读相关文件。\n"
        "检查：逻辑错误、会破坏现有功能的改动、安全问题、明显遗漏的测试。不要挑风格问题。\n"
        "用中文按严重程度列出问题（严重 / 一般 / 建议），每条带文件和行号。\n"
        "有「严重」问题就不通过。最后单独一行输出结论：VERDICT: PASS 或 VERDICT: FAIL"
    )


def main(argv=None):
    ap = argparse.ArgumentParser(description="审核：调 Codex 审一个分支的改动，只打印结论和问题清单。")
    ap.add_argument("--branch", required=True, help="要审核的分支")
    ap.add_argument("--base", help="基准分支，默认是主分支")
    ap.add_argument("--cwd", default=".", help="仓库或工作副本路径，默认当前目录")
    a = ap.parse_args(argv)

    cwd = os.path.abspath(a.cwd)
    root = repo_root(cwd) if os.path.isdir(cwd) else None
    if not root:
        print(f"审核：出错（不计入失败轮数）\n{cwd} 不是 git 仓库")
        return 2
    base = a.base or default_branch(cwd)
    if not base:
        print("审核：出错（不计入失败轮数）\n找不到主分支，请用 --base 指定")
        return 2
    for ref in (base, a.branch):
        if not git(cwd, "rev-parse", "--verify", "--quiet", ref + "^{commit}"):
            print(f"审核：出错（不计入失败轮数）\n找不到分支或提交 {ref}")
            return 2

    rng = f"{base}...{a.branch}"
    if not git(cwd, "diff", "--stat", rng):
        print("审核：没有改动")
        return 0

    cmd_text = review_cmd(root)
    try:
        cmd = shlex.split(cmd_text)
    except ValueError:
        cmd = []
    if not cmd:
        print(f"审核：出错（不计入失败轮数）\n审核命令写得不对：{cmd_text}")
        return 2
    try:
        r = subprocess.run(cmd + [prompt_for(rng)], cwd=cwd, capture_output=True, text=True,
                           errors="replace", timeout=TIMEOUT)
    except (FileNotFoundError, PermissionError):
        print(f"审核：出错（不计入失败轮数）\n找不到审核命令 `{cmd[0]}`。安装 Codex 命令行，"
              "或用项目配置 codex_cmd / 环境变量 AGENT_DEV_TEAM_CODEX_CMD 指定。")
        return 2
    except subprocess.TimeoutExpired:
        print(f"审核：出错（不计入失败轮数）\n审核超时（{TIMEOUT} 秒），稍后重试。")
        return 2

    out = (r.stdout or "") + ("\n" + r.stderr if r.returncode != 0 and r.stderr else "")
    verdicts = re.findall(r"VERDICT:\s*(PASS|FAIL)", out)
    if verdicts and verdicts[-1] == "PASS":
        shas = (git(cwd, "rev-list", f"{base}..{a.branch}") or "").split()
        records.add_passed(cwd, shas)
        records.clear_fails(cwd, a.branch)
        print("审核：通过")
        return 0

    n = records.add_fail(cwd, a.branch)
    print(f"审核：不通过（第 {n} 轮）")
    if n >= 2:
        print(f"同一任务已 {n} 轮不过：负责人先自己读一次 diff，再决定重派还是挂起。")
    if not verdicts:
        print("审核没有给出结论，按不通过处理。")
    lines = out.strip().splitlines()
    print("\n".join(lines[-MAX_LINES:]) if lines else "（审核没有输出）")
    return 1


if __name__ == "__main__":
    sys.exit(main())
