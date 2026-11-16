#!/usr/bin/env python3
"""agent-dev-team 审核：调 Codex 审一个分支的改动，通过就记一个审核单元（见 records.py）。
审核的是从基准到分支顶端这一整段的最终结果，所以通过只证明「整段一起上」没问题。
输出第一行是结论。退出码：通过或没有改动 0，不通过 1，
出错（不是仓库、找不到分支、找不到审核命令、超时）2，出错不计入失败轮数。
"""
import argparse
import os
import shlex
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import git, project_config, repo_root  # noqa: E402
import records  # noqa: E402

DEFAULT_CMD = "codex exec --sandbox read-only -c model_reasoning_effort=max"
TIMEOUT = 1800
MAX_LINES = 60
VERDICTS = {"VERDICT: PASS": "PASS", "VERDICT: FAIL": "FAIL"}


def verdicts_in(out):
    """输出里的结论：整行只有 VERDICT: PASS 或 VERDICT: FAIL（首尾可以有空白）才算，按出现顺序返回。
    行里还有别的字（引用提示词、加粗、举例）都不算。"""
    return [VERDICTS[ln.strip()] for ln in out.splitlines() if ln.strip() in VERDICTS]


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
        "有「严重」问题就不通过。最后单独一行输出结论：VERDICT: PASS 或 VERDICT: FAIL\n"
        "结论那一行只写这几个字，不加粗、不加引号、不加别的符号。"
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
    # 开跑前把基准和分支固定成提交号：审核期间分支上再加的提交不会被算进这次通过
    shas = []
    for ref in (base, a.branch):
        sha = git(cwd, "rev-parse", "--verify", "--quiet", ref + "^{commit}")
        if not sha:
            print(f"审核：出错（不计入失败轮数）\n找不到分支或提交 {ref}")
            return 2
        shas.append(sha)
    base_sha, branch_sha = shas

    commits = git(cwd, "rev-list", f"{base_sha}..{branch_sha}")
    # 有没有改动直接比两棵文件树（分叉点的和分支顶端的），不看 git diff 的输出：diff 的输出会被仓库配置影响
    fork = git(cwd, "merge-base", base_sha, branch_sha)
    trees = [git(cwd, "rev-parse", "--verify", "--quiet", f"{c}^{{tree}}") for c in (fork, branch_sha)] if fork else []
    if commits is None or not fork or not all(trees):
        print(f"审核：出错（不计入失败轮数）\ngit 查不出 {base}...{a.branch} 的改动")
        return 2
    commits = commits.split()
    if trees[0] == trees[1]:
        if commits:
            # 有提交但合起来没改任何文件（空提交、改了又撤销）：没什么可审，记成一个审核单元，免得守卫一直拦。
            # 单元只证明整段一起上没问题：只上传其中一部分（只推「改了」不推「撤销」）守卫照样拦。
            records.add_unit(cwd, branch_sha, commits, base_sha)
            records.clear_fails(cwd, a.branch)
            print(f"审核：没有改动\n{len(commits)} 个提交合起来没有改任何文件，已记为通过（要整段一起上传）。")
        else:
            print("审核：没有改动")
        return 0
    rng = f"{base_sha}...{branch_sha}"

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
        records.add_unit(cwd, branch_sha, commits, base_sha)  # 开跑前固定的那一段
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
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001  脚本自己出错时给出明确结论，不让负责人当成别的结果
        print('审核：出错（不计入失败轮数）' + "\n脚本自己出错：" + repr(e))
        sys.exit(2)
