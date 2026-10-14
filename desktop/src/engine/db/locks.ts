import type { Db } from './db.js'
import { recordEvent, type TaskRow, transition } from './states.js'

// §S3 / §S14: one writer at a time. Whoever runs tasks holds the writer lock and beats its
// heartbeat; a holder that died or stopped beating loses it, and its running tasks come back.

export const WRITER_LOCK = 'writer'
/** A lock whose heartbeat is older than this is broken (§S3). */
export const LOCK_STALE_MS = 2 * 60_000
/** How often a holder should beat: well inside LOCK_STALE_MS. */
export const HEARTBEAT_MS = 30_000

export interface LockRow {
  name: string
  pid: number
  host: string
  purpose: string | null
  acquired_at: string
  heartbeat_at: string
}

export class LockHeld extends Error {
  constructor(readonly holder: LockRow) {
    super(
      `${holder.purpose ?? 'another dld'} (pid ${holder.pid} on ${holder.host}) is already running; it started ${holder.acquired_at}. Wait for it to finish, or stop it.`,
    )
    this.name = 'LockHeld'
  }
}

export function getLock(db: Db, name: string = WRITER_LOCK): LockRow | undefined {
  return db.one<LockRow>('SELECT * FROM locks WHERE name = ?', name)
}

/** Why the holder no longer counts, or null when it is alive. */
export function staleReason(db: Db, lock: LockRow): string | null {
  const age = db.clock().getTime() - Date.parse(lock.heartbeat_at)
  if (lock.host === db.process.host && !db.process.isAlive(lock.pid)) {
    return `holder pid ${lock.pid} is gone`
  }
  if (!(age <= LOCK_STALE_MS)) return `heartbeat is ${Math.round(age / 1000)} s old`
  return null
}

function breakLock(db: Db, lock: LockRow, reason: string): void {
  db.run(
    'DELETE FROM locks WHERE name = ? AND pid = ? AND host = ?',
    lock.name,
    lock.pid,
    lock.host,
  )
  recordEvent(db, {
    entity: 'lock',
    entityId: lock.name,
    from: 'held',
    to: 'broken',
    reason,
    data: {
      pid: lock.pid,
      host: lock.host,
      purpose: lock.purpose,
      heartbeat_at: lock.heartbeat_at,
    },
  })
}

export interface Recovery {
  brokenLock: { lock: LockRow; reason: string } | null
  /** Tasks that were running for a writer that is gone, now ready again. */
  recovered: TaskRow[]
}

/**
 * Crash recovery (§S3), run at open and before taking the writer lock: when the writer is gone (no
 * lock, a dead pid on this host, or a heartbeat older than 2 minutes), its lock is broken and
 * every running task moves to failed ("interrupted"), then to ready.
 */
export function recoverOrphans(db: Db): Recovery {
  return db.tx(() => {
    const lock = getLock(db)
    let brokenLock: Recovery['brokenLock'] = null
    if (lock) {
      const reason = staleReason(db, lock)
      if (!reason) return { brokenLock: null, recovered: [] }
      breakLock(db, lock, reason)
      brokenLock = { lock, reason }
    }
    const running = db.all<{ id: number }>(
      "SELECT id FROM tasks WHERE state = 'running' ORDER BY id",
    )
    const recovered = running.map(({ id }) => {
      transition(db, id, 'failed', {
        reason: 'interrupted',
        data: brokenLock ? { lock: brokenLock.reason } : { lock: 'none held' },
      })
      return transition(db, id, 'ready', { reason: 'recovered after an interrupted run' })
    })
    return { brokenLock, recovered }
  })
}

/** Takes the writer lock (or another named lock). Throws LockHeld while a live holder has it. */
export function acquireLock(db: Db, purpose: string, name: string = WRITER_LOCK): LockRow {
  return db.tx(() => {
    if (name === WRITER_LOCK) recoverOrphans(db)
    const held = getLock(db, name)
    if (held) {
      const reason = staleReason(db, held)
      if (!reason) throw new LockHeld(held)
      breakLock(db, held, reason)
    }
    const now = db.now()
    db.run(
      'INSERT INTO locks (name, pid, host, purpose, acquired_at, heartbeat_at) VALUES (?, ?, ?, ?, ?, ?)',
      name,
      db.process.pid,
      db.process.host,
      purpose,
      now,
      now,
    )
    recordEvent(db, { entity: 'lock', entityId: name, from: null, to: 'held', reason: purpose })
    return getLock(db, name) as LockRow
  })
}

/** Beats the lock's heartbeat. False when it was broken meanwhile: the holder must stop writing. */
export function heartbeat(db: Db, lock: LockRow): boolean {
  const { changes } = db.run(
    'UPDATE locks SET heartbeat_at = ? WHERE name = ? AND pid = ? AND host = ?',
    db.now(),
    lock.name,
    lock.pid,
    lock.host,
  )
  return changes === 1
}

export function releaseLock(db: Db, lock: LockRow): void {
  db.tx(() => {
    const { changes } = db.run(
      'DELETE FROM locks WHERE name = ? AND pid = ? AND host = ?',
      lock.name,
      lock.pid,
      lock.host,
    )
    if (changes)
      recordEvent(db, { entity: 'lock', entityId: lock.name, from: 'held', to: 'released' })
  })
}

/** Holds a lock while `fn` runs, beating the heartbeat every HEARTBEAT_MS. */
export async function withLock<T>(
  db: Db,
  purpose: string,
  fn: (lock: LockRow) => Promise<T>,
  name: string = WRITER_LOCK,
): Promise<T> {
  const lock = acquireLock(db, purpose, name)
  const timer = setInterval(() => heartbeat(db, lock), HEARTBEAT_MS)
  timer.unref()
  try {
    return await fn(lock)
  } finally {
    clearInterval(timer)
    releaseLock(db, lock)
  }
}
