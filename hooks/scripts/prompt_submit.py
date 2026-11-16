#!/usr/bin/env python3
"""用户发消息时（UserPromptSubmit）按消息切换这个对话的模式。
【派活单】开头 → 子代理；【快修】开头 → 对接窗口；
整条消息正好是 负责人模式 / 普通对话 / 全自动 / 半自动 → 切到对应模式。
其他消息不做任何事、不输出。出任何错都安静退出，不影响用户发消息。
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import (AUTO_TEXT, KUAIXIU_TEXT, NORMAL_TEXT, auto_mode, guard_off, lead_text,  # noqa: E402
                    project_root, rules_text, save_session, session_id)

EXACT = {
    "负责人模式": ("role", "lead"),
    "普通对话": ("role", "normal"),
    "全自动": ("auto", "full"),
    "半自动": ("auto", "semi"),
}


def decide(prompt, data):
    """返回 (要改的状态, 注入的附加上下文)；不用切换时返回 (None, None)。"""
    text = (prompt or "").strip()
    sid = session_id(data)
    if text.startswith("【派活单】"):
        return {"role": "subagent"}, (
            "【Agent 开发团队】这个窗口是子代理，忽略之前的负责人提示。按下面的守则完成这张派活单。\n\n"
            + rules_text())
    if text.startswith("【快修】"):
        return {"role": "kuaixiu"}, "这个窗口是对接窗口（快修），忽略之前的负责人提示。\n" + KUAIXIU_TEXT
    if text not in EXACT:
        return None, None
    key, value = EXACT[text]
    note = f"【Agent 开发团队】模式已切换成「{text}」，用一句话告诉用户即可，不要做别的。"
    if key == "auto":
        note += "\n" + AUTO_TEXT[value]
    elif value == "normal":
        note += "\n" + NORMAL_TEXT
    else:
        note += "\n之后按负责人模式工作：\n" + lead_text(auto_mode(data), sid)
    return {key: value}, note


def main():
    data = json.load(sys.stdin)
    if not isinstance(data, dict) or data.get("agent_id"):
        return
    if guard_off(project_root(data.get("cwd") or os.getcwd())):
        return
    prompt = data.get("prompt")
    changes, note = decide(prompt if isinstance(prompt, str) else "", data)
    if not changes:
        return
    save_session(session_id(data), **changes)
    print(json.dumps(
        {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": note}},
        ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
