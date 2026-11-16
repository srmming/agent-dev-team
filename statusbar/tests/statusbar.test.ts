import { expect, test } from 'claude-code/testing'

// 这里没有真的文件系统：$.fs / $.env / $.process / $.agent 都由下面的假实现回答，
// 状态目录指到假的 /fake-home/adt，测试不会碰真实的 ~/.claude/agent-dev-team。

const HOME_DIR = '/fake-home/adt'
const SID = 'sess-1'
const FILE = `${HOME_DIR}/sessions/${SID}.json`
const ROOT = '/repo'

type AgentRow = { id: string; description: string; type: string; status: 'running' | 'completed'; name?: string }
const agent = (id: string, description = id, status: AgentRow['status'] = 'running'): AgentRow => ({
  id,
  description,
  type: 'general-purpose',
  status,
})

type Env = {
  file?: Record<string, unknown>
  agents?: AgentRow[]
  env?: Record<string, string>
  offFile?: boolean
  sid?: string
  branch?: string
}

const setup = (on: any, o: Env = {}) => {
  const files = new Map<string, string>()
  if (o.file !== undefined) files.set(FILE, JSON.stringify(o.file))
  const sid = o.sid ?? SID
  const branch = o.branch ?? 'main'
  if (o.offFile) files.set(`${ROOT}/.claude/agent-dev-team-off`, '')
  const env: Record<string, string> = { AGENT_DEV_TEAM_HOME: HOME_DIR, HOME: '/real-home', ...(o.env ?? {}) }
  const writes: string[] = []
  const agents = o.agents ?? []

  on('session.id', () => ({ value: sid }))
  on('session.cwd', () => ({ value: ROOT }))
  on('env.get', (_: unknown, e: { name: string }) => ({ value: env[e.name] }))
  // git rev-parse --show-toplevel 回答仓库根；--abbrev-ref HEAD 回答当前分支
  on('process.run', (_: unknown, e: { argv: readonly string[] }) => ({
    value: { exitCode: 0, stdout: (e.argv.includes('--abbrev-ref') ? branch : ROOT) + '\n', stderr: '' },
  }))
  on('fs.exists', (_: unknown, e: { path: string }) => ({ value: files.has(e.path) }))
  on('fs.read', (_: unknown, e: { path: string }) => {
    const text = files.get(e.path)
    if (text === undefined) throw new Error('ENOENT')
    return { value: text }
  })
  on('fs.write', (_: unknown, e: { path: string; text: string }) => {
    writes.push(e.path)
    files.set(e.path, e.text)
    return { value: undefined }
  })
  on('agent.list', () => ({ value: agents }))
  on('session.start', (_: unknown, e: { cwd: string }) => ({ cwd: e.cwd }))
  on('turn.complete', () => ({ text: '' }))
  on('classic.SubagentStart', () => ({}))
  on('classic.SubagentStop', () => ({}))

  return {
    agents,
    writes,
    files,
    saved: () => JSON.parse(files.get(FILE) ?? '{}') as Record<string, unknown>,
    // 模拟别的进程（主插件钩子 / 命令行）改了状态文件
    setFile: (data: Record<string, unknown>) => files.set(FILE, JSON.stringify(data)),
  }
}

const BAND = {
  plugin: 'agent-dev-team-status',
  component: 'AbovePrompt',
  requestId: 'agent-dev-team-status',
  props: {
    hasSurvey: false,
    isCollapsed: false,
    isWorking: false,
    maxRows: 10,
    bodyColumns: 100,
    scroll: { offset: 0, bodyRows: 10 },
    view: {},
  },
} as const

const SURFACES = ['terminal', 'desktop'] as const

const start = ($: any) => $.session.start({ cwd: ROOT, surface: 'desktop', isInteractive: true })
const base = { session_id: SID, transcript_path: '/t', cwd: ROOT }
const turnEnd = ($: any) =>
  $.turn.complete({ answer: '', durationMs: 1, isAborted: false, turnId: 't1', reason: 'answer' })

