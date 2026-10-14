import { afterEach, describe, expect, it } from 'vitest'
import {
  createTask,
  type Db,
  getTask,
  IllegalTransition,
  listEvents,
  NEXT_STATES,
  TASK_STATES,
  type TaskState,
  transition,
} from '../src/engine/db/index.js'
import { seededStore } from './fakes/store.js'

let db: Db
let dayId: number
let nextDay = 0

afterEach(() => db?.close())

function fresh() {
  ;({ db, dayId } = seededStore())
}

/** A task already in `state` (set directly, as a fixture). */
function taskIn(state: TaskState): number {
  // One task per (day, destination): give each fixture task its own destination name.
  const task = createTask(db, { dayId, destination: `d${nextDay++}`, mode: 'drafts' })
  db.run('UPDATE tasks SET state = ? WHERE id = ?', state, task.id)
  return task.id
}

const ALLOWED: Array<[TaskState, TaskState]> = TASK_STATES.flatMap((from) =>
  NEXT_STATES[from].map((to): [TaskState, TaskState] => [from, to]),
)
const ILLEGAL: Array<[TaskState, TaskState]> = TASK_STATES.flatMap((from) =>
  TASK_STATES.filter((to) => !NEXT_STATES[from].includes(to)).map((to): [TaskState, TaskState] => [
    from,
    to,
  ]),
)

describe('task states (§S3)', () => {
  it('allows exactly the moves §S3 lists', () => {
    expect(ALLOWED).toEqual([
      ['waiting', 'ready'],
      ['waiting', 'pulled'],
      ['ready', 'running'],
      ['ready', 'pulled'],
      ['running', 'scheduled'],
      ['running', 'failed'],
      ['running', 'needs_login'],
      ['running', 'waiting'],
      ['running', 'pulled'],
      ['scheduled', 'verified'],
      ['scheduled', 'running'],
      ['scheduled', 'pulled'],
      ['verified', 'running'],
      ['verified', 'pulled'],
      ['failed', 'ready'],
      ['failed', 'pulled'],
      ['needs_login', 'ready'],
      ['needs_login', 'pulled'],
    ])
  })

  it.each(ALLOWED)('%s → %s passes and records its event', (from, to) => {
    fresh()
    const id = taskIn(from)
    const task = transition(db, id, to, { reason: 'test', data: { n: 1 } })
    expect(task.state).toBe(to)
    const events = listEvents(db, { entity: 'task', entityId: id })
    expect(events.at(-1)).toMatchObject({
      from_state: from,
      to_state: to,
      reason: 'test',
      data: '{"n":1}',
    })
  })

  it.each(ILLEGAL)('%s → %s throws IllegalTransition and writes nothing', (from, to) => {
    fresh()
    const id = taskIn(from)
    const before = listEvents(db).length
    expect(() => transition(db, id, to)).toThrow(IllegalTransition)
    expect(getTask(db, id)?.state).toBe(from)
    expect(listEvents(db)).toHaveLength(before)
  })

  it('names the move and what is allowed instead', () => {
    fresh()
    expect(() => transition(db, taskIn('waiting'), 'running')).toThrow(
      /can't go from waiting to running \(from waiting: ready, pulled\)/,
    )
    expect(() => transition(db, taskIn('pulled'), 'ready')).toThrow(/pulled is final/)
    expect(() => transition(db, 9999, 'ready')).toThrow('no task 9999')
  })

  it('counts attempts, stamps start and finish, and keeps the last error until a success', () => {
    fresh()
    const { id } = createTask(db, { dayId, destination: 'youtube', mode: 'drafts' })
    let t = transition(db, id, 'running')
    expect(t).toMatchObject({ attempts: 1, finished_at: null })
    expect(t.started_at).toBeTruthy()
    t = transition(db, id, 'failed', { reason: 'HTTP 503 from upload' })
    expect(t.error).toBe('HTTP 503 from upload')
    expect(t.finished_at).toBeTruthy()
    t = transition(db, id, 'ready')
    expect(t.error).toBe('HTTP 503 from upload')
    t = transition(db, id, 'running')
    expect(t.attempts).toBe(2)
    t = transition(db, id, 'scheduled')
    expect(t.error).toBeNull()
  })

  it('records the mode a task was created with (D5)', () => {
    fresh()
    const task = createTask(db, { dayId, destination: 'wistia', mode: 'drafts', state: 'waiting' })
    expect(task).toMatchObject({ mode: 'drafts', state: 'waiting' })
    expect(listEvents(db, { entity: 'task', entityId: task.id })).toMatchObject([
      { from_state: null, to_state: 'waiting', reason: 'created', data: '{"mode":"drafts"}' },
    ])
  })

  it('writes the state and its event atomically: a failure between them leaves neither', () => {
    fresh()
    const id = taskIn('ready')
    // The state UPDATE runs first; the events INSERT then fails.
    db.sql.exec(
      "CREATE TRIGGER inject_failure BEFORE INSERT ON events BEGIN SELECT RAISE(ABORT, 'injected failure'); END",
    )
    expect(() => transition(db, id, 'running')).toThrow('injected failure')
    expect(getTask(db, id)).toMatchObject({ state: 'ready', attempts: 0, started_at: null })
    db.sql.exec('DROP TRIGGER inject_failure')
    expect(transition(db, id, 'running').state).toBe('running')
  })
})
