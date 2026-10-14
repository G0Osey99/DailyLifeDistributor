import { mkdirSync } from 'node:fs'
import { homedir } from 'node:os'
import { join, relative, resolve, sep } from 'node:path'

export const APP_NAME = 'Daily Life Distributor'

/** Folders inside the data dir (§S1). Each is created the first time it is asked for. */
export type DataSubdir = 'logs' | 'profiles' | 'evidence' | 'cache' | 'credits'

/**
 * The data dir: `DLD_DATA_DIR` if set, else ~/Library/Application Support/Daily Life Distributor.
 * Pure: nothing is created. `ensureDataDir` and `dataPath` create what they return.
 */
export function dataDir(env: NodeJS.ProcessEnv = process.env): string {
  const override = env.DLD_DATA_DIR?.trim()
  if (override) return resolve(override)
  return join(homedir(), 'Library', 'Application Support', APP_NAME)
}

/** Creates the data dir (owner-only: it will hold browser logins and logs) and returns it. */
export function ensureDataDir(env: NodeJS.ProcessEnv = process.env): string {
  const dir = dataDir(env)
  mkdirSync(dir, { recursive: true, mode: 0o700 })
  return dir
}

/** A folder inside the data dir, e.g. `dataPath('profiles', 'spotify')`, created on demand. */
export function dataPath(sub: DataSubdir, ...parts: string[]): string {
  const base = join(ensureDataDir(), sub)
  const dir = join(base, ...parts)
  const rel = relative(base, dir)
  if (rel === '..' || rel.startsWith(`..${sep}`)) {
    throw new Error(`${dir} is outside ${base}`)
  }
  mkdirSync(dir, { recursive: true, mode: 0o700 })
  return dir
}