// 把一次画出来的结果整理成好断言的样子
const look = async (ui: any) => {
  const buttons = await ui.findAll({ type: 'Button' })
  const texts = (await ui.findAll({ type: 'Text' })).map((t: any) => t.text)
  const btn = (key: string) => buttons.find((b: any) => b.key === key)
  return {
    keys: buttons.map((b: any) => b.key),
    // 高亮 = variant primary；淡色 = dimColor
    isOn: (key: string) => btn(key)?.props.variant === 'primary',
    isDim: (key: string) => btn(key)?.props.dimColor === true,
    label: (key: string) => btn(key)?.text,
    texts,
    all: texts.join(' | '),
  }
}

const mount = ($: any, surface: string) => $.ui.mount({ ...BAND, surface })

test('默认（没有状态文件）：负责人模式 + 全自动 + 待命', async ($, on) => {
  setup(on)
  await start($)
  for (const surface of SURFACES) {
    const ui = await mount($, surface)
    const l = await look(ui)
    expect(l.keys).toEqual(['role-lead', 'role-normal', 'auto-full', 'auto-semi', 'details'])
    expect(l.isOn('role-lead')).toBe(true)
    expect(l.isDim('role-normal')).toBe(true)
    expect(l.isOn('auto-full')).toBe(true)
    expect(l.isDim('auto-semi')).toBe(true)
    expect(l.texts).toContain('● 待命')
    expect(l.label('details')).toBe('详情')
    await ui.unmount()
  }
})

test('状态文件 role=lead auto=semi：半自动高亮', async ($, on) => {
  setup(on, { file: { role: 'lead', auto: 'semi' } })
  await start($)
  for (const surface of SURFACES) {
    const ui = await mount($, surface)
    const l = await look(ui)
    expect(l.isOn('auto-semi')).toBe(true)
    expect(l.isDim('auto-full')).toBe(true)
    expect(l.isOn('role-lead')).toBe(true)
    await ui.unmount()
  }
})

test('状态文件 role=normal：普通对话高亮，没有全自动 / 半自动，显示提示语', async ($, on) => {
  setup(on, { file: { role: 'normal', auto: 'semi' } })
  await start($)
  for (const surface of SURFACES) {
    const ui = await mount($, surface)
    const l = await look(ui)
    expect(l.keys).toEqual(['role-lead', 'role-normal', 'details'])
    expect(l.isOn('role-normal')).toBe(true)
    expect(l.isDim('role-lead')).toBe(true)
    expect(l.texts).toContain('这个窗口自己直接改文件')
    await ui.unmount()
  }
})

test('状态文件 role=kuaixiu：只显示对接窗口说明，没有按钮', async ($, on) => {
  setup(on, { file: { role: 'kuaixiu' } })
  await start($)
  for (const surface of SURFACES) {
    const ui = await mount($, surface)
    const l = await look(ui)
    expect(l.keys).toEqual([])
    expect(l.all).toBe('对接窗口 · 只做小改动，不能 push')
    await ui.unmount()
  }
})

test('状态文件 role=subagent：只显示子代理说明，没有按钮', async ($, on) => {
  setup(on, { file: { role: 'subagent', auto: 'full' } })
  await start($)
  for (const surface of SURFACES) {
    const ui = await mount($, surface)
    const l = await look(ui)
    expect(l.keys).toEqual([])
    expect(l.all).toBe('子代理 · 只做派给它的任务，不能 push 和合并')
    await ui.unmount()
  }
})

test('状态文件里不认识的值按默认处理；status 键优先显示', async ($, on) => {
  setup(on, { file: { role: 'boss', auto: 'maybe', status: '在改登录页' } })
  await start($)
  const ui = await mount($, 'desktop')
  const l = await look(ui)
  expect(l.isOn('role-lead')).toBe(true)
  expect(l.isOn('auto-full')).toBe(true)
  expect(l.texts).toContain('● 在改登录页')
  await ui.unmount()
})

test('点「普通对话」：状态文件 role 变 normal，别的键都保留', async ($, on) => {
  const s = setup(on)
  await start($)
  for (const surface of SURFACES) {
    s.setFile({ role: 'lead', auto: 'semi', status: '在改登录页', tokens: 1234, extra: { a: 1 } })
    await turnEnd($)
    const ui = await mount($, surface)
    await ui.press({ key: 'role-normal' })
    expect(s.saved()).toEqual({ role: 'normal', auto: 'semi', status: '在改登录页', tokens: 1234, extra: { a: 1 } })
    const l = await look(ui)
    expect(l.isOn('role-normal')).toBe(true)
    expect(l.keys).not.toContain('auto-full')

    // 再点回「负责人模式」
    await ui.press({ key: 'role-lead' })
    expect(s.saved().role).toBe('lead')
    expect((await look(ui)).isOn('role-lead')).toBe(true)
    await ui.unmount()
  }
})

