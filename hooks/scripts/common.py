"""agent-dev-team 共用函数：状态目录、每个对话的模式、角色判断、各角色的提示文字。"""
import json
import os
import re
import signal
import subprocess

KUAIXIU_PREFIX = "kuaixiu/"
ROLES = ("lead", "normal", "subagent", "kuaixiu")
AUTOS = ("full", "semi")

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
PLUGIN_ROOT = os.path.dirname(os.path.dirname(SCRIPTS_DIR))
RULES_FILE = os.path.join(PLUGIN_ROOT, "hooks", "rules", "subagent.md")


def git(cwd, *args):
    try:
        r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=15)
    except Exception:
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def repo_root(cwd):
    return git(cwd, "rev-parse", "--show-toplevel")


def project_root(cwd):
    """项目根目录：git 仓库根；不在仓库里就是 cwd。"""
    return repo_root(cwd) or cwd


def state_dir(cwd):
    """按仓库存的状态目录（所有工作副本共用）。不在仓库里返回 None。"""
    common = git(cwd, "rev-parse", "--git-common-dir")
    if not common:
        return None
    if not os.path.isabs(common):
        common = os.path.join(cwd, common)
    d = os.path.join(common, "agent-dev-team")
    os.makedirs(d, exist_ok=True)
    return d


# ---------- 每个对话的模式 ----------

def state_home():
    """按对话存的状态目录。测试用环境变量 AGENT_DEV_TEAM_HOME 指到临时目录。"""
    return os.environ.get("AGENT_DEV_TEAM_HOME") or os.path.join(
        os.path.expanduser("~"), ".claude", "agent-dev-team")


def session_id(data):
    sid = data.get("session_id") if isinstance(data, dict) else None
    return sid if isinstance(sid, str) and sid else "unknown"


def session_path(sid):
    name = re.sub(r"[^A-Za-z0-9_.-]", "_", sid or "") or "unknown"
    return os.path.join(state_home(), "sessions", name + ".json")


def load_session(sid):
    """读一个对话的状态。文件不存在或坏了返回 {}。"""
    try:
        with open(session_path(sid), encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        return {}
    return d if isinstance(d, dict) else {}


def save_session(sid, **changes):
    """改一个对话的状态，不认识的键原样保留。返回改完的内容。
    读-改-写整个过程加锁（几个钩子同时改也不丢键），写临时文件再换名（别人读不到写了一半的文件）。"""
    p = session_path(sid)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p + ".lock", "a") as lock:
        try:
            import fcntl
            fcntl.flock(lock, fcntl.LOCK_EX)
        except Exception:
            pass  # 没有 fcntl（比如 Windows）就不加锁
        d = load_session(sid)
        d.update(changes)
        tmp = f"{p}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False)
        os.replace(tmp, p)
    return d


