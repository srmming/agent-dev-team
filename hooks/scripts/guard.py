#!/usr/bin/env python3
"""agent-dev-team 守卫。
1. 负责人（你开的对话，没有 agent_id）不能改项目文件，命令行只能跑只读/验收/合并类命令。
2. 子代理和对接窗口可以改文件，但不能 push / 合并。
3. 负责人 push / 合并 / 开 PR 之前，必须通过 Codex 审核。
退出码 2 = 拦截，stderr 会反馈给 Claude。
"""
import json
import os
import re
import shlex
import subprocess
import sys

sys.path.insert(0, os.path.dirname(__file__))
from common import git, repo_root, role, state_dir

EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
SHIP_RE = re.compile(r"\bgit\s+(?:-C\s+\S+\s+)?(push|merge)\b|\bgh\s+pr\s+(create|merge)\b")

LEAD_CMDS = {
    "git", "gh", "ls", "cat", "head", "tail", "grep", "rg", "find", "wc", "pwd", "cd",
    "echo", "printf", "which", "jq", "sort", "uniq", "cut", "tr", "diff", "stat", "file",
    "tree", "date", "true", "test", "[", "codex", "sleep", "env", "timeout", "claude",
    "npm", "pnpm", "yarn", "bun", "npx", "pytest", "python", "python3", "cargo", "go", "make",
}
LEAD_GIT = {
    "status", "diff", "log", "show", "branch", "worktree", "fetch", "rev-parse", "ls-files",
    "blame", "merge", "push", "pull", "remote", "tag", "describe", "shortlog", "rev-list",
    "cat-file", "merge-base",
}


def block(msg):
    print("【Agent 开发团队 拦截】" + msg, file=sys.stderr)
    sys.exit(2)


def tokenize(cmd):
    """把命令拆成 (类型, 文本) 列表：w = 词，o = 引号外的操作符。
    引号里的 | ; > 等只算词的一部分，不算操作符。引号没闭合时抛 ValueError。"""
    toks, buf, has, i, n = [], [], False, 0, len(cmd)

    def flush():
        nonlocal buf, has
        if has:
            toks.append(("w", "".join(buf)))
        buf, has = [], False

    while i < n:
        c = cmd[i]
        if c == "\\" and i + 1 < n:
            if cmd[i + 1] != "\n":  # 反斜杠加换行是续行，直接跳过
                buf.append(cmd[i + 1])
                has = True
            i += 2
        elif c == "'":
            j = cmd.find("'", i + 1)
            if j < 0:
                raise ValueError("单引号没有闭合")
            buf.append(cmd[i + 1:j])
            has = True
            i = j + 1
        elif c == '"':
            j = i + 1
            while j < n and cmd[j] != '"':
                if cmd[j] == "\\" and j + 1 < n:
                    j += 1
                buf.append(cmd[j])
                j += 1
            if j >= n:
                raise ValueError("双引号没有闭合")
            has = True
            i = j + 1
        elif c in " \t\r":
            flush()
            i += 1
        elif c in "\n()":
            flush()
            toks.append(("o", c))
            i += 1
        elif c in ";|&<>":
            flush()
            j = i
            while j < n and cmd[j] in ";|&<>":
                j += 1
            toks.append(("o", cmd[i:j]))
            i = j
        else:
            buf.append(c)
            has = True
            i += 1
    flush()
    return toks


def split_segments(cmd):
    """按引号外的操作符拆成一段段命令。返回 (段列表, 拦截原因或 None)。"""
    toks = tokenize(cmd)
    segs, i = [[]], 0
    while i < len(toks):
        kind, text = toks[i]
        if kind == "w":
            segs[-1].append(text)
            i += 1
        elif ">" in text:
            nxt = toks[i + 1] if i + 1 < len(toks) else ("o", "")
            target = nxt[1] if nxt[0] == "w" else ""
            is_fd_copy = text.endswith(">&") and target.isdigit()  # 例如 2>&1
            if target != "/dev/null" and not is_fd_copy:
                return segs, "负责人不能用命令行写文件（检测到重定向）。把这个改动派给子代理。"
            i += 2
        elif set(text) <= {"<"}:
            i += 1  # 读入重定向，不算写文件
        else:
            segs.append([])  # ; | && || & 换行 括号：后面是另一条命令
            i += 1
    return segs, None


def check_claude(words):
    """负责人只能用 claude 做插件的只读检查：校验、测试、列出。"""
    rest = words[1:]
    if rest[:1] == ["--version"]:
        return None
    if rest[:1] == ["plugin"]:
        if rest[1:2] in (["validate"], ["test"], ["list"]) or rest[1:3] == ["marketplace", "list"]:
            return None
        if "--help" in rest or "-h" in rest:
            return None
    return ("负责人只能运行 `claude plugin validate / test / list`、`claude plugin marketplace list` "
            "和 `--help`。其他 claude 命令请派给子代理。")


def check_lead_bash(cmd):
    try:
        segs, reason = split_segments(cmd)
    except ValueError:
        return "命令里的引号没有闭合，守卫没法检查。改写后再试。"
    if reason:
        return reason
    for words in segs:
        while words and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", words[0]):
            words = words[1:]
        # rtk 是本机的命令包装器（会把 find 改写成 rtk find），按被包装的命令检查
        if len(words) > 1 and os.path.basename(words[0]) == "rtk":
            words = words[1:]
        if not words:
            continue
        first = os.path.basename(words[0])
        if first not in LEAD_CMDS:
            return f"负责人不能运行 `{first}`。负责人只读代码、派活、验收、合并；改动请派给子代理。"
        if first == "claude":
            reason = check_claude(words)
            if reason:
                return reason
        if first == "git":
            rest = [w for w in words[1:]]
            while rest and rest[0] in ("-C", "-c"):
                rest = rest[2:]
            sub = rest[0] if rest else ""
            if sub not in LEAD_GIT:
                return f"负责人不能运行 `git {sub}`（不 commit、不切分支、不改工作区）。派给子代理。"
        if first == "find" and any(w in ("-delete", "-exec", "-execdir") for w in words):
            return "负责人不能用 find 改动或删除文件。"
    return None


