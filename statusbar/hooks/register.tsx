import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'

import type { RunningAgent, StatusView } from '../types'

// 状态栏：输入框上方一行，左边是模式按钮，中间是状态文字，右边「详情 / 收起」。
// 模式存在主插件定的对话状态文件里：<状态目录>/sessions/<session_id>.json，
// 状态目录 = AGENT_DEV_TEAM_HOME，没有就是 ~/.claude/agent-dev-team。
// 这里只读写这个文件；文件可能被主插件的钩子或命令行改，所以在会话开始、
// 每轮结束、子代理开始 / 结束时重新读一遍。

const view = atom({ plugin: 'agent-dev-team-status', key: 'view' } as const, null)
const agents = atom({ plugin: 'agent-dev-team-status', key: 'agents' } as const, [])
const expanded = atom({ plugin: 'agent-dev-team-status', key: 'expanded' } as const, false)

const ACTIVE = ['pending', 'running', 'waiting']
const ROLES = ['lead', 'normal', 'subagent', 'kuaixiu']
const AUTOS = ['full', 'semi']
// 小任务池攒够这么多个一起派（和负责人技能里的规则一样）
const POOL_BATCH = 8

type $ = EngineInterface
type Raw = Record<string, unknown>

// ---------- 文件位置 ----------

// 和主插件 common.state_home 一样：AGENT_DEV_TEAM_HOME 非空就用它，否则 ~/.claude/agent-dev-team
async function stateDir($: $): Promise<string> {
  const custom = (await $.env.get('AGENT_DEV_TEAM_HOME')) ?? ''
  if (custom !== '') return custom.length > 1 ? custom.replace(/\/+$/, '') : custom
  const home = (await $.env.get('HOME')) ?? ''
  return `${home}/.claude/agent-dev-team`
}

// 和主插件 common.session_path 一样：文件名里只留 A-Z a-z 0-9 _ . -，别的字符换成 _，空的叫 unknown
function sessionName(id: string): string {
  return id.replace(/[^A-Za-z0-9_.-]/g, '_') || 'unknown'
}

// 当前对话的状态文件
async function sessionFile($: $): Promise<string> {
  return `${await stateDir($)}/sessions/${sessionName(await $.session.id())}.json`
}

// 兼容旧做法：状态文件里没有角色、当前分支以 kuaixiu/ 开头时是对接窗口（和主插件一样）
async function onKuaixiuBranch($: $, cwd: string): Promise<boolean> {
  try {
    const r = await $.process.run(['git', 'rev-parse', '--abbrev-ref', 'HEAD'], { cwd, timeoutMs: 3000 })
    return r.exitCode === 0 && r.stdout.trim().startsWith('kuaixiu/')
  } catch {
    return false
  }
}

// 项目根：git 仓库根；不在仓库里就是当前目录（和主插件同一个说法）
async function projectRoot($: $): Promise<string> {
  const cwd = await $.session.cwd()
  try {
    const top = await $.process.run(['git', 'rev-parse', '--show-toplevel'], { cwd, timeoutMs: 3000 })
    const root = top.stdout.trim()
    if (top.exitCode === 0 && root !== '') return root
  } catch {}
  return cwd
}

async function readJson($: $, path: string): Promise<Raw> {
  try {
    const text = await $.fs.read(path)
    const data: unknown = JSON.parse(typeof text === 'string' ? text : '')
    if (data !== null && typeof data === 'object' && !Array.isArray(data)) return data as Raw
  } catch {}
  return {}
}

// ---------- 读状态 ----------

async function loadView($: $): Promise<StatusView> {
  const root = await projectRoot($)
  // 和主插件 common.guard_off 一样：AGENT_DEV_TEAM 是 off（不分大小写），或项目根有关闭文件
  let off = ((await $.env.get('AGENT_DEV_TEAM')) ?? '').toLowerCase() === 'off'
  if (!off) {
    try {
      off = await $.fs.exists(`${root}/.claude/agent-dev-team-off`)
    } catch {}
  }
  const data = await readJson($, await sessionFile($))
  // auto 的默认：项目配置的 default_auto，没有就是 full
  let auto = typeof data.auto === 'string' && AUTOS.includes(data.auto) ? data.auto : undefined
  if (auto === undefined) {
    const cfg = await readJson($, `${root}/.claude/agent-dev-team.json`)
    auto = typeof cfg.default_auto === 'string' && AUTOS.includes(cfg.default_auto) ? cfg.default_auto : 'full'
  }
  let role = typeof data.role === 'string' && ROLES.includes(data.role) ? data.role : undefined
  if (role === undefined) role = (await onKuaixiuBranch($, await $.session.cwd())) ? 'kuaixiu' : 'lead'
  const status = typeof data.status === 'string' && data.status.trim() !== '' ? data.status.trim() : null
  // pool：小任务池里攒了几个，非负整数；没有或不对当 0
  const pool = typeof data.pool === 'number' && Number.isInteger(data.pool) && data.pool >= 0 ? data.pool : 0
  // 子代理窗口：主插件的钩子从派活单里存下的子代理名字、档位、任务名；没有或不是字符串当 null
  const agent = text(data.agent)
  const effort = text(data.effort)
  const task = text(data.task)
  return { off, role, auto, status, pool, agent, effort, task }
}

