#!/usr/bin/env python3
"""会话开始时（SessionStart）告诉这个窗口它是什么角色。守卫关掉时不输出。"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import role, role_text  # noqa: E402


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    text = role_text(role(data), data)
    if text:
        print(text)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
