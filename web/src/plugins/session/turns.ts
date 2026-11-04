// Turn model for one job's timeline, after ZCode's conversation render units (packages/ui/src/v4/conversationTurnRenderUnits.ts):
// a turn = the user's inputs that started it + one work segment (our Assignment) whose process rows fold under a
// "已工作 N 秒" header, + the final text shown outside the fold, + a tail (reports that need the person, the agent's
// replies, time). Pure functions; the components only render what this returns.
import type { Report } from '../../api/types'

export interface TrajMsg { role: string; content: unknown; extra?: { actions?: { command?: string }[]; exit_status?: string; submission?: string } }
export interface ConvItem {
  ts: number; kind: 'input' | 'assignment' | 'report' | 'reply'; actor?: string; text?: string; input_kind?: string; id?: string; state?: string
  sandbox?: string | null; recovery?: boolean; error?: string | null; cost?: number; elapsed_s?: number | null; started?: number; ended?: number
  traj?: { messages?: TrajMsg[]; info?: { exit_status?: string } } | null; purpose?: string; assignment?: string; attention?: { question: string }
  deliverable?: { branch: string; head: string }; say?: string
}

export type RowKind = 'command' | 'output' | 'thought' | 'exit'
export interface WorkRow { key: string; kind: RowKind; text: string; command?: string; returncode?: number | null; failed?: boolean }
export type WorkState = 'running' | 'completed' | 'interrupted' | 'failed'
export interface WorkSegment {
  assignment: string; state: WorkState; startedAt: number; durationMs?: number; cost?: number; sandbox?: string | null; recovery?: boolean; error?: string | null
  history: WorkRow[]; finalText?: string; reports: Report[]
}
export interface Turn { key: string; inputs: ConvItem[]; segment?: WorkSegment; replies: ConvItem[]; endedAt?: number }

/** The harness's observation is JSON {returncode, output, exception_info?}; older rows carry it as text with <returncode> tags. */
export function parseObservation(text: string): { returncode: number | null; output: string } {
  try {
    const j = JSON.parse(text)
    if (j && typeof j === 'object' && 'output' in j) return { returncode: typeof j.returncode === 'number' ? j.returncode : null, output: String(j.output ?? '') + (j.exception_info ? `\n${j.exception_info}` : '') }
  } catch { /* not JSON */ }
  const m = text.match(/<returncode>(-?\d+)<\/returncode>/)
  return { returncode: m ? Number(m[1]) : null, output: text }
}

/** Trajectory messages → work rows: each assistant step is a thought (its prose) plus one command row per action; the
 *  next user/tool message is that command's output and is attached to it. The final submission is not a row. */
export function rowsFromTrajectory(messages: TrajMsg[] | undefined, key: string): { history: WorkRow[]; finalText?: string } {
  const history: WorkRow[] = []; let finalText: string | undefined
  let pendingCommand: WorkRow | null = null; let seenAssistant = false; let skipNextObservation = false
  ;(messages ?? []).forEach((m, i) => {
    const text = typeof m.content === 'string' ? m.content : JSON.stringify(m.content)
    if (m.role === 'assistant') {
      const cmds = (m.extra?.actions ?? []).map(a => a.command || '').filter(Boolean)
      const isSubmit = cmds.some(c => /COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT/.test(c)) || Boolean(m.extra?.submission)
      if (isSubmit) { finalText = (m.extra?.submission || text || '').trim() || undefined; pendingCommand = null; skipNextObservation = true; return }   // the submit "command" is not executed; its observation says so
      seenAssistant = true
      if (text.trim()) history.push({ key: `${key}:${i}:t`, kind: 'thought', text: text.trim().slice(0, 4000) })
      cmds.forEach((c, j) => { const row: WorkRow = { key: `${key}:${i}:c${j}`, kind: 'command', text: '', command: c.slice(0, 4000) }; history.push(row); pendingCommand = row })
      return
    }
    if (m.role === 'user' || m.role === 'tool') {
      if (!seenAssistant) return                                                   // the task prompt itself is not a work row
      if (skipNextObservation) { skipNextObservation = false; return }
      const { returncode, output } = parseObservation(text)
      if (pendingCommand) { pendingCommand.text = output.slice(0, 20000); pendingCommand.returncode = returncode; pendingCommand.failed = returncode != null && returncode !== 0; pendingCommand = null }
      else if (i > 0 && output.trim()) history.push({ key: `${key}:${i}:o`, kind: 'output', text: output.slice(0, 20000), returncode, failed: returncode != null && returncode !== 0 })
      return
    }
    if (m.role === 'exit' && !finalText) history.push({ key: `${key}:${i}:x`, kind: 'exit', text: m.extra?.exit_status || text })   // after a submission the exit line says nothing new
  })
  return { history, finalText }
}

