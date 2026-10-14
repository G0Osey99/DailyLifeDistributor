import { mkdtempSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { afterEach, describe, expect, it } from 'vitest'
import {
  BUSY_TIMEOUT_MS,
  createTask,
  type Db,
  getSeries,
  getSetting,
  listEvents,
  loadMigrations,
  MIGRATIONS_DIR,
  migrate,
  migrationStatus,
  open,
  resolveSeriesId,
  saveSeries,
  upsertDay,
} from '../src/engine/db/index.js'
import { exampleSeries, fakeClock, seededStore, tempDbPath } from './fakes/store.js'

const opened: Db[] = []
function track(db: Db): Db {
  opened.push(db)
  return db
}
afterEach(() => {
  for (const db of opened.splice(0)) db.close()
})

describe('open (§S3)', () => {
  it('uses WAL, foreign keys and a 5000 ms busy timeout', () => {
    const db = track(open(tempDbPath()))
    expect(db.one<{ journal_mode: string }>('PRAGMA journal_mode')?.journal_mode).toBe('wal')
    expect(db.one<{ foreign_keys: number }>('PRAGMA foreign_keys')?.foreign_keys).toBe(1)
    expect(db.one<{ timeout: number }>('PRAGMA busy_timeout')?.timeout).toBe(BUSY_TIMEOUT_MS)
  })

  it('enforces foreign keys', () => {
    const { db } = seededStore()
    track(db)
    expect(() => upsertDay(db, 'no-such-series', '2026-10-20')).toThrow(/FOREIGN KEY/)
  })

  it('writes UTC ISO-8601 timestamps from its clock', () => {
    const clock = fakeClock('2026-10-14T12:34:56.789Z')
    const { db } = seededStore({ clock: clock.now })
    track(db)
    const day = upsertDay(db, 'daily-life', '2026-10-21')
    expect(day.created_at).toBe('2026-10-14T12:34:56.789Z')
  })
})

describe('migrate', () => {
  it('creates every table and is idempotent', () => {
    const path = tempDbPath()
    const db = track(open(path))
    expect(migrationStatus(db)).toMatchObject({ current: 0, latest: 1 })
    expect(migrate(db).map((m) => m.name)).toEqual(['001_init'])
    expect(migrate(db)).toEqual([])
    db.close()
    const again = track(open(path, { migrate: true }))
    expect(migrate(again)).toEqual([])
    expect(migrationStatus(again)).toMatchObject({ current: 1, latest: 1, pending: [] })
    const tables = again
      .all<{ name: string }>("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")
      .map((t) => t.name)
    expect(tables).toEqual([
      'days',
      'events',
      'locks',
      'photos',
      'quota',
      'schema_migrations',
      'series',
      'settings',
      'tasks',
    ])
  })

  it('indexes tasks by state and keeps one day per series and date, one task per day and destination', () => {
    const { db, dayId } = seededStore()
    track(db)
    const index = db.one<{ sql: string }>(
      "SELECT sql FROM sqlite_master WHERE name = 'tasks_state'",
    )
    expect(index?.sql).toMatch(/ON tasks \(state\)/)
    expect(() =>
      db.run(
        "INSERT INTO days (series_id, date, created_at, updated_at) VALUES ('daily-life', '2026-10-20', 'x', 'x')",
      ),
    ).toThrow(/UNIQUE constraint failed: days.series_id, days.date/)
    createTask(db, { dayId, destination: 'youtube', mode: 'drafts' })
    expect(() => createTask(db, { dayId, destination: 'youtube', mode: 'live' })).toThrow(
      /UNIQUE constraint failed: tasks.day_id, tasks.destination/,
    )
  })

  it('refuses a migration edited after it was applied, and a database newer than this dld', () => {
    const db = track(open(tempDbPath(), { migrate: true }))
    const migrations = loadMigrations()
    const edited = migrations.map((m) => ({ ...m, checksum: 'edited' }))
    expect(() => migrationStatus(db, edited)).toThrow(/001_init changed after it was applied/)
    db.run("INSERT INTO schema_migrations VALUES (2, '002_future', 'x', 'x')")
    expect(() => migrationStatus(db, migrations)).toThrow(
      /schema 2 \(002_future\), newer than this dld \(1\)/,
    )
  })

  it('loads NNN_name.sql files in order and rejects a gap', () => {
    expect(loadMigrations(MIGRATIONS_DIR).map((m) => m.version)).toEqual([1])
    const dir = mkdtempSync(join(tmpdir(), 'dld-mig-'))
    writeFileSync(join(dir, '001_a.sql'), 'SELECT 1;')
    writeFileSync(join(dir, '003_c.sql'), 'SELECT 1;')
    writeFileSync(join(dir, 'notes.txt'), 'ignored')
    expect(() => loadMigrations(dir)).toThrow(/003_c: expected version 2/)
  })
})

describe('repo helpers', () => {
  it('stores a series, makes the first the default, and replaces it on re-import', () => {
    const db = track(open(tempDbPath(), { migrate: true }))
    const series = exampleSeries()
    expect(saveSeries(db, series)).toEqual({ created: true })
    expect(getSetting(db, 'series.default')).toBe('daily-life')
    expect(saveSeries(db, { ...series, name: 'Daily Life (renamed)' })).toEqual({ created: false })
    expect(getSeries(db, 'daily-life')?.name).toBe('Daily Life (renamed)')
    expect(resolveSeriesId(db)).toBe('daily-life')
    expect(() => resolveSeriesId(db, 'nope')).toThrow('no series "nope"; stored: daily-life')
    expect(listEvents(db, { entity: 'series' }).map((e) => e.reason)).toEqual([
      'imported',
      'updated',
    ])
  })

  it('upserts a day and its JSON fields', () => {
    const { db } = seededStore()
    track(db)
    const day = upsertDay(db, 'daily-life', '2026-10-22', {
      files: { video: '/v.mp4' },
      row: { passage: 'Ps 1' },
    })
    const again = upsertDay(db, 'daily-life', '2026-10-22')
    expect(again.id).toBe(day.id)
    expect(JSON.parse(again.files)).toEqual({ video: '/v.mp4' })
    expect(JSON.parse(again.row ?? 'null')).toEqual({ passage: 'Ps 1' })
  })

  it('rolls back everything a failing transaction wrote, including nested savepoints', () => {
    const { db } = seededStore()
    track(db)
    expect(() =>
      db.tx(() => {
        upsertDay(db, 'daily-life', '2026-10-23')
        throw new Error('boom')
      }),
    ).toThrow('boom')
    expect(db.one('SELECT id FROM days WHERE date = ?', '2026-10-23')).toBeUndefined()
    db.tx(() => {
      upsertDay(db, 'daily-life', '2026-10-24')
      expect(() =>
        db.tx(() => {
          upsertDay(db, 'daily-life', '2026-10-25')
          throw new Error('inner')
        }),
      ).toThrow('inner')
    })
    expect(db.one('SELECT id FROM days WHERE date = ?', '2026-10-24')).toBeDefined()
    expect(db.one('SELECT id FROM days WHERE date = ?', '2026-10-25')).toBeUndefined()
  })
})
