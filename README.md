# Agent 开发团队（agent-dev-team）

在 git 项目里，你自己打开的对话是「负责人」：只调度，不写代码。开发由它派出的子代理完成，push / 合并前由 Codex 审核，做完汇报给 Dot，再整理记忆。

## 组成

| 类型 | 名字 | 作用 |
|---|---|---|
| 技能 | lead | 负责人总流程：查看板、分类、派活、验收、升级、合并 |
| 技能 | kuaixiu | 说「快修」开对接窗口，你和 AI 一起做小改动 |
| 技能 | shouwei | 更新 Notion 看板，Slack 通知 Dot |
| 技能 | shuijiao | 任务后整理记忆、沉淀技能 |
| 子代理 | dev-haiku / dev-sonnet / dev-opus / dev-fable | 四档开发子代理 |
| Hook | guard.py | 负责人改文件就拦；子代理和对接窗口不能 push / 合并；负责人 push / 合并前必过 Codex |
| Hook | session_start.py | 会话开始时告诉窗口它是负责人还是对接窗口 |

## 守卫怎么认角色

- 不在 git 仓库里：不管。
- 有子代理标识（`agent_id`）：子代理。
- 当前分支以 `kuaixiu/` 开头：对接窗口。
- 其余：负责人。

## 需要

- `python3` 和 `git`。
- Codex 命令行。默认审核命令是 `codex exec --sandbox read-only`，可用环境变量 `AGENT_DEV_TEAM_CODEX_CMD` 换成你要的型号和思考档位，例如
  `codex exec --sandbox read-only -m <型号> -c model_reasoning_effort=high`。
- Notion 和 Slack 连接（看板和通知 Dot 用）。

## 开关

- 整个关掉：环境变量 `AGENT_DEV_TEAM=off`，或在项目里建空文件 `.claude/agent-dev-team-off`。
- 只关 Codex 审核：环境变量 `AGENT_DEV_TEAM_CODEX=off`。

## 负责人能跑的命令

- 只读和验收类命令走白名单（`git status`、`ls`、`grep`、`npm`、`pytest` 等）。
- `claude` 只放行插件的只读检查：`claude plugin validate`、`claude plugin test`、`claude plugin list`、`claude plugin marketplace list` 和 `--help`。安装、卸载、`claude -p` 仍然要派给子代理。
- 命令前面带 `rtk`（本机的命令包装器）时，按被包装的命令检查。
- 引号里的 `|`、`;`、`>` 不算操作符，搜索词里带这些符号不会被误拦。

守卫脚本的测试：`python3 tests/test_guard.py`。

## 已知限制

- 守卫是护栏，不是安全边界：负责人的命令行按白名单检查，复杂写法可能误拦或漏拦。
- `python3`、`env`、`timeout`、`npx` 在白名单里，它们能间接运行任何程序，守卫不检查它们后面跑的是什么。
- 审核通过的记录按提交保存在 `.git/agent-dev-team/`，同一提交不会重复审。