export function buildTurns(items: ConvItem[], opts: { running?: boolean; nowMs?: number } = {}): Turn[] {
  const sorted = [...items].sort((a, b) => a.ts - b.ts)
  const turns: Turn[] = []; let cur: Turn = { key: 't0', inputs: [], replies: [] }
  const reportsFor = new Map<string, Report[]>()
  for (const it of sorted) if (it.kind === 'report' && it.assignment) reportsFor.set(it.assignment, [...(reportsFor.get(it.assignment) ?? []), it as unknown as Report])
  for (const it of sorted) {
    if (it.kind === 'input') {
      if (cur.segment) { turns.push(cur); cur = { key: `t${turns.length}`, inputs: [], replies: [] } }     // a new input after work starts the next turn
      cur.inputs.push(it); continue
    }
    if (it.kind === 'assignment') {
      if (cur.segment) { turns.push(cur); cur = { key: `t${turns.length}`, inputs: [], replies: [] } }     // retries without a new input are their own turn
      const { history, finalText } = rowsFromTrajectory(it.traj?.messages, it.id || `a${turns.length}`)
      const reports = reportsFor.get(it.id || '') ?? []
      const last = reports[reports.length - 1]
      const running = it.state === 'running' || (it.state !== 'ended' && Boolean(opts.running))
      const state: WorkState = running ? 'running' : it.error && /stopped/i.test(it.error) ? 'interrupted' : it.error || last?.state === 'failed' || last?.state === 'blocked' ? 'failed' : 'completed'
      const startedAt = (it.started || it.ts) * 1000
      const durationMs = it.elapsed_s != null ? it.elapsed_s * 1000 : running && opts.nowMs ? Math.max(0, opts.nowMs - startedAt) : undefined
      cur.segment = { assignment: it.id || '', state, startedAt, durationMs, cost: it.cost, sandbox: it.sandbox, recovery: it.recovery, error: it.error, history, finalText: finalText || (last?.state === 'ready' ? last.text || undefined : undefined), reports }
      cur.endedAt = it.ended ? it.ended * 1000 : it.elapsed_s != null ? (it.ts + it.elapsed_s) * 1000 : undefined
      continue
    }
    if (it.kind === 'reply') cur.replies.push(it)
  }
  if (cur.inputs.length || cur.segment || cur.replies.length) turns.push(cur)
  for (const t of turns) {                                                       // a ready report without text: the "做完了" reply is the final text
    if (t.segment && !t.segment.finalText) { const i = t.replies.findIndex(r => (r.say || r.purpose) === 'ready' && r.text); if (i >= 0) { t.segment.finalText = t.replies[i].text; t.replies.splice(i, 1) } }
    if (t.segment?.finalText) t.segment.history = t.segment.history.filter(r => r.kind !== 'exit')   // a finished job's exit line says nothing the final text does not
  }
  return turns
}

export function formatDuration(ms: number | undefined): string {
  if (ms == null) return ''
  const s = Math.round(ms / 1000)
  if (s < 60) return `${s} 秒`
  const m = Math.floor(s / 60), r = s % 60
  if (m < 60) return r ? `${m} 分 ${r} 秒` : `${m} 分`
  const h = Math.floor(m / 60)
  return `${h} 小时 ${m % 60} 分`
}

/** ZCode: history folds by default once the turn is done and has a final text; it stays open while running, for the
 *  last turn without a final text, and for abnormal ends (so the failure context is not hidden). */
export function historyDefaultOpen(seg: WorkSegment, isLast: boolean): boolean {
  if (seg.state === 'running') return true
  if (seg.state === 'failed' || seg.state === 'interrupted') return true
  if (isLast && !seg.finalText) return true
  return false
}
