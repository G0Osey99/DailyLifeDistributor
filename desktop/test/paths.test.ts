import { existsSync, mkdtempSync, statSync } from 'node:fs'
import { homedir, tmpdir } from 'node:os'
import { join } from 'node:path'
import { describe, expect, it, vi } from 'vitest'
import { dataDir, dataPath, ensureDataDir } from '../src/engine/paths.js'

describe('dataDir', () => {
  it('honours DLD_DATA_DIR', () => {
    expect(dataDir({ DLD_DATA_DIR: '/tmp/dld-elsewhere' })).toBe('/tmp/dld-elsewhere')
  })

  it('resolves a relative DLD_DATA_DIR against the working directory', () => {
    expect(dataDir({ DLD_DATA_DIR: 'rel/dir' })).toBe(join(process.cwd(), 'rel/dir'))
  })

  it('defaults to Application Support when DLD_DATA_DIR is unset or blank', () => {
    const expected = join(homedir(), 'Library', 'Application Support', 'Daily Life Distributor')
    expect(dataDir({})).toBe(expected)
    expect(dataDir({ DLD_DATA_DIR: '  ' })).toBe(expected)
  })

  it('reads process.env by default and creates nothing', () => {
    const dir = join(mkdtempSync(join(tmpdir(), 'dld-paths-')), 'not-yet')
    vi.stubEnv('DLD_DATA_DIR', dir)
    expect(dataDir()).toBe(dir)
    expect(existsSync(dir)).toBe(false)
  })
})

describe('ensureDataDir and dataPath', () => {
  it('create the data dir and its subdirs on demand, owner-only', () => {
    const dir = join(mkdtempSync(join(tmpdir(), 'dld-paths-')), 'data')
    vi.stubEnv('DLD_DATA_DIR', dir)
    expect(ensureDataDir()).toBe(dir)
    expect(statSync(dir).mode & 0o777).toBe(0o700)

    const profile = dataPath('profiles', 'spotify')
    expect(profile).toBe(join(dir, 'profiles', 'spotify'))
    expect(statSync(profile).isDirectory()).toBe(true)
    expect(dataPath('logs')).toBe(join(dir, 'logs'))
  })

  it('refuses a path that escapes its subdir', () => {
    expect(() => dataPath('evidence', '..', 'logs')).toThrow(/outside/)
    expect(() => dataPath('evidence', '../../x')).toThrow(/outside/)
  })
})
