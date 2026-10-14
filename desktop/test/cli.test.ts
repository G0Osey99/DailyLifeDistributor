import { existsSync, mkdtempSync, readFileSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { type CliDeps, runCli } from '../src/cli/program.js'
import { createTask, dbPath, open, transition, upsertDay } from '../src/engine/db/index.js'
import { KNOWN_SECRETS, MemorySecrets } from '../src/engine/secrets.js'

const EXAMPLE = new URL('../examples/daily-life.series.json', import.meta.url).pathname
const EXAMPLE_TEXT = readFileSync(EXAMPLE, 'utf8')

let dir: string

beforeEach(() => {
  // A fresh data dir (and so a fresh database) per test.
  dir = mkdtempSync(join(tmpdir(), 'dld-cli-'))
  vi.stubEnv('DLD_DATA_DIR', dir)
})

async function dld(argv: string[], deps: CliDeps = {}) {
  let out = ''
  let err = ''
  const code = await runCli(argv, {
    io: { out: (t) => (out += t), err: (t) => (err += t) },
    secrets: () => new MemorySecrets(),
    ...deps,
  })
  return { code, out, err }
}

// biome-ignore lint/suspicious/noExplicitAny: tests edit the raw JSON freely, typos included
function variant(edit: (s: Record<string, any>) => void): string {
  const s = JSON.parse(EXAMPLE_TEXT)
  edit(s)
  const file = join(dir, 'variant.series.json')
  writeFileSync(file, `${JSON.stringify(s, null, 2)}\n`)
  return file
}

describe('dld series', () => {
  it('round-trips the example: import, then export, unchanged', async () => {
    const imported = await dld(['series', 'import', EXAMPLE])
    expect(imported).toMatchObject({ code: 0, err: '' })
    expect(imported.out).toBe(
      'Imported series daily-life ("Daily Life").\n34 values are still TODO: dld series show daily-life lists them.\n',
    )
    const exported = await dld(['series', 'export'])
    expect(exported.code).toBe(0)
    expect(exported.out).toBe(EXAMPLE_TEXT)

    const file = join(dir, 'out.json')
    expect((await dld(['series', 'export', 'daily-life', file])).out).toBe(
      `Exported series daily-life to ${file}.\n`,
    )
    expect(readFileSync(file, 'utf8')).toBe(EXAMPLE_TEXT)
    const again = await dld(['series', 'export', 'daily-life', file])
    expect(again).toMatchObject({
      code: 1,
      err: `dld: ${file} exists: choose another path or add --force\n`,
    })
    expect((await dld(['series', 'export', 'daily-life', file, '--force'])).code).toBe(0)
  })

  it('replaces a stored series on re-import', async () => {
    await dld(['series', 'import', EXAMPLE])
    const renamed = variant((s) => {
      s.name = 'Daily Life (test)'
    })
    expect((await dld(['series', 'import', renamed])).out).toMatch(
      /^Replaced series daily-life \("Daily Life \(test\)"\)/,
    )
  })

  it('fails a misspelled key, naming its path, and stores nothing', async () => {
    const typo = variant((s) => {
      s.destinations.youtube.categoryID = s.destinations.youtube.categoryId
      delete s.destinations.youtube.categoryId
    })
    for (const command of ['check', 'import']) {
      const { code, err } = await dld(['series', command, typo])
      expect(code).toBe(1)
      expect(err).toContain(`dld: ${typo}: 2 problems in the series`)
      expect(err).toContain(
        '  destinations.youtube.categoryID: unknown key (did you mean "categoryId"?)',
      )
      expect(err).toContain('  destinations.youtube.categoryId: missing (expected string)')
    }
    expect(existsSync(dbPath())).toBe(false)
  })

  it('fails check and import when a drafts target is a live one (D5)', async () => {
    const clash = variant((s) => {
      s.drafts['rock-email'] = {
        baseUrl: s.destinations['rock-email'].baseUrl,
        channel: s.destinations['rock-email'].channel,
      }
    })
    const check = await dld(['series', 'check', clash])
    expect(check.code).toBe(1)
    expect(check.err).toContain('drafts targets must differ from live ones (D5)')
    expect(check.err).toContain(
      'drafts.rock-email.channel.guid: is a live channel (destinations.rock-email.channel.guid',
    )
    const imported = await dld(['series', 'import', clash])
    expect(imported.code).toBe(1)
    expect(imported.err).toContain('not imported')
    expect(existsSync(dbPath())).toBe(false)
  })

  it('checks a valid file and lists what is still TODO', async () => {
    const { code, out } = await dld(['series', 'check', EXAMPLE])
    expect(code).toBe(0)
    expect(out).toContain(
      `${EXAMPLE}: series daily-life ("Daily Life") is valid.\n34 values are still TODO:\n  media.folder\n`,
    )
    expect(existsSync(dbPath())).toBe(false)
  })

  it('shows the stored series', async () => {
    await dld(['series', 'import', EXAMPLE])
    const { code, out } = await dld(['series', 'show'])
    expect(code).toBe(0)
    expect(out).toMatch(/^Daily Life \(daily-life\) · default · America\/New_York\n/)
    expect(out).toMatch(/^youtube\s+yes\s+14:00\s+private\s+category 22$/m)
    expect(out).toMatch(/^rock-email\s+yes\s+—\s+TODO\s+rock\.lcbcchurch\.com, channel 24$/m)
    expect(out).toContain('34 values are still TODO:')
    expect((await dld(['series', 'show', 'nope'])).err).toBe(
      'dld: no series "nope"; stored: daily-life\n',
    )
  })

  it('says how to start when nothing is stored', async () => {
    expect((await dld(['series', 'show'])).err).toBe(
      'dld: no series yet: import one with `dld series import <file>`\n',
    )
  })
})

describe('dld db', () => {
  it('migrates once, then has nothing to apply', async () => {
    const first = await dld(['db', 'migrate'])
    expect(first.out).toMatch(/^Applied 001_init\.\nSchema 1 of 1: up to date \(.*dld\.db\)\.\n$/)
    const second = await dld(['db', 'migrate'])
    expect(second.out).toMatch(/^Nothing to apply\. Schema 1 of 1: up to date/)
  })

  it('reports status: schema, rows, task states and the writer', async () => {
    expect((await dld(['db', 'status'])).out).toMatch(
      /^No database yet at .*dld\.db\. Create it with: dld db migrate\n$/,
    )
    await dld(['series', 'import', EXAMPLE])
    const db = open(dbPath())
    const day = upsertDay(db, 'daily-life', '2026-10-20')
    createTask(db, { dayId: day.id, destination: 'youtube', mode: 'drafts' })
    const wistia = createTask(db, { dayId: day.id, destination: 'wistia', mode: 'drafts' })
    transition(db, wistia.id, 'running')
    db.run(
      "INSERT INTO locks (name, pid, host, purpose, acquired_at, heartbeat_at) VALUES ('writer', ?, ?, 'dld ship', ?, ?)",
      process.pid,
      db.process.host,
      db.now(),
      db.now(),
    )
    db.close()
    const { code, out } = await dld(['db', 'status'])
    expect(code).toBe(0)
    expect(out).toMatch(/^database\s+.*dld\.db \(\d+\.\d KB, wal\)$/m)
    expect(out).toMatch(/^schema\s+1 of 1: up to date$/m)
    expect(out).toMatch(/^series\s+Daily Life \(daily-life\) · default$/m)
    expect(out).toMatch(
      /^rows\s+series 1 · days 1 · tasks 2 · events \d+ · quota 0 · photos 0 · settings 1 · locks 1$/m,
    )
    expect(out).toMatch(/^tasks\s+ready 1 · running 1$/m)
    expect(out).toMatch(
      new RegExp(
        `^writer\\s+held by dld ship \\(pid ${process.pid} on .+\\) since .+; heartbeat \\d+ s ago$`,
        'm',
      ),
    )
  })

  it('backs up to a new file and refuses to overwrite', async () => {
    await dld(['series', 'import', EXAMPLE])
    const target = join(dir, 'backup.db')
    const { code, out } = await dld(['db', 'backup', target])
    expect(code).toBe(0)
    expect(out).toMatch(/^Backed up .*dld\.db to .*backup\.db \(\d+ pages, .*\)\.\n$/)
    const copy = open(target, { recover: false })
    expect(copy.one<{ id: string }>('SELECT id FROM series')?.id).toBe('daily-life')
    copy.close()
    expect((await dld(['db', 'backup', target])).err).toBe(
      `dld: ${target} exists: choose a new path\n`,
    )
  })
})

describe('dld secrets', () => {
  it('lists every §C name as present or missing, never a value', async () => {
    const store = new MemorySecrets({
      'wistia.token': 'wistia-token-value',
      'rock.rest_key': 'rock-key-value',
    })
    const { code, out } = await dld(['secrets', 'list'], { secrets: () => store })
    expect(code).toBe(0)
    for (const { name } of KNOWN_SECRETS)
      expect(out).toMatch(new RegExp(`^${name.replace('.', '\\.')}\\s+(present|missing)\\s`, 'm'))
    expect(out).toMatch(/^wistia\.token\s+present\s+Wistia API token \(upload \+ read\)$/m)
    expect(out).toMatch(
      /^youtube\.client\s+missing\s+.*M5: dld secrets set youtube\.client --from-file <client\.json>$/m,
    )
    expect(out).toContain('2 present · 6 missing')
    expect(out).not.toContain('wistia-token-value')
    expect(out).not.toContain('rock-key-value')
  })

  it('sets from a hidden prompt, and from a file', async () => {
    const store = new MemorySecrets()
    const prompts: string[] = []
    const typed = await dld(['secrets', 'set', 'wistia.token'], {
      secrets: () => store,
      readSecret: async (prompt) => {
        prompts.push(prompt)
        return 'typed-token-123'
      },
    })
    expect(typed).toMatchObject({
      code: 0,
      out: 'Stored wistia.token in the keychain (15 bytes).\n',
      err: '',
    })
    expect(prompts).toEqual(['wistia.token (typing is hidden): '])
    expect(store.values.get('wistia.token')).toBe('typed-token-123')

    const file = join(dir, 'client.json')
    writeFileSync(file, '{"installed":{"client_id":"x","client_secret":"y \\"z\\""}}\n')
    const fromFile = await dld(['secrets', 'set', 'youtube.client', '--from-file', file], {
      secrets: () => store,
    })
    expect(fromFile.out).toBe(
      `Stored youtube.client in the keychain (57 bytes).\nThe keychain now holds it: you can delete ${file}.\n`,
    )
    expect(store.values.get('youtube.client')).toBe(
      '{"installed":{"client_id":"x","client_secret":"y \\"z\\""}}',
    )
  })

  it('refuses a non-JSON client file, an empty value and a bad name', async () => {
    const store = new MemorySecrets()
    const file = join(dir, 'client.json')
    writeFileSync(file, 'not json')
    expect(
      (
        await dld(['secrets', 'set', 'youtube.client', '--from-file', file], {
          secrets: () => store,
        })
      ).err,
    ).toBe(
      'dld: youtube.client must be JSON (YouTube Desktop OAuth client (JSON)); nothing stored\n',
    )
    expect(
      (
        await dld(['secrets', 'set', 'wistia.token'], {
          secrets: () => store,
          readSecret: async () => '',
        })
      ).err,
    ).toBe('dld: nothing to store for wistia.token: the value is empty\n')
    expect((await dld(['secrets', 'set', 'Bad Name'], { secrets: () => store })).code).toBe(1)
    expect(store.values.size).toBe(0)
  })

  it('notes a name dld does not read, and deletes', async () => {
    const store = new MemorySecrets()
    const set = await dld(['secrets', 'set', 'dld.selftest'], {
      secrets: () => store,
      readSecret: async () => '{"ok":true}',
    })
    expect(set.err).toBe(
      "note: dld doesn't read dld.selftest; dld secrets list shows the names it uses.\n",
    )
    expect((await dld(['secrets', 'delete', 'dld.selftest'], { secrets: () => store })).out).toBe(
      'Deleted dld.selftest from the keychain.\n',
    )
    expect((await dld(['secrets', 'delete', 'dld.selftest'], { secrets: () => store })).out).toBe(
      'dld.selftest was not in the keychain.\n',
    )
  })
})
