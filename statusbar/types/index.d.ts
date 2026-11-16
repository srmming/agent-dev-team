// 状态栏画图要读的值，放在 $.state 里：值一变，输入框上方那一行就重画；reload 后不丢。
//
// view：从对话状态文件读出来的那份（null = 还没读过）
//   off    主插件整个关掉了（AGENT_DEV_TEAM=off 或项目根有 .claude/agent-dev-team-off）
//   role   lead / normal / subagent / kuaixiu
//   auto   full / semi
//   status 状态文件里的 status 键（没有为 null），负责人写成「<节点>：<任务名>」
//   pool   状态文件里的 pool 键：小任务池攒了几个（没有为 0）
// agents：在跑的子代理，id 和显示的名字
// expanded：详情是否展开
export type StatusView = {
  off: boolean
  role: string
  auto: string
  status: string | null
  pool: number
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
