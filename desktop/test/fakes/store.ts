import { mkdtempSync, readFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { parseSeriesJson, type Series } from '../../src/engine/config.js'
import {
  type Db,
  type OpenOptions,
  open,
  type ProcessInfo,
  saveSeries,
  upsertDay,
} from '../../src/engine/db/index.js'

/** A clock tests move by hand. */
export function fakeClock(start = '2026-10-14T12:00:00.000Z') {
  let now = Date.parse(start)
  return {
    now: () => new Date(now),
    advance(ms: number) {
      now += ms
    },
  }
}

/** This "process" and which pids count as alive. */
export function fakeProcess(pid = 1001, host = 'test-mac') {
  const alive = new Set([pid])
  const info: ProcessInfo = { pid, host, isAlive: (p) => alive.has(p) }
  return { info, alive }
}

export function tempDbPath(): string {
  return join(mkdtempSync(join(tmpdir(), 'dld-db-')), 'dld.db')
}

export function exampleSeries(): Series {
  return parseSeriesJson(
    readFileSync(new URL('../../examples/daily-life.series.json', import.meta.url), 'utf8'),
  )
}

/** A migrated store in a temp dir, with the example series and one day. */
export function seededStore(
  options: OpenOptions = {},
  path = tempDbPath(),
): { db: Db; path: string; dayId: number } {
  const db = open(path, { migrate: true, ...options })
  saveSeries(db, exampleSeries())
  const day = upsertDay(db, 'daily-life', '2026-10-20')
  return { db, path, dayId: day.id }
}
