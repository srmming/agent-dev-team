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

function tokensText(v: unknown): string | null {
  if (typeof v === 'number' && Number.isFinite(v)) return v.toLocaleString('en-US')
  if (typeof v === 'string' && v.trim() !== '') return v.trim()
  return null
}

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
  return { off, role, auto, status, tokens: tokensText(data.tokens) }
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

// 只改给出的键，其余键（status、tokens……）原样保留
async function writeMode($: $, patch: { role?: string; auto?: string }): Promise<void> {
  const file = await sessionFile($)
  const data = { ...(await readJson($, file)), ...patch }
  await $.fs.write(file, JSON.stringify(data, null, 2) + '\n')
  await refresh($)
}

// ---------- 文字 ----------

function statusLine(v: StatusView, n: number): string {
  const word = v.status ?? (n > 0 ? '开发中' : '待命')
  return n > 0 ? `● ${word} · ${n} 个子代理在跑` : `● ${word}`
}

function modeName(v: StatusView): string {
  if (v.role === 'normal') return '普通对话'
  if (v.role === 'kuaixiu') return '对接窗口'
  if (v.role === 'subagent') return '子代理'
  return `负责人模式 · ${v.auto === 'semi' ? '半自动' : '全自动'}`
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
    if (v.role === 'subagent') return <Text dimColor>子代理 · 只做派给它的任务，不能 push 和合并</Text>

    const list = await read($, agents)
    const isOpen = await read($, expanded)
    const isLead = v.role !== 'normal'
    const isFull = v.auto !== 'semi'

    const row = (
      <Box key="row" flexDirection="row" columnGap={1} alignItems="center">
        <Button
          key="role-lead"
          label="负责人模式"
          variant={isLead ? 'primary' : undefined}
          dimColor={!isLead}
          onPress={() => (isLead ? undefined : writeMode($, { role: 'lead' }))}
        />
        <Button
          key="role-normal"
          label="普通对话"
          variant={isLead ? undefined : 'primary'}
          dimColor={isLead}
          onPress={() => (isLead ? writeMode($, { role: 'normal' }) : undefined)}
        />
        {isLead ? (
          <Box key="auto" flexDirection="row" marginLeft={1}>
            <Button
              key="auto-full"
              label="全自动"
              variant={isFull ? 'primary' : undefined}
              dimColor={!isFull}
              onPress={() => (isFull ? undefined : writeMode($, { auto: 'full' }))}
            />
            <Button
              key="auto-semi"
              label="半自动"
              variant={isFull ? undefined : 'primary'}
              dimColor={isFull}
              onPress={() => (isFull ? writeMode($, { auto: 'semi' }) : undefined)}
            />
          </Box>
        ) : null}
        {isLead ? <Text>{statusLine(v, list.length)}</Text> : <Text dimColor>这个窗口自己直接改文件</Text>}
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

    return (
      <Box key="band" flexDirection="column">
        {row}
        <Box key="details-body" flexDirection="column" paddingLeft={2}>
          <Text dimColor>模式：{modeName(v)}</Text>
          <Text dimColor>现在在做：{v.status ?? '—'}</Text>
          <Text dimColor>
            在跑的子代理：{list.length === 0 ? '没有' : list.map(a => a.label).join('、')}
          </Text>
          <Text dimColor>token 用量：{v.tokens ?? '—'}</Text>
        </Box>
      </Box>
    )
  })
}
