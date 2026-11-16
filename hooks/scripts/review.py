#!/usr/bin/env python3
"""agent-dev-team 审核：调 Codex 审一个分支的改动，通过就记一个审核单元（见 records.py）。
审核的是从基准到分支顶端这一整段的最终结果，所以通过只证明「整段一起上」没问题。
上一轮没通过、之后分支上只是加了提交（起点没变）时，只复审增量：确认上一轮的问题修好了，
再审上一轮以来的改动。增量通过 = 前面全量审过、问题都修了、这次的改动没有新的严重问题，
记的审核单元和全量通过一样（起点到顶端整段）。--full 强制全量。
Codex 要在分支顶端那份代码里跑才能读对文件：--cwd 正好在顶端就在那里跑；不在顶端时，
换到检出了这个分支的工作副本；哪里都没检出，就建一个临时工作副本。要用的那份有没提交的改动就出错。
完整报告存进状态目录，输出里列全部严重问题和最多 10 条一般问题。
同一段改动（分叉点、顶端、提示词版本、审核命令、任务说明都一样）用 Codex 审过，就复用之前的结论。
--plan 不审分支：让 Codex 只读地评一个做法，第一行「设计：可以做 / 建议换做法」，不写审核记录。
输出第一行是结论。退出码：通过或没有改动 0，不通过 1，
出错（不是仓库、找不到分支、找不到审核命令、超时）2，出错不计入失败轮数。
"""
import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import default_branch, git, project_config, repo_root  # noqa: E402
import records  # noqa: E402

BASE_CMD = "codex exec --sandbox read-only -c service_tier=priority"  # 不含档位，档位按轮次追加在末尾
DEFAULT_CMD = BASE_CMD + " -c model_reasoning_effort=max"  # 第一轮实际跑的默认命令
EFFORT_FULL, EFFORT_INCR, EFFORT_PLAN = "max", "high", "medium"
PROMPT_VERSION = "0.2.2"  # 提示词改了就改这个号，之前审过的结论不再复用
TIMEOUT = 1800
MAX_LINES = 60  # 文字报告不超过这么多行就整份给负责人
GENERAL_MAX = 10  # 一般问题最多列这么多条，其余看完整报告
ISSUES_MAX = 20000  # 上一轮的问题清单超过这么多字就不做增量复审（不截断，免得开头的问题丢掉），改全量
VERDICTS = {"VERDICT: PASS": "PASS", "VERDICT: FAIL": "FAIL"}
DESIGNS = {"DESIGN: OK": "可以做", "DESIGN: CHANGE": "建议换做法"}
LEVELS = ("严重", "一般", "建议")
ERR = "审核：出错（不计入失败轮数）"

# Codex 按这个结构给结论和问题清单（codex exec --output-schema）
SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["verdict", "summary", "issues"],
    "properties": {
        "verdict": {"type": "string", "enum": ["PASS", "FAIL"]},
        "summary": {"type": "string"},
        "issues": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["severity", "title", "file", "line", "detail", "root_cause"],
            "properties": {
                "severity": {"type": "string", "enum": list(LEVELS)},
                "title": {"type": "string"}, "file": {"type": "string"},
                "line": {"type": ["integer", "null"]},
                "detail": {"type": "string"}, "root_cause": {"type": "string"}}}},
    },
}


def verdicts_in(out):
    """输出里的结论：整行只有 VERDICT: PASS 或 VERDICT: FAIL（首尾可以有空白）才算，按出现顺序返回。
    行里还有别的字（引用提示词、加粗、举例）都不算。"""
    lines = [ln.strip(" \t\r") for ln in out.split("\n")]  # 只按换行切：别的分隔符（换页符等）不算一行的开头结尾
    return [VERDICTS[ln] for ln in lines if ln in VERDICTS]