def project_config(root):
    """读 <项目根>/.claude/agent-dev-team.json。文件不存在或坏了返回 {}。"""
    try:
        with open(os.path.join(root, ".claude", "agent-dev-team.json"), encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        return {}
    return d if isinstance(d, dict) else {}


def guard_off(root):
    if os.environ.get("AGENT_DEV_TEAM", "").lower() == "off":
        return True
    return bool(root) and os.path.exists(os.path.join(root, ".claude", "agent-dev-team-off"))


def role(data):
    """返回 none / normal / subagent / kuaixiu / lead。
    顺序：守卫关了 → none；对话标成普通对话 → normal（普通对话里开的后台子代理也不拦）；
    有 agent_id → subagent；对话记下的角色；kuaixiu/ 分支 → kuaixiu；其余 lead。"""
    cwd = data.get("cwd") or os.getcwd()
    if guard_off(project_root(cwd)):
        return "none"
    saved = load_session(session_id(data)).get("role")
    if saved == "normal":
        return "normal"
    if data.get("agent_id"):
        return "subagent"
    if saved in ROLES:
        return saved
    branch = git(cwd, "rev-parse", "--abbrev-ref", "HEAD") or ""
    if branch.startswith(KUAIXIU_PREFIX):
        return "kuaixiu"
    return "lead"


def default_auto(cwd):
    auto = project_config(project_root(cwd)).get("default_auto")
    return auto if auto in AUTOS else "full"


def auto_mode(data):
    """返回 full（全自动）或 semi（半自动）。"""
    auto = load_session(session_id(data)).get("auto")
    if auto in AUTOS:
        return auto
    return default_auto(data.get("cwd") or os.getcwd())


# ---------- 各角色的提示文字 ----------

NORMAL_TEXT = "【Agent 开发团队】这个对话是「普通对话」，这个窗口可以直接改文件。说「负责人模式」切回去。"

KUAIXIU_TEXT = (
    "【Agent 开发团队】这个对话是「对接窗口」（快修）。和用户一起做简单沟通和小改动，"
    "可以直接改文件和 commit，但只在当前分支上，不能 push、不能合并。"
    "要跟上主分支用 git rebase（git merge 会被守卫拦）。"
    "发现是大改动就停下，告诉用户交回负责人正常派活。"
    "用户说「没问题了」后：提交到当前分支，写一段改动摘要（改了什么、动了哪些文件、怎么验证的），"
    "发消息给负责人会话交回；发不了就把摘要给用户，请他转给负责人。"
)

SUBAGENT_TEXT = (
    "【Agent 开发团队】这个窗口是「子代理」。只做派活单范围内的事；"
    "只在派活单指定的分支 / 工作副本上干活并 commit，不 push、不合并、不开 PR；"
    "解决合并冲突用 git rebase（git merge 会被守卫拦）；"
    "交回前自己跑一遍完成标准，写真实结果；卡住就停下交回；不联系 Dot，不写 Notion；交回 20 行以内。"
)

AUTO_TEXT = {
    "full": "当前是「全自动」：审核通过后直接合并、推送，不停下来等用户点头；卡住的任务挂起并报给 Dot。",
    "semi": "当前是「半自动」：不直接合并进主分支、不直接推主分支；推任务分支后开 PR，等用户点合并。",
}


def lead_text(auto, sid):
    def script(name):
        return os.path.join(SCRIPTS_DIR, name)

    return "\n".join([
        "【Agent 开发团队】这个对话是「负责人模式」。负责人只想、只调度，不写代码：不编辑项目文件、不 commit。",
        "- 所有开发请求先调用 lead 技能，按它的流程拆任务、派给子代理。用户说「快修」时调用 kuaixiu 技能。",
        "- 只讨论、只问问题的请求不派活，正常回答。",
        "- 向用户提问一律用提问器（AskUserQuestion 工具），不在正文里问。",
        "- " + AUTO_TEXT.get(auto, AUTO_TEXT["full"]),
        "- 负责人自己用 python3 跑的脚本（都支持 --help，只看结论）：",
        "  验收：" + script("verify.py"),
        "  审核：" + script("review.py"),
        "  上线检查：" + script("deploy_check.py"),
        "  回滚：" + script("rollback.py"),
        f"  查看 / 切换模式：{script('mode.py')}（本对话的 session id：{sid}）",
        "- 用户想自己直接改文件时，告诉他说「普通对话」可以退出负责人模式；说「全自动」「半自动」切换合并方式。",
    ])


def rules_text():
    """子代理守则全文（hooks/rules/subagent.md）。读不到就用简短版。
    不放在 agents/ 下：那里的 .md 会被插件当成子代理。"""
    try:
        with open(RULES_FILE, encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return SUBAGENT_TEXT


def role_text(r, data):
    """会话开始时按角色给的提示，同时记下「已经告诉模型的角色和模式」。none 返回空字符串。"""
    if r == "lead":
        text = lead_text(auto_mode(data), session_id(data))
    else:
        text = {"normal": NORMAL_TEXT, "kuaixiu": KUAIXIU_TEXT, "subagent": SUBAGENT_TEXT}.get(r, "")
    if text:
        save_told(data, r)
    return text


# ---------- 上次已经告诉模型的角色和模式 ----------
# 状态栏按钮只改状态文件，模型不知道。prompt_submit.py 每条消息比一下，变了才再告诉一次。
# 记在状态文件旁边的 <session>.told 里，不进状态文件：状态文件只放模式本身（mode.py get 原样打印它）。

def told_key(r, data):
    """模型该知道的角色和模式。全自动 / 半自动只有负责人用得上，别的角色只看角色。"""
    return f"{r}/{auto_mode(data)}" if r == "lead" else r


def told_path(sid):
    return session_path(sid)[:-len(".json")] + ".told"


def load_told(sid):
    """没记过（或读不了）返回 None。"""
    try:
        with open(told_path(sid), encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return None


def save_told(data, r):
    """记下已经告诉模型的角色和模式。子代理（有 agent_id）不记，它和开它的对话共用 session id。出错不影响提示。"""
    if data.get("agent_id"):
        return
    try:
        p = told_path(session_id(data))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = f"{p}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(told_key(r, data))
        os.replace(tmp, p)
    except Exception:
        pass


TAIL = 30


def tail(text, n=TAIL):
    return "\n".join(text.rstrip().splitlines()[-n:])


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