def review_range(cwd, cmd):
    """返回 (要审核的 diff 范围, 被审核的提交)。"""
    m = re.search(r"\bgit\s+(?:-C\s+\S+\s+)?merge\b(.*)", cmd)
    if m:
        try:
            args = [a for a in shlex.split(m.group(1).split("&&")[0].split(";")[0]) if not a.startswith("-")]
        except ValueError:
            args = []
        if args:
            return f"HEAD...{args[0]}", args[0]
    base = git(cwd, "rev-parse", "--abbrev-ref", "@{u}")
    if not base:
        head = git(cwd, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
        base = head or next((b for b in ("main", "master") if git(cwd, "rev-parse", "--verify", b)), None)
    cur = git(cwd, "rev-parse", "--abbrev-ref", "HEAD")
    if not base or base == cur:
        return "HEAD~1..HEAD", "HEAD"
    return f"{base}...HEAD", "HEAD"


def codex_review(cwd, cmd):
    if os.environ.get("AGENT_DEV_TEAM_CODEX", "").lower() == "off":
        return
    rng, tip = review_range(cwd, cmd)
    sha = git(cwd, "rev-parse", tip) or tip
    if not git(cwd, "diff", "--stat", rng):
        return  # 没有改动，不用审
    sdir = state_dir(cwd)
    passed = os.path.join(sdir, "passed")
    if os.path.exists(passed) and sha in open(passed).read().split():
        return
    fails_file = os.path.join(sdir, "fails-" + re.sub(r"[^A-Za-z0-9_.-]", "_", tip))
    prompt = (
        "你是代码审核员，只审核，不修改任何文件。\n"
        f"在当前仓库运行 `git diff {rng}` 查看这次要合并/上传的改动，必要时阅读相关文件。\n"
        "检查：逻辑错误、会破坏现有功能的改动、安全问题、明显遗漏的测试。不要挑风格问题。\n"
        "用中文按严重程度列出问题（严重 / 一般 / 建议），每条带文件和行号。\n"
        "有「严重」问题就不通过。最后单独一行输出结论：VERDICT: PASS 或 VERDICT: FAIL"
    )
    base_cmd = shlex.split(os.environ.get("AGENT_DEV_TEAM_CODEX_CMD", "codex exec --sandbox read-only"))
    try:
        r = subprocess.run(base_cmd + [prompt], cwd=cwd, capture_output=True, text=True, timeout=1300)
    except FileNotFoundError:
        block("找不到 codex 命令，无法审核，所以不能 push / 合并。请安装 Codex 命令行，"
              "或用环境变量 AGENT_DEV_TEAM_CODEX_CMD 指定审核命令。")
    except subprocess.TimeoutExpired:
        block("Codex 审核超时。稍后重试；连续超时就把任务挂起并报给 Dot。")
    out = (r.stdout or "") + ("\n" + r.stderr if r.returncode != 0 else "")
    verdicts = re.findall(r"VERDICT:\s*(PASS|FAIL)", out)
    if verdicts and verdicts[-1] == "PASS":
        with open(passed, "a") as f:
            f.write(sha + "\n")
        if os.path.exists(fails_file):
            os.remove(fails_file)
        return
    n = int(open(fails_file).read() or 0) + 1 if os.path.exists(fails_file) else 1
    open(fails_file, "w").write(str(n))
    tail = out.strip()[-4000:] or "（Codex 没有输出）"
    extra = ("没有给出结论，按不通过处理。" if not verdicts else "")
    if n >= 2:
        extra += "同一改动已经 2 轮不过：把任务挂起，记进看板，用 shouwei 技能报给 Dot，继续做别的任务。"
    else:
        extra += "把下面的问题清单派给子代理修，修完再重新 push / 合并。"
    block(f"Codex 审核不通过（第 {n} 轮）。{extra}\n\n{tail}")


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        sys.exit(0)
    r = role(data)
    if r == "none":
        sys.exit(0)
    tool = data.get("tool_name", "")
    inp = data.get("tool_input") or {}
    cwd = data.get("cwd") or os.getcwd()

    if tool == "Bash":
        cmd = inp.get("command", "") or ""
        ship = SHIP_RE.search(cmd)
        if r == "subagent":
            if ship:
                block("子代理不能 push / 合并 / 开 PR。提交到自己的分支，把结果交回负责人。")
            sys.exit(0)
        if r == "kuaixiu":
            if ship:
                block("对接窗口不能 push / 合并。提交到当前分支后上报给负责人，由负责人验收、过 Codex 审核再合并。")
            sys.exit(0)
        reason = check_lead_bash(cmd)
        if reason:
            block(reason)
        if ship:
            codex_review(cwd, cmd)
        sys.exit(0)

    if tool in EDIT_TOOLS and r == "lead":
        path = inp.get("file_path") or inp.get("notebook_path") or ""
        root = repo_root(cwd)
        full = os.path.realpath(path if os.path.isabs(path) else os.path.join(cwd, path))
        if root and (full + os.sep).startswith(os.path.realpath(root) + os.sep):
            block("负责人不写代码，也不直接改项目文件。把这个改动写成派活单，派给子代理；"
                  "如果用户想亲自一起改，用 kuaixiu 技能开对接窗口。")
    sys.exit(0)


if __name__ == "__main__":
    main()
