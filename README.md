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

子代理守则全文在 `hooks/rules/subagent.md`（窗口收到 `【派活单】` 时注入的就是它），四个 `agents/dev-*.md` 里是同一份。它不放在 `agents/` 下，因为那里的文件会被当成子代理。

## 状态栏（可选）

仓库的 `statusbar/` 目录是一个配套小插件 agent-dev-team-status：在输入框上方显示当前模式和在跑的子代理，可以点按钮切换「负责人模式 / 普通对话」「全自动 / 半自动」。它和主插件读写同一个对话状态文件。

它要单独安装，装法见 `statusbar/README.md`。没装时直接发口令切换：「负责人模式」「普通对话」「全自动」「半自动」。

状态栏目前只在测试里跑过，还没在真实的桌面版里确认过显示和点击。

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

- verify.py：查 `<基准>...<分支>` 改了的文件有没有超出 `--allow`（不给就不查；改名时新旧路径都算），再按顺序跑 `--cmd`，第一条失败（退出码非 0 或超时，默认每条 900 秒）就停，打印输出的最后 30 行。基准默认是主分支。
- review.py：开跑前先把基准和分支固定成提交号，Codex 审的、记通过的都是这两个提交号之间的改动；审核期间分支上再加的提交不算。超时 1800 秒。结论取输出里最后一行单独的 `VERDICT: PASS / FAIL`，没有结论按不通过。通过时把这批提交记进审核记录，清掉失败轮数；不通过时轮数加 1，打印问题清单（最多 60 行），第 2 轮起提醒负责人自己读一次 diff。分支有提交但合起来没改文件（空提交、改了又改回来）时打印「审核：没有改动」，并把这些提交记为通过。不是仓库、找不到分支、git 查不出改动、找不到审核命令、审核命令退出码不是 0、超时都算出错，不计入轮数。
- deploy_check.py：推送后检查线上。项目配置有 `deploy_check.cmd` 时：先等 `wait_seconds`（默认 60），跑命令，失败隔 `interval_seconds`（默认 30）重试，最多重试 `retries` 次（默认 3，一共跑 4 次）。没配命令、远端是 GitHub、本机有 `gh` 时：查这个提交的检查结果和部署状态；同名检查、同一环境的部署只看最新一次；还在跑就每 15 秒查一次，满 10 分钟（`--max-poll`）还没完判「无法判断」。有任何一项查询失败（没登录、没权限、网络）时不报通过：查到确定的失败报「不通过」，否则报「无法判断」。都没有：无法判断。
- rollback.py：按给的顺序 `git revert --no-edit`（合并提交加 `-m 1`），提交号要从新到旧给。先确认全部提交号都在，有一个不对就一个都不动。中途撤回失败时跑 `git revert --abort`，已经做好的回滚提交留在本地，并在输出里列出来。回滚提交记进审核记录（回到审核过的状态，推送不用再审）。带 `--push` 时先按守卫同样的算法查要推的提交，有没审核的就不推（`AGENT_DEV_TEAM_CODEX=off` 时不查），然后 `git push`。只撤回代码，不碰数据库。
- 四个脚本自己出意外错误时，第一行分别是「验收：出错」「审核：出错（不计入失败轮数）」「上线检查：无法判断」「回滚：失败」，不会被当成通过。
- mode.py：状态栏按钮和负责人用。`set` 至少带一个 `--role` / `--auto` / `--status`，改完打印一遍。

审核记录按仓库存在 `<git 公共目录>/agent-dev-team/`（`passed` 是通过的提交号，`fails-<分支>` 是失败轮数），所有工作副本共用。

## 守卫怎么判

