# Agent 开发团队（agent-dev-team）

你打开的对话默认是「负责人」：只想、只调度，不写代码。读代码交给便宜的子代理，验收交给脚本，逐行审代码交给 Codex。合并前必须审核通过，做完按你的 Slack 汇报规则汇报，一批做完再整理记忆。

负责人向你提问一律用提问器（AskUserQuestion 工具），不在正文里问。

## 组成

| 类型 | 名字 | 作用 |
|---|---|---|
| 技能 | lead | 负责人总流程：分类、派活、验收、升级、审核、合并、上线 |
| 技能 | kuaixiu | 说「快修」开对接窗口，你和 AI 一起做小改动 |
| 技能 | shouwei | 更新 Notion 看板，按你的 Slack 汇报规则发汇报 |
| 技能 | shuijiao | 一批任务做完后整理记忆、沉淀技能 |
| 子代理 | dev-haiku | 杂活和侦察 |
| 子代理 | dev-sonnet | 一般任务（范围清楚、有现成写法、一条命令能验证、不用做设计取舍） |
| 子代理 | dev-opus | 主力开发 |
| 子代理 | dev-fable | 重构、改架构、跨多模块 |
| 钩子 | guard.py | 负责人不能改项目文件；子代理和对接窗口不能 push / 合并；合并和推送前查审核记录 |
| 钩子 | session_start.py | 会话开始时告诉窗口它是什么角色，给出脚本的绝对路径 |
| 钩子 | prompt_submit.py | 根据你发的消息切换模式 |

## 角色怎么认

按顺序判断：

1. 守卫关了（见「开关」）：不管。
2. 后台子代理（有 `agent_id`）：子代理。
3. 这个对话记过角色：用记下的角色。
4. 当前分支以 `kuaixiu/` 开头：对接窗口（旧做法，保留兼容）。
5. 其余：负责人。

每个对话的角色存在 `~/.claude/agent-dev-team/sessions/<对话 id>.json`。环境变量 `AGENT_DEV_TEAM_HOME` 可以换这个目录。

## 怎么切换

点状态栏按钮，或者直接发下面这些话（整条消息就是这几个字）：

| 你说 | 结果 |
|---|---|
| 普通对话 | 这个窗口变回普通对话，可以直接改文件 |
| 负责人模式 | 切回负责人 |
| 全自动 | 合并后直接推送 |
| 半自动 | 推任务分支、开 PR，等你点合并 |

另外两种由负责人自动使用：消息以 `【派活单】` 开头，窗口变成子代理；以 `【快修】` 开头，窗口变成对接窗口。

## 五个脚本

都在插件的 `hooks/scripts/` 下，用 `python3` 跑，都支持 `--help`，只打印很短的结论。负责人看到的绝对路径在会话开始的提示里。

| 脚本 | 用法 | 结论 |
|---|---|---|
| verify.py | `--branch <分支> [--base <基准>] [--cwd <工作副本>] --allow <通配符>... --cmd "<命令>"... [--timeout <秒>]` | 验收：通过 / 不通过（退出码 0 / 1，参数或 git 出错 2） |
| review.py | `--branch <分支> [--base <基准>] [--cwd <仓库>]` | 审核：通过 / 不通过（第 N 轮）/ 没有改动（退出码 0 / 1，找不到审核命令或超时 2） |
| deploy_check.py | `[--cwd <仓库>] [--sha <提交>]` | 上线检查：通过 / 不通过 / 无法判断（退出码 0 / 1 / 3） |
| rollback.py | `--commits <提交号>... [--cwd <仓库>] [--push]` | 回滚：完成 / 失败（退出码 0 / 1） |
| mode.py | `get --session <id>`；`set --session <id> [--role lead\|normal\|subagent\|kuaixiu] [--auto full\|semi] [--status "<文字>"]` | 读或改对话的模式 |

- verify.py：查改动文件有没有超出 `--allow` 的范围，再逐条跑 `--cmd`。
- review.py：用 Codex 审 `<基准>...<分支>` 的改动。通过的提交记进审核记录，守卫靠它放行合并和推送。
- deploy_check.py：推送后检查线上。优先跑项目配置的 `deploy_check.cmd`；没配时，GitHub 仓库用 `gh` 查检查结果和部署状态。
- rollback.py：按顺序 `git revert` 给的提交，带 `--push` 时接着推送。

## 开关

- 整个关掉：环境变量 `AGENT_DEV_TEAM=off`，或在项目里建空文件 `.claude/agent-dev-team-off`。
- 只关审核关：环境变量 `AGENT_DEV_TEAM_CODEX=off`。
- 换审核命令：环境变量 `AGENT_DEV_TEAM_CODEX_CMD`（项目配置的 `codex_cmd` 优先）。

## 项目配置（可选）

`<项目根>/.claude/agent-dev-team.json`，每个键都可以不写：

```json
{
  "default_auto": "full",
  "deploy_check": {"cmd": "curl -fsS https://example.com/health", "wait_seconds": 120, "retries": 5, "interval_seconds": 30},
  "db_backup_cmd": "",
  "codex_cmd": ""
}
```

| 键 | 意思 |
|---|---|
| `default_auto` | 新对话默认全自动（`full`）还是半自动（`semi`）。不写就是全自动 |
| `deploy_check.cmd` | 推送后检查线上的命令，退出码 0 算通过 |
| `deploy_check.wait_seconds` | 推送后先等多少秒再检查 |
| `deploy_check.retries` | 检查失败后最多重试几次 |
| `deploy_check.interval_seconds` | 每次重试之间隔多少秒 |
| `db_backup_cmd` | 动数据库前的备份命令。不配时，动数据库的任务不会自动上线，负责人会先问你 |
| `codex_cmd` | 审核命令。不写就用 `codex exec --sandbox read-only -c model_reasoning_effort=max`，型号跟着本机 Codex 配置走 |

## 需要

- `python3` 和 `git`。
- Codex 命令行（审核用）。
- `gh`（半自动开 PR、查 GitHub 上线状态用，可选）。
- Notion 和 Slack 连接（看板和汇报用）。

测试：`python3 -m unittest discover -s tests`。

## 已知限制

- 守卫是护栏，不是安全边界。负责人的命令按黑名单只拦写的动作，`python3` 这类能间接写文件的命令不拦，复杂写法也可能漏拦或误拦。
- 新对话默认是负责人模式，对所有文件夹都生效，不只是 git 项目。不想要时说「普通对话」，或用「开关」关掉。
- 回滚只能撤回代码，不碰数据库。数据库出问题靠任务里的「改回来」脚本或备份。