function text(x: unknown): string | null {
  return typeof x === 'string' && x.trim() !== '' ? x.trim() : null
}

type Row = { id: string; type: string; teammateId?: string; status: string; name?: string; description: string }

// 在跑的子代理（不算队友）。
// include：刚启动的（启动事件到来时名单里可能还没有）；exclude：刚结束的（名单里可能还留着）
function running(list: readonly Row[], include?: RunningAgent, exclude?: string): RunningAgent[] {
  const out = new Map<string, string>()
  for (const a of list) {
    if (a.teammateId !== undefined || a.type === 'teammate' || !ACTIVE.includes(a.status)) continue
    out.set(a.id, a.name || a.description || a.type)
  }
  if (include !== undefined && !out.has(include.id)) out.set(include.id, include.label)
  if (exclude !== undefined) out.delete(exclude)
  return [...out].map(([id, label]) => ({ id, label }))
}

async function refresh($: $, include?: RunningAgent, exclude?: string): Promise<void> {
  try {
    const v = await loadView($)
    const list = v.off ? [] : running(await $.agent.list(), include, exclude)
    await update($, view, () => v)
    await update($, agents, () => list)
  } catch {}
}

// ---------- 写状态（按钮） ----------

// 只改给出的键，其余键（status、pool……）原样保留
async function writeMode($: $, patch: { role?: string; auto?: string }): Promise<void> {
  const file = await sessionFile($)
  const data = { ...(await readJson($, file)), ...patch }
  await $.fs.write(file, JSON.stringify(data, null, 2) + '\n')
  await refresh($)
}

// ---------- 文字 ----------

// 负责人写的 status 是「<节点>：<任务名>」：冒号（中文或英文）前是大字，后是小字；没有冒号整句是大字。
// node：是不是「节点：任务」这种写法
function splitStatus(status: string): { big: string; small: string | null; node: boolean } {
  const m = /^([^:：]+)[:：](.*)$/.exec(status)
  if (m === null) return { big: status, small: null, node: false }
  const small = (m[2] ?? '').trim()
  return { big: (m[1] ?? '').trim(), small: small === '' ? null : small, node: true }
}

// 没写 status 时的默认说法
function idleWord(n: number): string {
  return n > 0 ? '开发中' : '待命'
}

// 收起那一行的状态词：写了「节点：任务」就是「节点中」（验收 → 验收中）；整句就原样
function statusWord(v: StatusView, n: number): string {
  if (v.status === null) return idleWord(n)
  const { big, node } = splitStatus(v.status)
  if (!node || big.endsWith('中')) return big
  return `${big}中`
}

// 收起那一行「●」后面的后半句：有子代理在跑时带上个数
function agentsNote(n: number): string | null {
  return n > 0 ? `· ${n} 个子代理在跑` : null
}

// 状态点的颜色：什么都没在做（没写 status、没有子代理）是灰色，别的用强调色
function dotColor(v: StatusView, n: number): string {
  return v.status === null && n === 0 ? 'inactive' : 'claude'
}

type Column = { key: string; title: string; big: string; small: string }

// 详情的三栏：现在在做 / 子代理 / 小任务池（模式就是上面那排按钮，不再重复）
function columns(v: StatusView, list: readonly RunningAgent[]): Column[] {
  const n = list.length
  const doing = v.status === null ? { big: idleWord(n), small: null } : splitStatus(v.status)
  return [
    { key: 'col-doing', title: '现在在做', big: doing.big, small: doing.small ?? '—' },
    {
      key: 'col-agents',
      title: '子代理',
      big: n > 0 ? `${n} 个在跑` : '没有在跑',
      small: n > 0 ? list.map(a => a.label).join('、') : '—',
    },
    { key: 'col-pool', title: '小任务池', big: `攒了 ${v.pool} 个`, small: `攒够 ${POOL_BATCH} 个一起派` },
  ]
}

