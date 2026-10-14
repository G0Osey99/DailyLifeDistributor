import { afterEach, describe, expect, it } from 'vitest'
import {
  acquireLock,
  createTask,
  type Db,
  getLock,
  getTask,
  HEARTBEAT_MS,
  heartbeat,
  LOCK_STALE_MS,
  LockHeld,
  listEvents,
  open,
  releaseLock,
  transition,
  withLock,
} from '../src/engine/db/index.js'
import { fakeClock, fakeProcess, seededStore } from './fakes/store.js'

const opened: Db[] = []
afterEach(() => {
  for (const db of opened.splice(0)) db.close()
})

/** Two connections to one database, as two processes on one Mac. */
function twoProcesses() {
  const clock = fakeClock()
  const a = fakeProcess(1001)
  const b = fakeProcess(2002)
  const { db: dbA, path, dayId } = seededStore({ clock: clock.now, process: a.info })
  // Both see each other alive until a test says otherwise.
  a.alive.add(2002)
  b.alive.add(1001)
  const dbB = open(path, { clock: clock.now, process: b.info })
  opened.push(dbA, dbB)
  return { clock, a, b, dbA, dbB, path, dayId }
}

describe('writer lock (§S3, §S14)', () => {
  it('lets one holder in and tells the other who has it', () => {
    const { dbA, dbB } = twoProcesses()
    const lock = acquireLock(dbA, 'dld ship 2026-10-20')
    expect(lock).toMatchObject({ name: 'writer', pid: 1001, host: 'test-mac' })
    expect(() => acquireLock(dbB, 'dld ship')).toThrow(LockHeld)
    expect(() => acquireLock(dbB, 'dld ship')).toThrow(
      /dld ship 2026-10-20 \(pid 1001 on test-mac\) is already running/,
    )
    releaseLock(dbA, lock)
    expect(acquireLock(dbB, 'dld ship').pid).toBe(2002)
  })

  it('breaks a lock whose heartbeat is older than 2 minutes', () => {
    const { clock, dbA, dbB } = twoProcesses()
    const lock = acquireLock(dbA, 'dld ship')
    clock.advance(LOCK_STALE_MS)
    expect(() => acquireLock(dbB, 'dld ship')).toThrow(LockHeld)
    expect(heartbeat(dbA, lock)).toBe(true)
    clock.advance(LOCK_STALE_MS + 1000)
    expect(acquireLock(dbB, 'dld ship').pid).toBe(2002)
    expect(listEvents(dbB, { entity: 'lock' }).map((e) => [e.to_state, e.reason])).toEqual([
      ['held', 'dld ship'],
      ['broken', 'heartbeat is 121 s old'],
      ['held', 'dld ship'],
    ])
    // The old holder finds out on its next beat and must stop writing.
    expect(heartbeat(dbA, lock)).toBe(false)
  })

  it('breaks a lock at once when its holder pid is gone from this Mac', () => {
    const { b, dbA, dbB } = twoProcesses()
    acquireLock(dbA, 'dld ship')
    b.alive.delete(1001)
    expect(acquireLock(dbB, 'dld ship').pid).toBe(2002)
    expect(listEvents(dbB, { entity: 'lock' }).at(-2)?.reason).toBe('holder pid 1001 is gone')
  })

  it('beats while held and releases when done, even on failure', async () => {
    const { dbA } = twoProcesses()
    await expect(
      withLock(dbA, 'dld ship', async (lock) => {
        expect(getLock(dbA)?.pid).toBe(lock.pid)
        throw new Error('run failed')
      }),
    ).rejects.toThrow('run failed')
    expect(getLock(dbA)).toBeUndefined()
    expect(HEARTBEAT_MS).toBeLessThan(LOCK_STALE_MS / 2)
  })
})

describe('crash recovery at open (§S3)', () => {
  function runningTask(db: Db, dayId: number, destination = 'youtube'): number {
    const { id } = createTask(db, { dayId, destination, mode: 'drafts' })
    transition(db, id, 'running')
    return id
  }

  it('moves an orphaned running task to ready via failed ("interrupted") when its holder died', () => {
    const { b, dbA, dbB, path, clock, dayId } = twoProcesses()
    acquireLock(dbA, 'dld ship')
    const id = runningTask(dbA, dayId)
    // Process A is killed: its pid is gone. B opens the store.
    b.alive.delete(1001)
    dbB.close()
    const reopened = open(path, { clock: clock.now, process: b.info })
    opened.push(reopened)
    expect(getTask(reopened, id)?.state).toBe('ready')
    const moves = listEvents(reopened, { entity: 'task', entityId: id }).map((e) => [
      e.from_state,
      e.to_state,
      e.reason,
    ])
    expect(moves).toEqual([
      [null, 'ready', 'created'],
      ['ready', 'running', null],
      ['running', 'failed', 'interrupted'],
      ['failed', 'ready', 'recovered after an interrupted run'],
    ])
    expect(getLock(reopened)).toBeUndefined()
    expect(getTask(reopened, id)?.error).toBe('interrupted')
  })

  it('recovers running tasks left with no lock at all', () => {
    const { dbA, path, clock, a, dayId } = twoProcesses()
    const id = runningTask(dbA, dayId)
    const reopened = open(path, { clock: clock.now, process: a.info })
    opened.push(reopened)
    expect(getTask(reopened, id)?.state).toBe('ready')
  })

  it('leaves a live writer and its running tasks alone', () => {
    const { dbA, path, clock, b, dayId } = twoProcesses()
    acquireLock(dbA, 'dld ship')
    const id = runningTask(dbA, dayId)
    const other = open(path, { clock: clock.now, process: b.info })
    opened.push(other)
    expect(getTask(other, id)?.state).toBe('running')
    expect(getLock(other)?.pid).toBe(1001)
  })

  it('recovers when the writer stopped beating, even though its pid looks alive', () => {
    const { dbA, path, clock, b, dayId } = twoProcesses()
    acquireLock(dbA, 'dld ship')
    const id = runningTask(dbA, dayId)
    clock.advance(LOCK_STALE_MS + 5000)
    const other = open(path, { clock: clock.now, process: b.info })
    opened.push(other)
    expect(getTask(other, id)?.state).toBe('ready')
  })

  it('skips recovery when asked', () => {
    const { dbA, path, clock, a, dayId } = twoProcesses()
    const id = runningTask(dbA, dayId)
    const untouched = open(path, { clock: clock.now, process: a.info, recover: false })
    opened.push(untouched)
    expect(getTask(untouched, id)?.state).toBe('running')
  })
})
