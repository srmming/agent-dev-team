#!/usr/bin/env python3
"""会话开始时告诉这个窗口它是什么角色。"""
import json
import sys

sys.path.insert(0, __import__("os").path.dirname(__file__))
from common import role

try:
    data = json.load(sys.stdin)
except Exception:
    data = {}

r = role(data)
if r == "lead":
    print(
        "【Agent 开发团队】这个对话是「负责人」。只调度，不写代码：不编辑文件、不 commit。"
        "所有开发任务先调用 lead 技能，按它的流程派给子代理。"
        "用户说「快修」时调用 kuaixiu 技能。全自动运行，不要停下来等用户点头；"
        "卡住的任务挂起并报给 Dot。"
    )
elif r == "kuaixiu":
    print(
        "【Agent 开发团队】这个对话是「对接窗口」（快修）。和用户一起做简单沟通和小改动，"
        "可以直接改文件和 commit，但只在当前分支上，不能 push、不能合并。"
        "发现是大改动就停下，告诉用户交回负责人正常派活。"
        "用户说「没问题了」后：提交到当前分支，写一段改动摘要（改了什么、动了哪些文件、怎么验证的），"
        "发消息给负责人会话交回；发不了就把摘要给用户，请他转给负责人。"
    )