- 普通对话、守卫关了：全部放行。
- 子代理、对接窗口：只拦上传类命令：`git push`、`git merge`、`gh pr create`、`gh pr merge`，以及用 `gh api` 合并 PR 或改远端分支。所以它们要跟上主分支或解决合并冲突，只能用 `git rebase`。
- 负责人：
  1. 编辑工具改项目根目录（git 仓库根；不在仓库里就是当前目录）里的文件 → 拦。项目外的文件放行。
  2. 命令行只拦写的动作：重定向写文件（写到 `/dev/null`、`/dev/stdout`、`/dev/stderr`、`/dev/tty` 和 `2>&1` 这类不算）；`rm` `mv` `cp` `tee` `touch` `truncate` `dd` `patch` `install` `ln` `chmod` `chown`；`sed -i` / `perl -i`；`git commit` `add` `checkout` `switch` `reset` `restore` `rebase` `cherry-pick` `stash` `apply` `am` `clean` `rm` `mv`；`find -delete / -exec / -execdir`。引号或括号没闭合的命令也拦，请改写。
  3. 上传关：`git merge <分支>` 要求 `HEAD..<分支>` 的提交都审核过；`git push`、`gh pr create`、`gh pr merge` 要求要上传的提交（从上游算起；没有上游用远端的 HEAD、main、master）都审核过。推指定分支时（如 `git push origin feat`、`gh pr create --head feat`）查那个分支；`--all` 查所有分支，`--tags` 查所有标签；带通配符的推送直接拦。合并提交里，两边自动合出来的不用审，夹带了别的改动的要审。查不了（纯本地仓库、第一次推送）就放行；git 查不出要上传哪些提交时拦。用 `gh api` 直接改远端分支一律拦，请改用 `git push`。被拦时提示里给出要跑的审核命令。守卫自己不跑 Codex。
  4. 半自动：有远端时拦在主分支上 `git merge <分支>`、拦推主分支（含 `--all`、`--branches`、`--mirror`，以及上游是主分支的推送）；`gh pr merge` 和用 `gh api` 合并 PR 一律拦，PR 由用户自己点合并。
- 认命令时会剥掉开头的 `VAR=值`、`do` `then` `else` `if` `while` `!` `{` `time` 这类关键字，以及包装器和它们的选项：`env`（含 `-u X`、`-i`、`-S`、`VAR=值`）、`rtk`（含 `rtk proxy`）、`sudo`、`doas`、`command`、`builtin`、`exec`、`nohup`、`timeout <时长>`、`nice`、`ionice`、`stdbuf`、`caffeinate`、`xargs`，剥到真正的命令为止。黑名单和上传判断用同一套。
- 这些地方的命令也拆开查：`$( )`、反引号、括号和 `{ ...; }`、`&&` `||` `;` `|` 串起来的每一段、`bash` / `sh` / `zsh -c "..."`、`eval "..."`、喂给 shell 的 heredoc 和 `<<<`、`find -exec`。
- `git -C <路径>`、`--git-dir`、`--work-tree`、`GIT_DIR=...` 会到那个仓库里查；`cd <目录> && git merge ...`、`pushd` 也跟着走。`cd` 用了变量等看不懂的路径、后面又有上传命令时，负责人这边直接拦，请改用 `git -C <绝对路径>`。
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

- 守卫是护栏，不是安全边界。下面这些写法挡不住：
  - 别的语言里间接写文件或调 git：`python3 -c "..."`、`node -e "..."`、`python3 脚本.py`、`bash 脚本.sh`（脚本文件的内容不查）。
  - 命令名或子命令来自变量或展开：`$GIT push`、`x=push; git $x`、`$(echo git) push`。
  - git 别名：`git config alias.p push` 之后的 `git p`，或 `git -c alias.p=push p`。
  - 绕开 git 和 gh 直接调 GitHub 接口（`curl` 等）。
  - 负责人的黑名单只拦规格列的命令，`git update-ref`、`git branch -f` 这类挪分支的不拦；挪完再推送时上传关照样查。
- 比规格多做的几处：多拦了包装器（`sudo` `env` `timeout` 等）和 `do` `then` `else` 后面的命令，以及 `bash -c`、`eval`、喂给 shell 的 heredoc、`find -exec` 里的命令；多放了重定向到 `/dev/stdout`、`/dev/stderr`、`/dev/tty`；推送时按被推的分支查审核；夹带改动的合并提交要审；半自动下 `gh pr merge` 一律拦。
- 全自动下的 `gh pr merge` 查的是本地当前分支要上传的提交，不是 PR 里的提交。负责人平时不用它合并。
- 新对话默认是负责人模式，对所有文件夹都生效，不只是 git 项目。不想要时说「普通对话」，或用「开关」关掉。
- 普通对话里开的后台子代理不受守卫限制（规格里没有这条，是后加的）。
- `rollback.py --push` 在脚本里直接推送，不经过守卫的半自动限制，半自动模式下也会直接推主分支（审核记录照样查）。
- 状态栏改对话状态文件时不加锁（它用不了文件锁），和钩子同时改时可能丢一次改动；钩子之间互相加了锁。
- 回滚只能撤回代码，不碰数据库。数据库出问题靠任务里的「改回来」脚本或备份。