test('没有状态文件时点「普通对话」：新建文件，只写 role', async ($, on) => {
  const s = setup(on)
  await start($)
  const ui = await mount($, 'desktop')
  await ui.press({ key: 'role-normal' })
  expect(s.saved()).toEqual({ role: 'normal' })
  // 所有写入都在 AGENT_DEV_TEAM_HOME 指的目录里，没碰 HOME 下的真实目录
  expect(s.writes).toEqual([FILE])
  await ui.unmount()
})

test('点全自动 / 半自动来回切换', async ($, on) => {
  const s = setup(on, { file: { role: 'lead', status: '在改登录页' } })
  await start($)
  for (const surface of SURFACES) {
    const ui = await mount($, surface)
    await ui.press({ key: 'auto-semi' })
    expect(s.saved()).toEqual({ role: 'lead', status: '在改登录页', auto: 'semi' })
    expect((await look(ui)).isOn('auto-semi')).toBe(true)
    await ui.press({ key: 'auto-full' })
    expect(s.saved()).toEqual({ role: 'lead', status: '在改登录页', auto: 'full' })
    expect((await look(ui)).isOn('auto-full')).toBe(true)
    await ui.unmount()
  }
})

test('0 / 1 / 2 个子代理的文字', async ($, on) => {
  const s = setup(on)
  await start($)
  const text = async () => {
    const ui = await mount($, 'desktop')
    const t = (await look(ui)).texts.find((x: string) => x.startsWith('●'))
    await ui.unmount()
    return t
  }
  expect(await text()).toBe('● 待命')

  s.agents.push(agent('a1', '改登录页'))
  await $.classic.SubagentStart({ ...base, agent_id: 'a1', agent_type: 'general-purpose' })
  expect(await text()).toBe('● 开发中 · 1 个子代理在跑')

  // 名单里还没有刚启动的子代理，也要算上
  await $.classic.SubagentStart({ ...base, agent_id: 'a2', agent_type: 'Explore' })
  expect(await text()).toBe('● 开发中 · 2 个子代理在跑')

  // 结束的那个名单里还留着，也不重复算
  s.agents.push(agent('a2', '查代码'))
  await $.classic.SubagentStop({
    ...base,
    stop_hook_active: false,
    agent_id: 'a1',
    agent_transcript_path: '/a1',
    agent_type: 'general-purpose',
  })
  expect(await text()).toBe('● 开发中 · 1 个子代理在跑')

  s.agents.length = 0
  s.agents.push(agent('a1', '改登录页', 'completed'), agent('a2', '查代码', 'completed'))
  await $.classic.SubagentStop({
    ...base,
    stop_hook_active: false,
    agent_id: 'a2',
    agent_transcript_path: '/a2',
    agent_type: 'Explore',
  })
  expect(await text()).toBe('● 待命')
})

test('status 键有值时用它，后面带子代理数', async ($, on) => {
  setup(on, { file: { status: '等你确认' }, agents: [agent('a1'), agent('a2')] })
  await start($)
  const ui = await mount($, 'desktop')
  expect((await look(ui)).texts).toContain('● 等你确认 · 2 个子代理在跑')
  await ui.unmount()
})

test('主插件关掉（AGENT_DEV_TEAM=off）：不显示', async ($, on) => {
  setup(on, { env: { AGENT_DEV_TEAM: 'OFF' }, agents: [agent('a1')] })
  await start($)
  for (const surface of SURFACES) {
    const ui = await mount($, surface)
    expect(await ui.find({ type: 'Button' })).toBeUndefined()
    expect(await ui.find({ type: 'Text' })).toBeUndefined()
    await ui.unmount()
  }
})

test('主插件关掉（项目根有 .claude/agent-dev-team-off）：不显示', async ($, on) => {
  setup(on, { offFile: true, file: { role: 'lead' } })
  await start($)
  const ui = await mount($, 'desktop')
  expect(await ui.find({ type: 'Button' })).toBeUndefined()
  expect(await ui.find({ type: 'Text' })).toBeUndefined()
  await ui.unmount()
})

