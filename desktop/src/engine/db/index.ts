import { join } from 'node:path'
import { dataDir, ensureDataDir } from '../paths.js'
import { type ConnectOptions, Db, migrate, migrationStatus } from './db.js'
import { recoverOrphans } from './locks.js'

export * from './db.js'
export * from './locks.js'
export * from './repo.js'
export * from './states.js'

export const DB_FILE = 'dld.db'

/** `<data dir>/dld.db`. Pure: nothing is created. */
export function dbPath(env: NodeJS.ProcessEnv = process.env): string {
  return join(dataDir(env), DB_FILE)
}

export interface OpenOptions extends ConnectOptions {
  /** Apply pending migrations first. */
  migrate?: boolean
  /** Crash recovery at open (§S3); on unless false. Skipped read-only or before the first migration. */
  recover?: boolean
}

/**
 * Opens the store (§S3): WAL, foreign keys, a 5000 ms busy timeout; migrates when asked; then, on a
 * migrated database, breaks a dead writer's lock and brings its running tasks back to ready.
 */
export function open(path: string, options: OpenOptions = {}): Db {
  const db = new Db(path, options)
  try {
    if (options.migrate) migrate(db)
    if (options.recover !== false && !options.readOnly && migrationStatus(db).current >= 1) {
      recoverOrphans(db)
    }
    return db
  } catch (err) {
    db.close()
    throw err
  }
}

/** The engine's store in the data dir, migrated and recovered: what every command uses. */
export function openStore(options: Omit<OpenOptions, 'migrate'> = {}): Db {
  ensureDataDir()
  return open(dbPath(), { ...options, migrate: true })
}
