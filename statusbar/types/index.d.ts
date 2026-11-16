// 状态栏画图要读的值，放在 $.state 里：值一变，输入框上方那一行就重画；reload 后不丢。
//
// view：从对话状态文件读出来的那份（null = 还没读过）
//   off    主插件整个关掉了（AGENT_DEV_TEAM=off 或项目根有 .claude/agent-dev-team-off）
//   role   lead / normal / subagent / kuaixiu
//   auto   full / semi
//   status 状态文件里的 status 键（没有为 null），负责人写成「<节点>：<任务名>」
//   pool   状态文件里的 pool 键：小任务池攒了几个（没有为 0）
//   agent  子代理窗口：派活单第二行写的子代理名字（dev-opus 等，没有为 null）
//   effort 子代理窗口：派活单第二行写的档位（低 / 中 / 高，没有为 null）
//   task   子代理窗口：派活单的任务名（没有为 null）
// agents：在跑的子代理，id 和显示的名字
// expanded：详情是否展开
export type StatusView = {
  off: boolean
  role: string
  auto: string
  status: string | null
  pool: number
  agent: string | null
  effort: string | null
  task: string | null
}

export type RunningAgent = { id: string; label: string }

declare module 'claude-code' {
  interface PluginState {
    'agent-dev-team-status': {
      view: StatusView | null
      agents: RunningAgent[]
      expanded: boolean
    }
  }
}
