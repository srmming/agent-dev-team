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

子代理守则全文在 `agents/RULES.md`，四个 `agents/dev-*.md` 里是同一份。

## 角色怎么认

按顺序判断：

1. 守卫关了（见「开关」）：不管。
2. 这个对话标成了「普通对话」：普通对话。普通对话里开的后台子代理也按普通对话放行。
3. 后台子代理（钩子输入里有 `agent_id`）：子代理。
4. 这个对话记过别的角色：用记下的角色。
5. 当前分支以 `kuaixiu/` 开头：对接窗口（旧做法，保留兼容）。
6. 其余：负责人。不在 git 仓库里也一样。

每个对话的角色存在 `~/.claude/agent-dev-team/sessions/<对话 id>.json`，例如 `{"role": "lead", "auto": "full"}`。环境变量 `AGENT_DEV_TEAM_HOME` 可以换这个目录。

## 怎么切换

点状态栏按钮，或者直接发下面这些话（整条消息就是这几个字）：

| 你说 | 结果 |
|---|---|
| 普通对话 | 这个窗口变回普通对话，可以直接改文件 |
| 负责人模式 | 切回负责人 |
| 全自动 | 审核通过后直接合并、推送 |
| 半自动 | 推任务分支、开 PR，等你点合并 |

切换后 AI 用一句话告诉你切好了。切到「负责人模式」「普通对话」时，钩子还会把这个角色的提示一起交给 AI。

另外两种由负责人自动使用：消息以 `【派活单】` 开头，窗口变成子代理（附上子代理守则）；以 `【快修】` 开头，窗口变成对接窗口。

## 五个脚本

都在插件的 `hooks/scripts/` 下，用 `python3` 跑，都支持 `--help`，第一行就是结论。负责人看到的绝对路径在会话开始的提示里。参数写错时退出码 2。

| 脚本 | 用法 | 第一行 | 退出码 |
|---|---|---|---|
| verify.py | `--branch <分支> [--base <基准>] [--cwd <工作副本>] [--allow <通配符>]... [--cmd "<命令>"]... [--timeout <秒>]` | 验收：通过 / 不通过 / 出错 | 0 / 1 / 2 |
| review.py | `--branch <分支> [--base <基准>] [--cwd <仓库>]` | 审核：通过 / 没有改动 / 不通过（第 N 轮）/ 出错（不计入失败轮数） | 0 / 0 / 1 / 2 |
| deploy_check.py | `[--cwd <仓库>] [--sha <提交>] [--wait <秒>] [--interval <秒>] [--retries <次>] [--max-poll <秒>]` | 上线检查：通过 / 不通过 / 无法判断 | 0 / 1 / 3 |
| rollback.py | `--commits <提交号>... [--cwd <仓库>] [--push]` | 回滚：完成 / 失败 | 0 / 1 |
| mode.py | `get --session <id> [--cwd <项目>]`；`set --session <id> [--role lead\|normal\|subagent\|kuaixiu] [--auto full\|semi] [--status "<文字>"]` | 打印这个对话的状态（JSON） | 0，参数不对 2 |

- verify.py：查 `<基准>...<分支>` 改了的文件有没有超出 `--allow`（不给就不查），再按顺序跑 `--cmd`，第一条失败（退出码非 0 或超时，默认每条 900 秒）就停，打印输出的最后 30 行。基准默认是主分支。
- review.py：用 Codex 审 `<基准>...<分支>` 的改动，超时 1800 秒。取输出里最后一个 `VERDICT:`，没有结论按不通过。通过时把这个分支的提交记进审核记录，清掉失败轮数；不通过时轮数加 1，打印问题清单（最多 60 行），第 2 轮起提醒负责人自己读一次 diff。不是仓库、找不到分支、找不到审核命令、超时都算出错，不计入轮数。
- deploy_check.py：推送后检查线上。项目配置有 `deploy_check.cmd` 时：先等 `wait_seconds`（默认 60），跑命令，失败隔 `interval_seconds`（默认 30）重试，最多重试 `retries` 次（默认 3，一共跑 4 次）。没配命令、远端是 GitHub、本机有 `gh` 时：查这个提交的检查结果和部署状态，还在跑就每 15 秒查一次，满 10 分钟（`--max-poll`）还没完判「无法判断」。都没有：无法判断。
- rollback.py：按给的顺序 `git revert --no-edit`（合并提交加 `-m 1`），提交号要从新到旧给。先确认全部提交号都在，有一个不对就一个都不动。中途撤回失败时跑 `git revert --abort`，已经做好的回滚提交留在本地，并在输出里列出来。回滚提交记进审核记录（回到审核过的状态，推送不用再审）。带 `--push` 时接着 `git push`。只撤回代码，不碰数据库。
- mode.py：状态栏按钮和负责人用。`set` 至少带一个 `--role` / `--auto` / `--status`，改完打印一遍。