test('详情展开和收起', async ($, on) => {
  setup(on, {
    file: { role: 'lead', auto: 'semi', status: '在改登录页', tokens: 123456 },
    agents: [agent('a1', '改登录页'), { ...agent('a2', '查代码'), name: 'checker' }],
  })
  await start($)
  for (const surface of SURFACES) {
    const ui = await mount($, surface)
    expect((await look(ui)).all).not.toContain('模式：')

    await ui.press({ key: 'details' })
    let l = await look(ui)
    expect(l.label('details')).toBe('收起')
    expect(l.texts).toContain('模式：负责人模式 · 半自动')
    expect(l.texts).toContain('现在在做：在改登录页')
    expect(l.texts).toContain('在跑的子代理：改登录页、checker')
    expect(l.texts).toContain('token 用量：123,456')

    await ui.press({ key: 'details' })
    l = await look(ui)
    expect(l.label('details')).toBe('详情')
    expect(l.all).not.toContain('模式：')
    await ui.unmount()
  }
})

test('详情里没有的值显示「—」', async ($, on) => {
  setup(on)
  await start($)
  const ui = await mount($, 'desktop')
  await ui.press({ key: 'details' })
  const l = await look(ui)
  expect(l.texts).toContain('模式：负责人模式 · 全自动')
  expect(l.texts).toContain('现在在做：—')
  expect(l.texts).toContain('在跑的子代理：没有')
  expect(l.texts).toContain('token 用量：—')
  await ui.unmount()
})

test('别的进程改了状态文件，每轮结束后显示跟上', async ($, on) => {
  const s = setup(on)
  await start($)
  s.setFile({ role: 'normal', status: 'x' })
  await turnEnd($)
  const ui = await mount($, 'desktop')
  expect((await look(ui)).isOn('role-normal')).toBe(true)
  await ui.unmount()

  s.setFile({ role: 'kuaixiu' })
  await turnEnd($)
  const ui2 = await mount($, 'desktop')
  expect((await look(ui2)).all).toBe('对接窗口 · 只做小改动，不能 push')
  await ui2.unmount()
})

test('和主插件一样：session id 里的怪字符换成 _，写到同一个文件', async ($, on) => {
  const s = setup(on, { sid: '../a b:c' })
  await start($)
  const ui = await mount($, 'desktop')
  await ui.press({ key: 'role-normal' })
  const name = `${HOME_DIR}/sessions/.._a_b_c.json`
  expect(s.writes).toEqual([name])
  expect(JSON.parse(s.files.get(name) ?? '{}')).toEqual({ role: 'normal' })
  await ui.unmount()
})

test('和主插件一样：状态文件没写角色、分支是 kuaixiu/ 开头时是对接窗口', async ($, on) => {
  setup(on, { branch: 'kuaixiu/fix', file: { auto: 'semi' } })
  await start($)
  const ui = await mount($, 'desktop')
  expect((await look(ui)).all).toBe('对接窗口 · 只做小改动，不能 push')
  await ui.unmount()
})

test('状态文件写了角色时比分支名优先', async ($, on) => {
  setup(on, { branch: 'kuaixiu/fix', file: { role: 'lead' } })
  await start($)
  const ui = await mount($, 'desktop')
  expect((await look(ui)).isOn('role-lead')).toBe(true)
  await ui.unmount()
})

test('项目配置 default_auto=semi：没写 auto 时默认半自动', async ($, on) => {
  const s = setup(on, { file: { role: 'lead' } })
  s.files.set(`${ROOT}/.claude/agent-dev-team.json`, JSON.stringify({ default_auto: 'semi' }))
  await start($)
  const ui = await mount($, 'desktop')
  expect((await look(ui)).isOn('auto-semi')).toBe(true)
  await ui.unmount()
})

test('有问卷占着输入框上方时让路', async ($, on) => {
  setup(on)
  await start($)
  const ui = await $.ui.mount({ ...BAND, props: { ...BAND.props, hasSurvey: true }, surface: 'desktop' })
  expect(await ui.find({ type: 'Button' })).toBeUndefined()
  await ui.unmount()
})
