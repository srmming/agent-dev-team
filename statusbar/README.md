# agent-dev-team-status（状态栏）

agent-dev-team 的配套小插件。它在输入框上方画一行：

```
[负责人模式] [普通对话]  [全自动] [半自动]  ● 开发中 · 2 个子代理在跑        详情
```

- 点「负责人模式 / 普通对话」切换这个对话的模式；点「全自动 / 半自动」切换自动程度。当前选中的按钮高亮。
- 状态文字：有「现在在做什么」就显示它，否则有子代理在跑显示「开发中」，没有显示「待命」。
- 点「详情」展开：模式、现在在做、在跑的子代理名单、token 用量。再点「收起」。
- 普通对话时显示「这个窗口自己直接改文件」；对接窗口、子代理窗口只显示一行说明，没有按钮。
- 主插件关掉时（环境变量 `AGENT_DEV_TEAM=off`，或项目里有 `.claude/agent-dev-team-off`）什么都不显示。

它和主插件分开安装：状态栏出问题不会影响主插件的守卫。

## 模式存在哪

和主插件用同一个文件：`<状态目录>/sessions/<对话 id>.json`。状态目录是环境变量
`AGENT_DEV_TEAM_HOME`，没有就是 `~/.claude/agent-dev-team`。点按钮只改 `role` / `auto`
两个键，文件里别的内容原样保留。主插件或命令行改了文件，状态栏在每轮结束、子代理开始 / 结束、
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

在 `~/.claude/settings.json` 的 `env` 里加一行（路径换成你电脑上这个文件夹的完整路径）：

```json
{ "env": { "CLAUDE_CODE_PLUGIN_DIRS": "/path/to/agent-dev-team/statusbar" } }
```

以后每次打开 Claude Code（包括桌面版）都会带上它。如果这个变量里已经有别的路径，用英文冒号 `:` 隔开接在后面。

## 卸载

- 把那一行从 `~/.claude/settings.json` 删掉，重开 Claude Code。
- 旧版 `lead-status` 和它功能重复，装了新版就把旧版卸掉，免得输入框上方出现两行。

## 开发

在这个文件夹里：

```bash
claude plugin validate .   # 检查插件
claude plugin test .       # 跑 tests/ 里的测试（不读写真实的状态目录）
```

类型检查：用 `--plugin-dir` 打开过一次后，引擎会在 `.claude-plugin/types/` 放好类型文件，
然后 `tsc -p .` 即可。
