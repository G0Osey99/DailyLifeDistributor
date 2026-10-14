import type { Db } from './db.js'

// §S3 task states (plan "Architecture"). transition() is the only way a task changes state.

export const TASK_STATES = [
  'waiting',
  'ready',
  'running',
  'scheduled',
  'verified',
  'failed',
  'needs_login',
  'pulled',
] as const
export type TaskState = (typeof TASK_STATES)[number]

export const MODES = ['plan', 'drafts', 'live'] as const
export type Mode = (typeof MODES)[number]

/**
 * The allowed moves, exactly as §S3:
 * waiting → ready → running → scheduled → verified; running → failed | needs_login | waiting;
 * failed → ready; needs_login → ready; scheduled | verified → running (re-ship, edit); any → pulled.
 */
export const NEXT_STATES: Readonly<Record<TaskState, readonly TaskState[]>> = {
  waiting: ['ready', 'pulled'],
  ready: ['running', 'pulled'],
  running: ['scheduled', 'failed', 'needs_login', 'waiting', 'pulled'],
  scheduled: ['verified', 'running', 'pulled'],
  verified: ['running', 'pulled'],
  failed: ['ready', 'pulled'],
  needs_login: ['ready', 'pulled'],
  pulled: [],
}

export function canTransition(from: TaskState, to: TaskState): boolean {
  return NEXT_STATES[from].includes(to)
}

export class IllegalTransition extends Error {
  constructor(
    readonly taskId: number,
    readonly from: TaskState,
    readonly to: TaskState,
  ) {
    const allowed = NEXT_STATES[from]
    super(
      `task ${taskId} can't go from ${from} to ${to}${allowed.length ? ` (from ${from}: ${allowed.join(', ')})` : ` (${from} is final)`}`,
    )
    this.name = 'IllegalTransition'
  }
}

export class TaskNotFound extends Error {
  constructor(readonly taskId: number) {
    super(`no task ${taskId}`)
    this.name = 'TaskNotFound'
  }
}

export interface TaskRow {
  id: number
  day_id: number
  destination: string
  state: TaskState
  mode: Mode
  attempts: number
  /** JSON: artifact → {value, origin}. */
  produced: string
  error: string | null
  created_at: string
  updated_at: string
  started_at: string | null
  finished_at: string | null
}

export interface TransitionOptions {
  reason?: string
  data?: unknown
}

/** Writes an events row. Callers inside a transaction get it atomically with their change. */
export function recordEvent(
  db: Db,
  event: {
    entity: string
    entityId: string | number
    from?: string | null
    to?: string | null
    reason?: string | undefined
    data?: unknown
  },
): void {
  db.run(
    'INSERT INTO events (entity, entity_id, from_state, to_state, reason, data, at) VALUES (?, ?, ?, ?, ?, ?, ?)',
    event.entity,
    String(event.entityId),
    event.from ?? null,
    event.to ?? null,
    event.reason ?? null,
    event.data === undefined ? null : JSON.stringify(event.data),
    db.now(),
  )
}

export function getTask(db: Db, taskId: number): TaskRow | undefined {
  return db.one<TaskRow>('SELECT * FROM tasks WHERE id = ?', taskId)
}

/**
 * Moves a task to `to` and records the event, in one transaction (§S3). Throws IllegalTransition
 * for a move §S3 doesn't allow; nothing is written then.
 */
export function transition(
  db: Db,
  taskId: number,
  to: TaskState,
  options: TransitionOptions = {},
): TaskRow {
  return db.tx(() => {
    const task = getTask(db, taskId)
    if (!task) throw new TaskNotFound(taskId)
    if (!canTransition(task.state, to)) throw new IllegalTransition(taskId, task.state, to)
    const now = db.now()
    const error = to === 'failed' || to === 'needs_login' ? (options.reason ?? to) : null
    db.run(
      `UPDATE tasks SET
         state = ?,
         updated_at = ?,
         attempts = attempts + (CASE WHEN ? = 'running' THEN 1 ELSE 0 END),
         started_at = CASE WHEN ? = 'running' THEN ? ELSE started_at END,
         finished_at = CASE WHEN ? = 'running' THEN NULL WHEN state = 'running' THEN ? ELSE finished_at END,
         error = CASE WHEN ? IN ('failed', 'needs_login') THEN ? WHEN ? IN ('scheduled', 'verified') THEN NULL ELSE error END
       WHERE id = ?`,
      to,
      now,
      to,
      to,
      now,
      to,
      now,
      to,
      error,
      to,
      taskId,
    )
    recordEvent(db, {
      entity: 'task',
      entityId: taskId,
      from: task.state,
      to,
      reason: options.reason,
      data: options.data,
    })
    return getTask(db, taskId) as TaskRow
  })
}

/** Creates a task in its first state (`ready`, or `waiting` for its needs), with a created event. */
export function createTask(
  db: Db,
  task: {
    dayId: number
    destination: string
    mode: Mode
    state?: 'ready' | 'waiting'
    reason?: string
  },
): TaskRow {
  return db.tx(() => {
    const now = db.now()
    const state = task.state ?? 'ready'
    const { lastId } = db.run(
      'INSERT INTO tasks (day_id, destination, state, mode, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)',
      task.dayId,
      task.destination,
      state,
      task.mode,
      now,
      now,
    )
    recordEvent(db, {
      entity: 'task',
      entityId: lastId,
      from: null,
      to: state,
      reason: task.reason ?? 'created',
      data: { mode: task.mode },
    })
    return getTask(db, lastId) as TaskRow
  })
}