// ---------- 注册 ----------

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    const r = await next(e)
    await refresh($)
    return r
  })

  // 每轮结束重新读（主插件的切换口令、命令行都可能改了文件）；子代理自己的轮次不算
  on('turn.complete', async ($, e, next) => {
    const r = await next(e)
    if (e.agentId === undefined) await refresh($)
    return r
  })

  on('classic.SubagentStart', async ($, e, next) => {
    const r = await next(e)
    await refresh($, { id: e.agent_id, label: e.agent_type })
    return r
  })

  on('classic.SubagentStop', async ($, e, next) => {
    const r = await next(e)
    await refresh($, undefined, e.agent_id)
    return r
  })

  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    if (e.props.hasSurvey) return next(e)
    const v = await read($, view)
    if (v === null || v.off) return next(e)
    const { Box, Button, Text } = $.ui.resolve(e)

    if (v.role === 'kuaixiu') return <Text dimColor>对接窗口 · 只做小改动，不能 push</Text>
    if (v.role === 'subagent') {
      if (v.agent === null) return <Text dimColor>子代理 · 只做派给它的任务，不能 push 和合并</Text>
      // 有子代理名字时：子代理 · dev-opus · 中档（强调色加粗）  任务名（灰色，长了截短）  说明（灰色）
      return (
        <Box key="sub" flexDirection="row" columnGap={1}>
          <Box key="sub-who" flexDirection="row" columnGap={1} flexShrink={0}>
            <Text dimColor>子代理 ·</Text>
            <Text bold color="claude">
              {v.effort === null ? v.agent : `${v.agent} · ${v.effort}档`}
            </Text>
          </Box>
          <Box key="sub-what" flexDirection="row" columnGap={1} flexShrink={1} overflow="hidden">
            {v.task !== null ? (
              <Text dimColor wrap="truncate-end">
                {`· ${v.task}`}
              </Text>
            ) : null}
            <Text dimColor wrap="truncate-end">
              · 只做派给它的任务，不能 push 和合并
            </Text>
          </Box>
        </Box>
      )
    }

    const list = await read($, agents)
    const isOpen = await read($, expanded)
    const isLead = v.role !== 'normal'
    const isFull = v.auto !== 'semi'
    const n = list.length
    const note = agentsNote(n)

    // 一组互斥按钮：选中的高亮，没选中的淡色；点已选中的不做事
    const seg = (key: string, label: string, active: boolean, press: () => Promise<void>) => (
      <Button
        key={key}
        label={label}
        variant={active ? 'primary' : undefined}
        dimColor={!active}
        onPress={() => (active ? undefined : press())}
      />
    )

    // 收起时这一行：[模式两键]  [自动两键]  ● 状态 · 子代理数 …… 详情
    // 按钮组不收缩；窄终端时只截短状态文字，不折行
    const row = (
      <Box key="row" flexDirection="row" columnGap={2} alignItems="center">
        <Box key="roles" flexDirection="row" flexShrink={0}>
          {seg('role-lead', '负责人模式', isLead, () => writeMode($, { role: 'lead' }))}
          {seg('role-normal', '普通对话', !isLead, () => writeMode($, { role: 'normal' }))}
        </Box>
        {isLead ? (
          <Box key="autos" flexDirection="row" flexShrink={0}>
            {seg('auto-full', '全自动', isFull, () => writeMode($, { auto: 'full' }))}
            {seg('auto-semi', '半自动', !isFull, () => writeMode($, { auto: 'semi' }))}
          </Box>
        ) : null}
        {isLead ? (
          <Box key="status" flexDirection="row" columnGap={1} flexShrink={1} overflow="hidden">
            <Text color={dotColor(v, n)}>●</Text>
            <Text bold wrap="truncate-end">
              {statusWord(v, n)}
            </Text>
            {note !== null ? (
              <Text dimColor wrap="truncate-end">
                {note}
              </Text>
            ) : null}
          </Box>
        ) : (
          <Text dimColor wrap="truncate-end">
            这个窗口自己直接改文件
          </Text>
        )}
        <Box key="gap" flexGrow={1} />
        <Button
          key="details"
          label={isOpen ? '收起' : '详情'}
          plain
          dimColor
          onPress={() => update($, expanded, x => !x)}
        />
      </Box>
    )
    if (!isOpen) return row

    // 展开：按钮行下面空一行，再是一张圆角卡片，三栏等宽，每栏 小标题 / 粗体大字 / 灰色小字
    return (
      <Box key="band" flexDirection="column">
        {row}
        <Box
          key="details-body"
          flexDirection="row"
          marginTop={1}
          borderStyle="round"
          borderDimColor
          paddingX={1}
        >
          {columns(v, list).map(c => (
            <Box key={c.key} flexDirection="column" width="33%" flexGrow={1} paddingRight={2}>
              <Text dimColor wrap="truncate-end">
                {c.title}
              </Text>
              <Text bold wrap="truncate-end">
                {c.big}
              </Text>
              <Text dimColor wrap="truncate-end">
                {c.small}
              </Text>
            </Box>
          ))}
        </Box>
      </Box>
    )
  })
}
