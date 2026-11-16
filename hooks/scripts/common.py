"""agent-dev-team 共用函数：判断当前会话的角色。"""
import os
import subprocess

KUAIXIU_PREFIX = "kuaixiu/"


def git(cwd, *args):
    try:
        r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=15)
    except Exception:
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def repo_root(cwd):
    return git(cwd, "rev-parse", "--show-toplevel")


def state_dir(cwd):
    common = git(cwd, "rev-parse", "--git-common-dir")
    if not common:
        return None
    if not os.path.isabs(common):
        common = os.path.join(cwd, common)
    d = os.path.join(common, "agent-dev-team")
    os.makedirs(d, exist_ok=True)
    return d


def guard_off(root):
    if os.environ.get("AGENT_DEV_TEAM", "").lower() == "off":
        return True
    return bool(root) and os.path.exists(os.path.join(root, ".claude", "agent-dev-team-off"))


def role(data):
    """返回 none / subagent / kuaixiu / lead。"""
    cwd = data.get("cwd") or os.getcwd()
    root = repo_root(cwd)
    if not root or guard_off(root):
        return "none"
    if data.get("agent_id"):
        return "subagent"
    branch = git(cwd, "rev-parse", "--abbrev-ref", "HEAD") or ""
    if branch.startswith(KUAIXIU_PREFIX):
        return "kuaixiu"
    return "lead"
