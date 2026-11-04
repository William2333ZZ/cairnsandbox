import { describe, expect, it } from 'vitest'
import { buildTurns, formatDuration, historyDefaultOpen, parseObservation, rowsFromTrajectory, type ConvItem } from './turns'

const traj = (final?: string) => ({ messages: [
  { role: 'system', content: 'sys' },
  { role: 'user', content: 'task text' },
  { role: 'assistant', content: 'I will look around.', extra: { actions: [{ command: 'ls' }] } },
  { role: 'user', content: JSON.stringify({ returncode: 0, output: 'a.py\nb.py' }) },
  { role: 'assistant', content: '', extra: { actions: [{ command: 'pytest' }] } },
  { role: 'user', content: JSON.stringify({ returncode: 1, output: 'FAILED' }) },
  ...(final ? [{ role: 'assistant', content: final, extra: { actions: [{ command: 'echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT' }] } }, { role: 'exit', content: 'Submitted' }] : []),
] })

describe('turns', () => {
  it('parses the harness observation and attaches output to its command', () => {
    expect(parseObservation('{"returncode": 2, "output": "x"}')).toEqual({ returncode: 2, output: 'x' })
    expect(parseObservation('<returncode>0</returncode>ok').returncode).toBe(0)
    const { history, finalText } = rowsFromTrajectory(traj('done: fixed').messages, 'a')
    expect(history.map(r => r.kind)).toEqual(['thought', 'command', 'command'])
    expect(history[1]).toMatchObject({ command: 'ls', text: 'a.py\nb.py', returncode: 0, failed: false })
    expect(history[2]).toMatchObject({ command: 'pytest', returncode: 1, failed: true })
    expect(finalText).toBe('done: fixed')
  })
  it('groups inputs, one assignment and its reports into a turn; a later input starts the next', () => {
    const items: ConvItem[] = [
      { ts: 1, kind: 'input', actor: 'me', text: 'fix it', input_kind: 'trigger' },
      { ts: 2, kind: 'assignment', id: 'a1', state: 'ended', started: 2, ended: 12, elapsed_s: 10, cost: 0.01, traj: traj('fixed') },
      { ts: 11.999, kind: 'report', assignment: 'a1', state: 'ready', text: 'fixed' } as ConvItem,
      { ts: 12, kind: 'reply', text: 'Opened PR #2', say: 'ready' },
      { ts: 20, kind: 'input', actor: 'me', text: 'also bump version', input_kind: 'answer' },
      { ts: 21, kind: 'assignment', id: 'a2', state: 'running', started: 21, traj: traj() },
    ]
    const turns = buildTurns(items, { running: true, nowMs: 31000 })
    expect(turns).toHaveLength(2)
    expect(turns[0].inputs.map(i => i.text)).toEqual(['fix it'])
    expect(turns[0].segment).toMatchObject({ assignment: 'a1', state: 'completed', durationMs: 10000, finalText: 'fixed' })
    expect(turns[0].segment!.reports).toHaveLength(1); expect(turns[0].replies).toHaveLength(1)
    const noText = buildTurns([{ ts: 2, kind: 'assignment', id: 'a1', state: 'ended', elapsed_s: 10, traj: traj() }, { ts: 12, kind: 'reply', text: 'Opened PR #2', say: 'ready' }])
    expect(noText[0].segment!.finalText).toBe('Opened PR #2'); expect(noText[0].replies).toHaveLength(0)
    expect(turns[1].inputs.map(i => i.text)).toEqual(['also bump version'])
    expect(turns[1].segment).toMatchObject({ assignment: 'a2', state: 'running', durationMs: 10000 })
    expect(historyDefaultOpen(turns[0].segment!, false)).toBe(false)
    expect(historyDefaultOpen(turns[1].segment!, true)).toBe(true)
  })
  it('marks failed and stopped segments and keeps their history open', () => {
    const t = buildTurns([{ ts: 1, kind: 'assignment', id: 'a', state: 'ended', error: 'stopped by a person', traj: traj() }])
    expect(t[0].segment!.state).toBe('interrupted'); expect(historyDefaultOpen(t[0].segment!, true)).toBe(true)
    const f = buildTurns([{ ts: 1, kind: 'assignment', id: 'a', state: 'ended', traj: traj() }, { ts: 2, kind: 'report', assignment: 'a', state: 'failed', text: 'budget' } as ConvItem])
    expect(f[0].segment!.state).toBe('failed')
  })
  it('formats durations the way the header reads them', () => {
    expect(formatDuration(4000)).toBe('4 秒'); expect(formatDuration(144000)).toBe('2 分 24 秒'); expect(formatDuration(3600000)).toBe('1 小时 0 分')
  })
})
