# agent-dev-team-status（状态栏）

agent-dev-team 的配套小插件。它在输入框上方画一行：

```
[ 负责人模式 ][ 普通对话 ]  [ 全自动 ][ 半自动 ]  ● 验收中 · 2 个子代理在跑             详情
```

两组按钮之间空开；「●」待命时是灰色，有事在做时用强调色；状态词粗体，子代理数灰色；
「详情」靠右。终端窄时按钮不动，只把状态文字截短，不折行。

点「详情」，按钮行下面空一行，再出一张圆角卡片，三栏等宽（每栏三行：小标题 / 粗体大字 / 灰色小字）：

```
╭──────────────────────────────────────────────────────────────────────╮
│ 现在在做              子代理                  小任务池               │
│ 验收                  2 个在跑                攒了 2 个              │
│ 结算页优惠码校验      dev-opus、dev-sonnet    攒够 8 个一起派        │
╰──────────────────────────────────────────────────────────────────────╯
```

模式只看上面那排按钮，详情里不再重复；不显示 token 用量。

- 点「负责人模式 / 普通对话」切换这个对话的模式；点「全自动 / 半自动」切换自动程度。当前选中的按钮高亮。
- 「现在在做」来自状态文件的 `status`。负责人写成「<节点>：<任务名>」（冒号中英文都行），
  冒号前是大字、后是小字，收起那一行显示「节点中」（验收 → 验收中）；没有冒号整句当大字。
  没写或写了空字符串时：有子代理在跑显示「开发中」，没有显示「待命」。
- 「小任务池」来自状态文件的 `pool`（没有当 0），显示「攒了 N 个」。
- 再点「收起」收回去。
- 普通对话时显示「这个窗口自己直接改文件」；对接窗口、子代理窗口只显示一行说明，没有按钮。
- 主插件关掉时（环境变量 `AGENT_DEV_TEAM=off`，或项目里有 `.claude/agent-dev-team-off`）什么都不显示。

它和主插件分开安装：状态栏出问题不会影响主插件的守卫。

## 模式存在哪

和主插件用同一个文件：`<状态目录>/sessions/<对话 id>.json`。状态目录是环境变量
`AGENT_DEV_TEAM_HOME`，没有就是 `~/.claude/agent-dev-team`。点按钮只改 `role` / `auto`
两个键，文件里别的内容原样保留。`status` 和 `pool` 由负责人用主插件的命令写：

```bash
python3 hooks/scripts/mode.py set --session <id> --status "验收：结算页优惠码校验"
python3 hooks/scripts/mode.py set --session <id> --pool 2
python3 hooks/scripts/mode.py set --session <id> --status ""   # 清空，回到默认显示
```
主插件或命令行改了文件，状态栏在每轮结束、子代理开始 / 结束、
会话开始时重新读，跟着变。

默认值和主插件一样：没写 `role` 时是负责人模式（当前分支以 `kuaixiu/` 开头时算对接窗口）；
没写 `auto` 时看项目配置 `.claude/agent-dev-team.json` 的 `default_auto`，再没有就是全自动。
对话 id 里 `A-Z a-z 0-9 _ . -` 以外的字符换成 `_`，和主插件找的是同一个文件。

## 本地试一下

```bash
claude --plugin-dir /path/to/agent-dev-team/statusbar
```

只在这一次打开的 Claude Code 里生效，关掉就没了。

## 常驻安装

首选从同一个市场装：先按主 README 的「安装」把 `srmming/agent-dev-team` 添加成市场，再装 `agent-dev-team-status`（命令行：`/plugin install agent-dev-team-status --marketplace srmming/agent-dev-team`）。

备用写法：在 `~/.claude/settings.json` 的 `env` 里加一行（路径换成你电脑上这个文件夹的完整路径）：

```json
{ "env": { "CLAUDE_CODE_PLUGIN_DIRS": "/path/to/agent-dev-team/statusbar" } }
```

以后每次打开 Claude Code（包括桌面版）都会带上它。如果这个变量里已经有别的路径，用英文冒号 `:` 隔开接在后面。

## 卸载

- 从市场装的，在插件页卸载。
- 用环境变量装的：把那一行从 `~/.claude/settings.json` 删掉，重开 Claude Code。
- 旧版 `lead-status` 和它功能重复，装了新版就把旧版卸掉，免得输入框上方出现两行。

## 开发

在这个文件夹里：

```bash
claude plugin validate .   # 检查插件
claude plugin test .       # 跑 tests/ 里的测试（不读写真实的状态目录）
```

类型检查：用 `--plugin-dir` 打开过一次后，引擎会在 `.claude-plugin/types/` 放好类型文件，
然后 `tsc -p .` 即可。
