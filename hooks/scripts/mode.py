#!/usr/bin/env python3
"""查看 / 切换一个对话的模式（状态栏按钮和负责人都能用）。

  mode.py get --session <id> [--cwd <项目目录>]
  mode.py set --session <id> [--role lead|normal|subagent|kuaixiu] [--auto full|semi] [--status "<文字>"]

get 打印这个对话的状态（JSON）。set 改完后也打印一遍。参数不对时退出码 2。
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import AUTOS, ROLES, default_auto, load_session, save_session  # noqa: E402


class Parser(argparse.ArgumentParser):
    def error(self, message):
        self.print_usage(sys.stderr)
        print("参数不对：" + message, file=sys.stderr)
        sys.exit(2)


def fail(message):
    print("参数不对：" + message, file=sys.stderr)
    sys.exit(2)


def show(state, cwd):
    out = dict(state)
    if out.get("role") not in ROLES:
        out["role"] = "lead"
    if out.get("auto") not in AUTOS:
        out["auto"] = default_auto(cwd)
    print(json.dumps(out, ensure_ascii=False))


def main(argv=None):
    parser = Parser(prog="mode.py", description="查看 / 切换一个对话的模式。")
    sub = parser.add_subparsers(dest="action")
    for name, text in (("get", "打印这个对话的模式（JSON）"), ("set", "切换这个对话的模式")):
        p = sub.add_parser(name, help=text, description=text)
        p.add_argument("--session", required=True, help="对话的 session id")
        p.add_argument("--cwd", default=os.getcwd(), help="项目目录，用来读默认的全自动 / 半自动（默认当前目录）")
        if name == "set":
            p.add_argument("--role", help="lead（负责人模式）/ normal（普通对话）/ subagent（子代理）/ kuaixiu（对接窗口）")
            p.add_argument("--auto", help="full（全自动）/ semi（半自动）")
            p.add_argument("--status", help="状态栏上显示的一句话")
    args = parser.parse_args(argv)
    if not args.action:
        parser.error("要写 get 或 set")
    if not args.session.strip():
        fail("--session 不能为空")

    if args.action == "get":
        show(load_session(args.session), args.cwd)
        return

    changes = {}
    if args.role is not None:
        if args.role not in ROLES:
            fail(f"--role 只能是 {' / '.join(ROLES)}，不能是 {args.role!r}")
        changes["role"] = args.role
    if args.auto is not None:
        if args.auto not in AUTOS:
            fail(f"--auto 只能是 {' / '.join(AUTOS)}，不能是 {args.auto!r}")
        changes["auto"] = args.auto
    if args.status is not None:
        changes["status"] = args.status
    if not changes:
        fail("set 至少要带 --role、--auto、--status 里的一个")
    show(save_session(args.session, **changes), args.cwd)


if __name__ == "__main__":
    main()
