import { createHash } from 'node:crypto'
import { readdirSync, readFileSync } from 'node:fs'
import { hostname } from 'node:os'
import { join } from 'node:path'
import { DatabaseSync, type SQLInputValue } from 'node:sqlite'
import { fileURLToPath } from 'node:url'

// §S3. node:sqlite (D3, decided in step 1.2): built into Node 24 and Electron 44, so the CLI, the
// tests and the app share one driver with no native module.

export const BUSY_TIMEOUT_MS = 5000

/** Who this process is, for locks; tests replace it. */
export interface ProcessInfo {
  pid: number
  host: string
  /** True when `pid` is a live process on this host. */
  isAlive(pid: number): boolean
}

export function isPidAlive(pid: number): boolean {
  try {
    process.kill(pid, 0)
    return true
  } catch (err) {
    // EPERM: it exists but belongs to someone else.
    return (err as NodeJS.ErrnoException).code === 'EPERM'
  }
}

export const thisProcess: ProcessInfo = { pid: process.pid, host: hostname(), isAlive: isPidAlive }

export interface ConnectOptions {
  /** The clock for every timestamp written. */
  clock?: () => Date
  process?: ProcessInfo
  readOnly?: boolean
}

export class Db {
  readonly sql: DatabaseSync
  readonly clock: () => Date
  readonly process: ProcessInfo
  private depth = 0

  constructor(
    readonly path: string,
    options: ConnectOptions = {},
  ) {
    this.clock = options.clock ?? (() => new Date())
    this.process = options.process ?? thisProcess
    this.sql = new DatabaseSync(path, {
      readOnly: options.readOnly ?? false,
      enableForeignKeyConstraints: true,
      timeout: BUSY_TIMEOUT_MS,
    })
    this.sql.exec(`PRAGMA busy_timeout = ${BUSY_TIMEOUT_MS}; PRAGMA foreign_keys = ON`)
    if (!options.readOnly) this.sql.exec('PRAGMA journal_mode = WAL')
  }

  /** Now, as UTC ISO-8601. */
  now(): string {
    return this.clock().toISOString()
  }

  one<T>(query: string, ...params: SQLInputValue[]): T | undefined {
    return this.sql.prepare(query).get(...params) as T | undefined
  }

  all<T>(query: string, ...params: SQLInputValue[]): T[] {
    return this.sql.prepare(query).all(...params) as T[]
  }

  run(query: string, ...params: SQLInputValue[]): { changes: number; lastId: number } {
    const result = this.sql.prepare(query).run(...params)
    return { changes: Number(result.changes), lastId: Number(result.lastInsertRowid) }
  }

  /**
   * Runs `fn` in one transaction: BEGIN IMMEDIATE (so writers queue on the busy timeout instead of
   * failing mid-way), or a savepoint when nested. Anything `fn` throws rolls it all back.
   */
  tx<T>(fn: () => T): T {
    const savepoint = this.depth > 0 ? `sp${this.depth}` : null
    this.sql.exec(savepoint ? `SAVEPOINT ${savepoint}` : 'BEGIN IMMEDIATE')
    this.depth++
    let done = false
    try {
      const out = fn()
      if (out instanceof Promise) throw new Error('Db.tx needs a synchronous function')
      this.sql.exec(savepoint ? `RELEASE ${savepoint}` : 'COMMIT')
      done = true
      return out
    } finally {
      this.depth--
      // SQLite may already have rolled back (e.g. a full disk); the original error is what matters.
      if (!done && this.sql.isTransaction) {
        this.sql.exec(savepoint ? `ROLLBACK TO ${savepoint}; RELEASE ${savepoint}` : 'ROLLBACK')
      }
    }
  }

  close(): void {
    if (this.sql.isOpen) this.sql.close()
  }
}

export interface Migration {
  version: number
  name: string
  sql: string
  checksum: string
}

export const MIGRATIONS_DIR = fileURLToPath(new URL('./migrations/', import.meta.url))

/** `NNN_name.sql` files in version order. */
export function loadMigrations(dir: string = MIGRATIONS_DIR): Migration[] {
  const out: Migration[] = []
  for (const file of readdirSync(dir).sort()) {
    const match = /^(\d{3})_([a-z0-9_]+)\.sql$/.exec(file)
    if (!match) continue
    const sql = readFileSync(join(dir, file), 'utf8')
    out.push({
      version: Number(match[1]),
      name: `${match[1]}_${match[2]}`,
      sql,
      checksum: createHash('sha256').update(sql).digest('hex'),
    })
  }
  out.forEach((m, i) => {
    if (m.version !== i + 1) throw new Error(`migration ${m.name}: expected version ${i + 1}`)
  })
  return out
}

export interface AppliedMigration {
  version: number
  name: string
  checksum: string
  applied_at: string
}

function hasTable(db: Db, name: string): boolean {
  return (
    db.one("SELECT 1 AS x FROM sqlite_master WHERE type = 'table' AND name = ?", name) !== undefined
  )
}

export function appliedMigrations(db: Db): AppliedMigration[] {
  if (!hasTable(db, 'schema_migrations')) return []
  return db.all<AppliedMigration>(
    'SELECT version, name, checksum, applied_at FROM schema_migrations ORDER BY version',
  )
}

export interface MigrationStatus {
  current: number
  latest: number
  applied: AppliedMigration[]
  pending: Migration[]
}

/** Compares the database with the migrations this build knows. Throws on a foreign or edited history. */
export function migrationStatus(
  db: Db,
  migrations: Migration[] = loadMigrations(),
): MigrationStatus {
  const applied = appliedMigrations(db)
  for (const row of applied) {
    const known = migrations[row.version - 1]
    if (!known) {
      throw new Error(
        `the database is at schema ${row.version} (${row.name}), newer than this dld (${migrations.length}): update dld`,
      )
    }
    if (known.checksum !== row.checksum) {
      throw new Error(
        `migration ${row.name} changed after it was applied: migrations are append-only`,
      )
    }
  }
  return {
    current: applied.length,
    latest: migrations.length,
    applied,
    pending: migrations.slice(applied.length),
  }
}

/** Applies every pending migration, each in its own transaction. Idempotent. */
export function migrate(db: Db, migrations: Migration[] = loadMigrations()): Migration[] {
  db.sql.exec(`CREATE TABLE IF NOT EXISTS schema_migrations (
    version    INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    checksum   TEXT NOT NULL,
    applied_at TEXT NOT NULL
  )`)
  const { pending } = migrationStatus(db, migrations)
  for (const m of pending) {
    db.tx(() => {
      db.sql.exec(m.sql)
      db.run(
        'INSERT INTO schema_migrations (version, name, checksum, applied_at) VALUES (?, ?, ?, ?)',
        m.version,
        m.name,
        m.checksum,
        db.now(),
      )
    })
  }
  return pending
}