def review_cmd(root):
    """审核命令（不含档位）：项目配置 codex_cmd > 环境变量 AGENT_DEV_TEAM_CODEX_CMD > 默认。"""
    cfg = project_config(root) if root else {}
    c = cfg.get("codex_cmd") if isinstance(cfg, dict) else None
    return c or os.environ.get("AGENT_DEV_TEAM_CODEX_CMD") or BASE_CMD


def split_cmd(cmd_text):
    try:
        return shlex.split(cmd_text)
    except ValueError:
        return []


def is_codex_exec(cmd):
    """是 codex exec 命令：才加 --json、-o、--output-schema（别的命令不一定认这些参数）。"""
    return bool(cmd) and os.path.basename(cmd[0]) == "codex" and "exec" in cmd[1:] and "--json" not in cmd


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

SCAN_ALL = (
    "这一轮要一次扫全：整段改动都看完，把发现的问题全部列出来，不准找到几条就停。"
    "同一类问题合成一条，写清根本原因、判定方法（什么样的代码算这一类）和已知的例子（文件和行号）。\n"
)

STRUCTURED = (
    "\n最终回复按给定的 JSON 结构输出，结论写在 verdict 里（PASS 或 FAIL），不另写 VERDICT 行；"
    "issues 每条写严重程度（严重 / 一般 / 建议）、标题、文件、行号（没有填 null）、说明、根因；"
    "summary 写一两句总评。"
)


def brief_text(brief):
    if not brief:
        return ""
    return ("\n\n负责人给的任务说明（目标、完成标准、已经定下的取舍）。按这个需求判断，"
            "已经定下的取舍不要当问题报：\n" + brief)


def prompt_for(rng, brief=None, structured=False):
    return (
        "你是代码审核员，只审核，不修改任何文件。\n"
        f"在当前仓库运行 `git diff {rng}` 查看这次要合并/上传的改动，必要时阅读相关文件。\n" + SCAN_ALL + STANDARD
        + (STRUCTURED if structured else "") + brief_text(brief)
    )


def prompt_incr(prev, tip, issues, brief=None, structured=False):
    """增量复审：上一轮审到 prev 没通过，之后加了修复到 tip。"""
    return (
        "你是代码审核员，只审核，不修改任何文件。\n"
        f"这是复审。这个分支上一轮审到 {prev}，没通过，问题清单在最后。之后分支上加了修复，现在到了 {tip}。"
        "不用把整个分支从头再审一遍，做两件事：\n"
        "1. 逐条确认上一轮列出的问题是否已修好，每条写「已修好」或「没修好」并说明。\n"
        f"2. 在当前仓库运行 `git diff {prev}..{tip}` 查看上一轮以来的改动，需要时阅读相关代码，"
        "按下面的标准判断有没有新的严重问题。\n"
        "上一轮的严重问题没修好，也算有严重问题。\n"
        "复审只因为两种问题不通过：上一轮的问题没修好；这次新改的代码"
        f"（{prev}..{tip} 的改动）里的严重问题。"
        "上一轮已经审过、这次没动的代码里新挑出来的问题，一律算「一般」，照样列出来，但不挡合并。\n"
        + STANDARD + (STRUCTURED if structured else "") + brief_text(brief)
        + "\n\n上一轮的问题清单：\n" + issues
    )


def prompt_plan(plan):
    return (
        "你是设计评审，只读，不修改任何文件，也不要动手实现。\n"
        "负责人打算用下面的做法完成一个任务。需要时阅读当前仓库的代码，评一下这个做法：\n"
        "1. 能不能做成。\n"
        "2. 主要风险。\n"
        "3. 是不是靠「猜」：比如靠解析去猜 shell 命令或代码的意思。这类做法容易被一轮轮审核找出新漏洞，"
        "如果是，说出不用猜的做法。\n"
        "用中文简短回答。最后单独一行输出结论：DESIGN: OK（可以做）或 DESIGN: CHANGE（建议换做法），"
        "那一行只写这几个字。\n\n做法说明：\n" + plan
    )


