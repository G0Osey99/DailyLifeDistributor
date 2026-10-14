import { parseSeries, type Series } from '../config.js'
import type { Db } from './db.js'
import { recordEvent } from './states.js'

// Small helpers over the §S3 tables. Plain SQL, no ORM.

export interface SeriesRow {
  id: string
  name: string
  created_at: string
  updated_at: string
}

export const DEFAULT_SERIES_SETTING = 'series.default'

/** Stores a validated series (insert or replace its config). The first one becomes the default. */
export function saveSeries(db: Db, series: Series): { created: boolean } {
  return db.tx(() => {
    const now = db.now()
    const existing = db.one<{ id: string }>('SELECT id FROM series WHERE id = ?', series.id)
    const config = JSON.stringify(series)
    if (existing) {
      db.run(
        'UPDATE series SET name = ?, config = ?, updated_at = ? WHERE id = ?',
        series.name,
        config,
        now,
        series.id,
      )
    } else {
      db.run(
        'INSERT INTO series (id, name, config, created_at, updated_at) VALUES (?, ?, ?, ?, ?)',
        series.id,
        series.name,
        config,
        now,
        now,
      )
    }
    if (getSetting(db, DEFAULT_SERIES_SETTING) === undefined)
      setSetting(db, DEFAULT_SERIES_SETTING, series.id)
    recordEvent(db, {
      entity: 'series',
      entityId: series.id,
      from: existing ? 'stored' : null,
      to: 'stored',
      reason: existing ? 'updated' : 'imported',
    })
    return { created: !existing }
  })
}

export function listSeries(db: Db): SeriesRow[] {
  return db.all<SeriesRow>('SELECT id, name, created_at, updated_at FROM series ORDER BY id')
}

/** The stored series, validated again (a schema change since it was stored shows up here). */
export function getSeries(db: Db, id: string): Series | undefined {
  const row = db.one<{ config: string }>('SELECT config FROM series WHERE id = ?', id)
  return row ? parseSeries(JSON.parse(row.config), `stored series ${id}`) : undefined
}

/** `id`, else the default series, else the only one. Throws with what exists when it can't tell. */
export function resolveSeriesId(db: Db, id?: string): string {
  const all = listSeries(db)
  const ids = all.map((s) => s.id)
  if (id) {
    if (ids.includes(id)) return id
    throw new Error(`no series "${id}"${ids.length ? `; stored: ${ids.join(', ')}` : ''}`)
  }
  if (all.length === 0) throw new Error('no series yet: import one with `dld series import <file>`')
  const preferred = getSetting<string>(db, DEFAULT_SERIES_SETTING)
  if (preferred && ids.includes(preferred)) return preferred
  if (all.length === 1) return all[0]?.id as string
  throw new Error(`several series (${ids.join(', ')}): name one`)
}

export interface DayRow {
  id: number
  series_id: string
  date: string
  files: string
  row: string | null
  suggestions: string
  approved_title: string | null
  approved_at: string | null
  approved_source: string | null
  created_at: string
  updated_at: string
}

export function getDay(db: Db, seriesId: string, date: string): DayRow | undefined {
  return db.one<DayRow>('SELECT * FROM days WHERE series_id = ? AND date = ?', seriesId, date)
}

/** Creates the day if needed and sets the fields given (files, sheet row). */
export function upsertDay(
  db: Db,
  seriesId: string,
  date: string,
  patch: { files?: unknown; row?: unknown } = {},
): DayRow {
  return db.tx(() => {
    const now = db.now()
    db.run(
      `INSERT INTO days (series_id, date, created_at, updated_at) VALUES (?, ?, ?, ?)
       ON CONFLICT (series_id, date) DO NOTHING`,
      seriesId,
      date,
      now,
      now,
    )
    if (patch.files !== undefined) {
      db.run(
        'UPDATE days SET files = ?, updated_at = ? WHERE series_id = ? AND date = ?',
        JSON.stringify(patch.files),
        now,
        seriesId,
        date,
      )
    }
    if (patch.row !== undefined) {
      db.run(
        'UPDATE days SET row = ?, updated_at = ? WHERE series_id = ? AND date = ?',
        JSON.stringify(patch.row),
        now,
        seriesId,
        date,
      )
    }
    return getDay(db, seriesId, date) as DayRow
  })
}

export interface EventRow {
  id: number
  entity: string
  entity_id: string
  from_state: string | null
  to_state: string | null
  reason: string | null
  data: string | null
  at: string
}

export function listEvents(
  db: Db,
  filter: { entity?: string; entityId?: string | number } = {},
): EventRow[] {
  const where: string[] = []
  const params: string[] = []
  if (filter.entity) {
    where.push('entity = ?')
    params.push(filter.entity)
  }
  if (filter.entityId !== undefined) {
    where.push('entity_id = ?')
    params.push(String(filter.entityId))
  }
  return db.all<EventRow>(
    `SELECT * FROM events${where.length ? ` WHERE ${where.join(' AND ')}` : ''} ORDER BY id`,
    ...params,
  )
}

export function getSetting<T>(db: Db, key: string): T | undefined {
  const row = db.one<{ value: string }>('SELECT value FROM settings WHERE key = ?', key)
  return row ? (JSON.parse(row.value) as T) : undefined
}

export function setSetting(db: Db, key: string, value: unknown): void {
  db.run(
    `INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)
     ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at`,
    key,
    JSON.stringify(value),
    db.now(),
  )
}

export const STORE_TABLES = [
  'series',
  'days',
  'tasks',
  'events',
  'quota',
  'photos',
  'settings',
  'locks',
] as const

export function tableCounts(db: Db): Record<(typeof STORE_TABLES)[number], number> {
  return Object.fromEntries(
    STORE_TABLES.map((t) => [t, db.one<{ n: number }>(`SELECT count(*) AS n FROM ${t}`)?.n ?? 0]),
  ) as Record<(typeof STORE_TABLES)[number], number>
}

export function taskCountsByState(db: Db): Record<string, number> {
  const rows = db.all<{ state: string; n: number }>(
    'SELECT state, count(*) AS n FROM tasks GROUP BY state',
  )
  return Object.fromEntries(rows.map((r) => [r.state, r.n]))
}