审核记录按仓库存在 `<git 公共目录>/agent-dev-team/`（`passed` 是通过的提交号，`fails-<分支>` 是失败轮数），所有工作副本共用。

## 守卫怎么判

- 普通对话、守卫关了：全部放行。
- 子代理、对接窗口：只拦 `git push`、`git merge`、`gh pr create`、`gh pr merge`。所以它们要跟上主分支或解决合并冲突，只能用 `git rebase`。
- 负责人：
  1. 编辑工具改项目根目录（git 仓库根；不在仓库里就是当前目录）里的文件 → 拦。项目外的文件放行。
  2. 命令行只拦写的动作：重定向写文件（写到 `/dev/null`、`/dev/stdout`、`/dev/stderr`、`/dev/tty` 和 `2>&1` 这类不算）；`rm` `mv` `cp` `tee` `touch` `truncate` `dd` `patch` `install` `ln` `chmod` `chown`；`sed -i` / `perl -i`；`git commit` `add` `checkout` `switch` `reset` `restore` `rebase` `cherry-pick` `stash` `apply` `am` `clean` `rm` `mv`；`find -delete / -exec / -execdir`。引号或括号没闭合的命令也拦，请改写。
  3. 上传关：`git merge <分支>` 要求 `HEAD..<分支>` 的提交都审核过；`git push`、`gh pr create`、`gh pr merge` 要求要上传的提交（从上游算起；没有上游用远端的 HEAD、main、master）都审核过，推指定分支时（如 `git push origin feat`、`gh pr create --head feat`）查那个分支。合并提交不用审。查不了（纯本地仓库、第一次推送）就放行。被拦时提示里给出要跑的审核命令。守卫自己不跑 Codex。
  4. 半自动且仓库有远端：拦在主分支上 `git merge <分支>`、拦推主分支（含 `--all`、`--mirror`）。
- 认命令时会跳过开头的 `VAR=值`、`do` `then` `else` `if` `while` 这类关键字，以及 `rtk` `sudo` `env` `command` `exec` `nohup` `xargs` 这类包装器，`$( )`、反引号和括号里的命令也查。`git -C <路径>`、`git -c <配置>` 会跳过再看子命令，`cd <目录> && git merge ...` 会到那个目录里查。
- 守卫自己出任何错都放行。

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
| `deploy_check.wait_seconds` | 推送后先等多少秒再检查，默认 60 |
| `deploy_check.retries` | 检查失败后最多重试几次，默认 3 |
| `deploy_check.interval_seconds` | 每次重试之间隔多少秒，默认 30 |
| `db_backup_cmd` | 动数据库前的备份命令。不配时，动数据库的任务不会自动上线，负责人会先问你 |
| `codex_cmd` | 审核命令。不写就用 `codex exec --sandbox read-only -c model_reasoning_effort=max`，型号跟着本机 Codex 配置走 |

数字写错了（不是数字或是负数）按默认值算。

## 需要

- `python3` 和 `git`。
- Codex 命令行（审核用）。
- `gh`（半自动开 PR、查 GitHub 上线状态用，可选）。
- Notion 和 Slack 连接（看板和汇报用）。

测试：`python3 -m unittest discover -s tests`。

## 已知限制

- 守卫是护栏，不是安全边界。负责人的命令按黑名单只拦写的动作，`bash -c "..."`、`python3 -c "..."`、`python3 脚本.py` 这类间接写文件的写法挡不住，复杂写法也可能漏拦或误拦。
- 比规格多做的几处：多拦了 `sudo` `env` `command` `exec` `nohup` `xargs` 后面的命令，以及 `do` `then` `else` 后面的命令；多放了重定向到 `/dev/stdout`、`/dev/stderr`、`/dev/tty`；推送时按被推的分支查审核。
- 新对话默认是负责人模式，对所有文件夹都生效，不只是 git 项目。不想要时说「普通对话」，或用「开关」关掉。
- 普通对话里开的后台子代理不受守卫限制（规格里没有这条，是后加的）。
- `rollback.py --push` 在脚本里直接推送，不经过守卫，半自动模式下也会直接推主分支。
- 回滚只能撤回代码，不碰数据库。数据库出问题靠任务里的「改回来」脚本或备份。