class CodexError(Exception):
    """审核命令没跑出结果（找不到、超时、退出码非 0）。文字是给负责人看的说明。"""


def events_of(out):
    evs = []
    for ln in out.splitlines():
        try:
            ev = json.loads(ln)
        except ValueError:
            continue
        if isinstance(ev, dict):
            evs.append(ev)
    return evs


def usage_of(events):
    """--json 事件流里每轮 turn.completed 带的 token 用量，加起来。拿不到返回 None。"""
    total = {}
    for ev in events:
        u = ev.get("usage")
        if ev.get("type") != "turn.completed" or not isinstance(u, dict):
            continue
        for k, v in u.items():
            if isinstance(v, int) and not isinstance(v, bool):
                total[k] = total.get(k, 0) + v
    return total or None


def last_message(events):
    """事件流里最后一条 Codex 的回复。"""
    text = ""
    for ev in events:
        item = ev.get("item")
        if ev.get("type") == "item.completed" and isinstance(item, dict) and item.get("type") == "agent_message" \
                and isinstance(item.get("text"), str):
            text = item["text"]
    return text


def run_codex(cmd_text, effort, prompt, cwd, schema=None):
    """跑审核命令，末尾追加档位。返回 (Codex 的最终回复, 用量或 None)。
    codex exec 会加 --json、-o（和 --output-schema）；别的命令照旧只看标准输出。"""
    cmd = split_cmd(cmd_text)
    if not cmd:
        raise CodexError(f"审核命令写得不对：{cmd_text}")
    use_json = is_codex_exec(cmd)
    cmd += ["-c", f"model_reasoning_effort={effort}"]  # 后面的 -c 覆盖前面的
    with tempfile.TemporaryDirectory(prefix="adt-review-") as tmp:
        last = os.path.join(tmp, "last-message.txt")
        if use_json:
            cmd += ["--json", "-o", last]
            if schema:
                sp = os.path.join(tmp, "schema.json")
                with open(sp, "w", encoding="utf-8") as f:
                    json.dump(schema, f, ensure_ascii=False)
                cmd += ["--output-schema", sp]
        try:
            r = subprocess.run(cmd + [prompt], cwd=cwd, capture_output=True, text=True,
                               errors="replace", timeout=TIMEOUT)
        except (FileNotFoundError, PermissionError):
            raise CodexError(f"找不到审核命令 `{cmd[0]}`。安装 Codex 命令行，"
                             "或用项目配置 codex_cmd / 环境变量 AGENT_DEV_TEAM_CODEX_CMD 指定。")
        except subprocess.TimeoutExpired:
            raise CodexError(f"审核超时（{TIMEOUT} 秒），稍后重试。")
        if r.returncode != 0:
            # 审核命令自己出错（没登录、崩了）：不知道结论，不算通过也不计轮数
            err = ((r.stderr or "") + "\n" + (r.stdout or "")).strip().splitlines()
            raise CodexError("\n".join([f"审核命令退出码 {r.returncode}，稍后重试。"] + err[-20:]))
        out = r.stdout or ""
        if not use_json:
            return out, None
        try:
            with open(last, encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError:
            text = ""
        events = events_of(out)
        if not text.strip():
            text = last_message(events) if events else out
        return text, usage_of(events)


USAGE_NAMES = (("input_tokens", "输入"), ("cached_input_tokens", "其中缓存"),
               ("output_tokens", "输出"), ("reasoning_output_tokens", "其中推理"))


def usage_line(u):
    """「Codex 用量：约 N token（…）」。N 是输入加输出；拿到哪些分项列哪些。"""
    if "input_tokens" in u or "output_tokens" in u:
        n = u.get("input_tokens", 0) + u.get("output_tokens", 0)
    else:
        n = sum(u.values())
    known = dict(USAGE_NAMES)
    parts = [f"{label} {u[k]}" for k, label in USAGE_NAMES if k in u] + [f"{k} {v}" for k, v in u.items() if k not in known]
    return f"Codex 用量：约 {n} token（{'，'.join(parts)}）"


def log_usage(cwd, u, branch, rnd, tip, effort):
    """打印用量，追加一行到 usage.jsonl。拿不到用量就什么都不做。"""
    if not u:
        return
    print(usage_line(u))
    try:
        records.add_usage(cwd, {"at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "branch": branch, "round": rnd,
                                "tip": tip, "effort": effort, **u})
    except OSError:
        pass


# ---------- 结论和问题清单 ----------

def structured(text):
    """按 SCHEMA 给的 JSON 结论。不是这个结构返回 None（改按文字解析）。有严重问题一律算不通过。"""
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\n|\n?```$", "", t)
    try:
        d = json.loads(t)
    except ValueError:
        return None
    if not isinstance(d, dict) or d.get("verdict") not in ("PASS", "FAIL") or not isinstance(d.get("issues"), list):
        return None
    issues = []
    for i in d["issues"]:
        if isinstance(i, dict):
            issues.append({"severity": i.get("severity") if i.get("severity") in LEVELS else "一般",
                           **{k: "" if i.get(k) is None else str(i.get(k))
                              for k in ("title", "file", "line", "detail", "root_cause")}})
    verdict = "FAIL" if any(i["severity"] == "严重" for i in issues) else d["verdict"]
    return {"verdict": verdict, "summary": str(d.get("summary") or ""), "issues": issues}


def issue_lines(n, i):
    where = i["file"] + (f":{i['line']}" if i["line"] else "")
    out = [f"{n}. {i['title']}" + (f"（{where}）" if where else "")]
    if i["detail"]:
        out.append("   说明：" + i["detail"])
    if i["root_cause"]:
        out.append("   根因：" + i["root_cause"])
    return out


def render(d, general_max=None):
    """JSON 结论转成文字。严重问题全列；general_max：一般和建议一共最多列几条（None 不限，存完整报告用）。"""
    lines = [d["summary"]] if d["summary"] else []
    listed, hidden = 0, 0
    for level in LEVELS:
        items = [i for i in d["issues"] if i["severity"] == level]
        if level != "严重" and general_max is not None:
            room = max(general_max - listed, 0)
            hidden += max(len(items) - room, 0)
            items = items[:room]
            listed += len(items)
        if items:
            lines += ["", f"{level}："]
            for n, i in enumerate(items, 1):
                lines += issue_lines(n, i)
    if hidden:
        lines += ["", f"另有 {hidden} 条一般 / 建议问题没列出，见完整报告。"]
    lines += ["", "VERDICT: " + d["verdict"]]
    return "\n".join(lines).strip()


LEVEL_RE = re.compile(r"^[\s#>*+\-\d.、()（）【\[]*(严重|一般|建议)(.*)$")
ITEM_RE = re.compile(r"^\s*([-*+]|\d+[.、)）])\s*")


def pick_text(out):
    """文字报告交给负责人的部分：不长就整份；长了就留「严重」小节的全部内容和最多 10 条一般 / 建议问题。"""
    lines = out.strip().splitlines()
    if len(lines) <= MAX_LINES:
        return "\n".join(lines)
    keep, level, items, hidden = [], None, 0, 0
    for ln in lines:
        if ln.strip(" \t\r") in VERDICTS:
            keep.append(ln)
            continue
        m = LEVEL_RE.match(ln)
        heading = bool(m) and not m.group(2).strip(" :：*#）)】]（(问题条0123456789")
        if m:
            level = m.group(1)
        if level == "严重":
            keep.append(ln)
        elif level in ("一般", "建议"):
            if not heading and (m or ITEM_RE.match(ln)):  # 新的一条
                items += 1
                hidden += items > GENERAL_MAX
            if heading or items <= GENERAL_MAX:
                keep.append(ln)
    if hidden:
        keep.append(f"另有 {hidden} 条一般 / 建议问题没列出，见完整报告。")
    return "\n".join(keep) if keep else "\n".join(lines[-MAX_LINES:])


# ---------- Codex 在哪里跑 ----------

def dirty(path):
    """有没提交的改动（不算没加进 git 的新文件）。查不出来也当有。"""
    s = git(path, "status", "--porcelain", "--untracked-files=no")
    return s is None or bool(s)


def worktree_of(cwd, branch):
    """检出了这个分支的工作副本路径，没有返回 None。"""
    want = "branch " + (branch if branch.startswith("refs/heads/") else "refs/heads/" + branch)
    path = None
    for ln in (git(cwd, "worktree", "list", "--porcelain") or "").splitlines():
        if ln.startswith("worktree "):
            path = ln[len("worktree "):]
        elif ln == want and path:
            return path
    return None


def run_place(cwd, branch, tip):
    """Codex 在哪里跑，才能读到分支顶端的代码。返回 (目录, 说明, 临时目录)；出错时目录是 None，说明是错误。"""
    if git(cwd, "rev-parse", "--verify", "--quiet", "HEAD") == tip:
        if dirty(cwd):
            return None, (f"{cwd} 有没提交的改动，Codex 会读到和 {branch} 顶端不一样的代码。"
                          f"先提交，再用 --cwd {shlex.quote(cwd)} 重审。"), None
        return cwd, "", None
    wt = worktree_of(cwd, branch)
    if wt:
        if git(wt, "rev-parse", "--verify", "--quiet", "HEAD") != tip or dirty(wt):
            return None, (f"--cwd 不在 {branch} 的顶端；任务的工作副本 {wt} 有没提交的改动。"
                          f"先在那里提交，再用 --cwd {shlex.quote(wt)} 重审。"), None
        return wt, f"--cwd 不在 {branch} 的顶端，Codex 改在任务的工作副本里跑（下次用 --cwd {shlex.quote(wt)}）。", None
    # 哪里都没检出这个分支：建一个临时工作副本，审完删掉
    tmp = tempfile.mkdtemp(prefix="adt-review-wt-")
    path = os.path.join(tmp, "wt")
    try:
        r = subprocess.run(["git", "worktree", "add", "--detach", "--quiet", path, tip], cwd=cwd,
                           capture_output=True, text=True, timeout=600)
        ok, err = r.returncode == 0, r.stderr.strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        ok, err = False, repr(e)
    if not ok:
        shutil.rmtree(tmp, ignore_errors=True)
        return None, f"--cwd 不在 {branch} 的顶端，{branch} 也没检出在别的工作副本里，建临时工作副本失败：{err}", None
    return path, f"--cwd 不在 {branch} 的顶端，Codex 在临时工作副本里跑（审完删掉）。", tmp


def remove_place(cwd, tmp):
    if not tmp:
        return
    try:
        subprocess.run(["git", "worktree", "remove", "--force", os.path.join(tmp, "wt")], cwd=cwd,
                       capture_output=True, text=True, timeout=120)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        subprocess.run(["git", "worktree", "prune"], cwd=cwd, capture_output=True, text=True, timeout=60)


# ---------- 设计关 ----------

def plan_main(a):
    """--plan：Codex 用 medium 档只读地评做法。不写任何审核记录。"""
    cwd = os.path.abspath(a.cwd)
    if not os.path.isdir(cwd):
        print(f"设计：出错\n{cwd} 不是目录")
        return 2
    root = repo_root(cwd)
    try:
        text, u = run_codex(review_cmd(root), EFFORT_PLAN, prompt_plan(a.plan), cwd)
    except CodexError as e:
        print(f"设计：出错\n{e}")
        return 2
    found = [DESIGNS[ln.strip(" \t\r")] for ln in text.split("\n") if ln.strip(" \t\r") in DESIGNS]
    print(f"设计：{found[-1]}" if found else "设计：出错\nCodex 没有给出结论（DESIGN: OK / DESIGN: CHANGE）。")
    if u:
        print(usage_line(u))
    print(text.strip() or "（Codex 没有输出）")
    if not found:
        return 2
    return 0 if found[-1] == "可以做" else 1


# ---------- 审核 ----------

def main(argv=None):
    ap = argparse.ArgumentParser(description="审核：调 Codex 审一个分支的改动，只打印结论和问题清单。")
    ap.add_argument("--branch", help="要审核的分支（--plan 时不用）")
    ap.add_argument("--base", help="基准分支，默认是主分支")
    ap.add_argument("--cwd", default=".", help="任务的工作副本路径（HEAD 要正好是分支顶端），默认当前目录")
    ap.add_argument("--full", action="store_true", help="强制全量审（默认：上一轮没通过、之后只加了提交时只复审增量）")
    ap.add_argument("--brief", help="任务说明：目标、完成标准、已经定下的取舍，原样放进提示词")
    ap.add_argument("--plan", help="不审分支，让 Codex 只读地评这个做法说明，第一行「设计：可以做 / 建议换做法」")
    ap.add_argument("--since", help="手动指定上一轮审到的顶端提交，和 --last-issues 一起用（上一轮没存记录时）")
    ap.add_argument("--last-issues", help="上一轮的问题清单文件，和 --since 一起用")
    a = ap.parse_args(argv)
    if a.plan is not None:
        return plan_main(a)
    if not a.branch:
        print(ERR + "\n要用 --branch 指定审核的分支")
        return 2
    if bool(a.since) != bool(a.last_issues) or (a.full and a.since):
        print(ERR + "\n--since 和 --last-issues 要一起用，不能和 --full 一起用")
        return 2
    issues = None
    if a.last_issues:
        try:
            with open(a.last_issues, encoding="utf-8", errors="replace") as f:
                issues = f.read()
        except OSError as e:
            print(f"{ERR}\n读不了 --last-issues 文件：{e}")
            return 2

    cwd = os.path.abspath(a.cwd)
    root = repo_root(cwd) if os.path.isdir(cwd) else None
    if not root:
        print(f"{ERR}\n{cwd} 不是 git 仓库")
        return 2
    base = a.base or default_branch(cwd)
    if not base:
        print(ERR + "\n找不到主分支，请用 --base 指定")
        return 2
    # 开跑前把基准和分支固定成提交号：审核期间分支上再加的提交不会被算进这次通过
    shas = []
    for ref in (base, a.branch):
        sha = git(cwd, "rev-parse", "--verify", "--quiet", ref + "^{commit}")
        if not sha:
            print(f"{ERR}\n找不到分支或提交 {ref}")
            return 2
        shas.append(sha)
    base_sha, branch_sha = shas

    # 起点是基准和分支的分叉点。审的、记的都是从起点到分支顶端这一段
    fork = git(cwd, "merge-base", base_sha, branch_sha)
    commits = git(cwd, "rev-list", f"{fork}..{branch_sha}") if fork else None
    # 有没有改动直接比两棵文件树（起点的和分支顶端的），不看 git diff 的输出：diff 的输出会被仓库配置影响
    trees = [git(cwd, "rev-parse", "--verify", "--quiet", f"{c}^{{tree}}") for c in (fork, branch_sha)] if fork else []
    if commits is None or not fork or not all(trees):
        print(f"{ERR}\ngit 查不出 {base}...{a.branch} 的改动")
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
            print(f"{ERR}\n找不到提交 {a.since}")
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

    cmd_text = review_cmd(root)
    if not split_cmd(cmd_text):
        print(f"{ERR}\n审核命令写得不对：{cmd_text}")
        return 2
    use_json = is_codex_exec(split_cmd(cmd_text))
    if prev:
        prompt = prompt_incr(prev["tip"], branch_sha, prev["issues"], a.brief, use_json)
        note, effort = f"增量复审：确认上一轮的问题，审了 {prev['tip'][:12]}..{branch_sha[:12]}", EFFORT_INCR
    else:
        prompt = prompt_for(rng, a.brief, use_json)
        note, effort = (f"上一轮的记录用不上（{why}），这次全量审。" if why else ""), EFFORT_FULL

    # 同一段改动审过就复用。只复用 codex exec 按固定结构给的结论（别的命令每次照跑）
    key = hashlib.sha256(json.dumps([fork, branch_sha, PROMPT_VERSION, cmd_text, a.brief or ""]).encode()).hexdigest()
    old = records.load_review(cwd, key) if use_json else None
    if old:
        report = f"\n完整报告：{old['report']}" if old.get("report") else ""
        if old["verdict"] == "PASS":
            if not any(u["tip"] == branch_sha and u["fork"] == fork for u in records.units(cwd)):
                records.add_unit(cwd, branch_sha, fork, commits)
            records.clear_fails(cwd, a.branch)
            records.clear_last(cwd, a.branch)
            print("审核：通过（复用之前的结论）" + report)
            return 0
        print(f"审核：不通过（第 {max(records.fails(cwd, a.branch), 1)} 轮，复用之前的结论）" + report)
        print("这段改动之前审过、没通过，这次没有调 Codex，也不加失败轮数。改了代码再审。")
        print(old["shown"])
        return 1

    place, why_place, tmp = run_place(cwd, a.branch, branch_sha)
    if not place:
        print(f"{ERR}\n{why_place}")
        return 2
    try:
        text, usage = run_codex(cmd_text, effort, prompt, place, SCHEMA if use_json else None)
    except CodexError as e:
        print(f"{ERR}\n{e}")
        return 2
    finally:
        remove_place(cwd, tmp)
    note = "\n".join(x for x in (why_place, note) if x)

    rnd = records.fails(cwd, a.branch) + 1  # 这是第几轮（报告文件名用）
    d = structured(text) if use_json else None
    if d:
        verdict, full, shown = d["verdict"], render(d), render(d, GENERAL_MAX)
    else:
        verdicts = verdicts_in(text)  # 取最后一个整行结论
        verdict, full, shown = (verdicts[-1] if verdicts else None), text, pick_text(text)
    report = records.report_path(cwd, a.branch, rnd)
    if report:
        with open(report, "w", encoding="utf-8") as f:
            f.write(f"# 审核 {a.branch} 第 {rnd} 轮（{rng}）\n\n{full}\n")
    if verdict and use_json:
        records.save_review(cwd, key, {"verdict": verdict, "report": report, "shown": shown, "fork": fork,
                                       "tip": branch_sha, "at": time.strftime("%Y-%m-%dT%H:%M:%S%z")})
    where = f"完整报告：{report}" if report else ""

    if verdict == "PASS":
        records.add_unit(cwd, branch_sha, fork, commits)  # 开跑前固定的那一段：从分叉点到分支顶端（增量通过也一样）
        records.clear_fails(cwd, a.branch)
        records.clear_last(cwd, a.branch)
        print("\n".join(x for x in ("审核：通过", note, where) if x))
        log_usage(cwd, usage, a.branch, rnd, branch_sha, effort)
        return 0

    n = records.add_fail(cwd, a.branch)
    if verdict:  # 拿到有效的 VERDICT: FAIL 才用这一轮覆盖记录（下一轮只复审增量用）；没有结论时原记录一字不动
        records.save_last(cwd, a.branch, fork, branch_sha, full)
    print("\n".join(x for x in (f"审核：不通过（第 {n} 轮）", note, where) if x))
    log_usage(cwd, usage, a.branch, n, branch_sha, effort)
    if n >= 2:
        print(f"同一任务已 {n} 轮不过：负责人先自己读一次 diff，再决定重派还是挂起。")
    if not verdict:
        print("审核没有给出结论，按不通过处理。")
    print(shown or "（审核没有输出）")
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # noqa: BLE001  脚本自己出错时给出明确结论，不让负责人当成别的结果
        print(ERR + "\n脚本自己出错：" + repr(e))
        sys.exit(2)
