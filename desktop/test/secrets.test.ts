import { beforeEach, describe, expect, it, vi } from 'vitest'
import { REDACTED, redact } from '../src/engine/log.js'
import {
  KEYCHAIN_SERVICE,
  KeychainError,
  KeychainSecrets,
  KNOWN_SECRETS,
  MemorySecrets,
  parsePasswordLine,
  SECURITY,
} from '../src/engine/secrets.js'
import { FakeKeychain } from './fakes/security.js'

// spawn is mocked: KeychainSecrets runs through its real runner into a fake `security`.
const keychain = vi.hoisted(() => ({
  current: null as null | { spawn: (cmd: string, args: string[]) => unknown },
}))
vi.mock('node:child_process', async (importOriginal) => ({
  ...(await importOriginal<typeof import('node:child_process')>()),
  spawn: vi.fn((cmd: string, args: string[]) => keychain.current?.spawn(cmd, args)),
}))

let fake: FakeKeychain
let secrets: KeychainSecrets

beforeEach(() => {
  fake = new FakeKeychain()
  keychain.current = fake
  secrets = new KeychainSecrets()
})

const CLIENT_JSON = JSON.stringify({
  installed: {
    client_id: 'abc.apps.googleusercontent.com',
    client_secret: 'it\'s "quoted" & spaced',
    redirect_uris: ['http://127.0.0.1'],
  },
})

function hex(value: string): string {
  return Buffer.from(value, 'utf8').toString('hex')
}

describe('KeychainSecrets (§S4)', () => {
  it('stores through `security -i` on stdin, hex-encoded with -X', async () => {
    await secrets.set('wistia.token', 'tok-123 "quoted"')
    expect(fake.calls).toEqual([
      {
        cmd: SECURITY,
        args: ['-i'],
        stdin: `add-generic-password -U -s ${KEYCHAIN_SERVICE} -a wistia.token -X ${hex('tok-123 "quoted"')}\n`,
      },
    ])
  })

  it('deletes through `security -i` on stdin, and says when there was nothing to delete', async () => {
    await secrets.set('wistia.token', 'tok-123')
    expect(await secrets.delete('wistia.token')).toBe(true)
    expect(await secrets.delete('wistia.token')).toBe(false)
    expect(fake.calls.slice(1)).toEqual([
      {
        cmd: SECURITY,
        args: ['-i'],
        stdin: `delete-generic-password -s ${KEYCHAIN_SERVICE} -a wistia.token\n`,
      },
      {
        cmd: SECURITY,
        args: ['-i'],
        stdin: `delete-generic-password -s ${KEYCHAIN_SERVICE} -a wistia.token\n`,
      },
    ])
  })

  it('reads with find-generic-password -g, and checks presence without reading the value', async () => {
    await secrets.set('rock.rest_key', 'rock-key-1')
    expect(await secrets.has('rock.rest_key')).toBe(true)
    expect(await secrets.get('rock.rest_key')).toBe('rock-key-1')
    expect(await secrets.get('pexels.api_key')).toBeNull()
    expect(await secrets.has('pexels.api_key')).toBe(false)
    expect(fake.calls.slice(1).map((c) => c.args)).toEqual([
      ['find-generic-password', '-s', KEYCHAIN_SERVICE, '-a', 'rock.rest_key'],
      ['find-generic-password', '-s', KEYCHAIN_SERVICE, '-a', 'rock.rest_key', '-g'],
      ['find-generic-password', '-s', KEYCHAIN_SERVICE, '-a', 'pexels.api_key', '-g'],
      ['find-generic-password', '-s', KEYCHAIN_SERVICE, '-a', 'pexels.api_key'],
    ])
  })

  it.each([
    ['JSON with quotes', CLIENT_JSON],
    ['a multi-line value', '{\n  "a": "b"\n}\n'],
    ['non-ASCII text', 'café ✓ — «ok»'],
    ['a printable value that looks like hex', 'ABCDEF12'],
    ['a value with a tab', 'a\tb'],
  ])('round-trips %s', async (_, value) => {
    await secrets.set('youtube.client', value)
    expect(await secrets.get('youtube.client')).toBe(value)
  })

  it('never puts a value in argv, in either form', async () => {
    const values = [CLIENT_JSON, 'line1\nline2', 'tok-secret-999']
    for (const [i, value] of values.entries()) {
      const name = KNOWN_SECRETS[i]?.name as string
      await secrets.set(name, value)
      await secrets.get(name)
      await secrets.has(name)
      await secrets.delete(name)
    }
    expect(fake.calls.length).toBe(values.length * 4)
    for (const call of fake.calls) {
      const argv = JSON.stringify(call.args)
      for (const value of values) {
        expect(argv).not.toContain(value)
        expect(argv).not.toContain(hex(value))
      }
      expect(call.args).not.toContain('-X')
      expect(call.args).not.toContain('-w')
    }
  })

  it('registers every value it reads or writes with redact()', async () => {
    fake.items.set(
      `${KEYCHAIN_SERVICE}\u0000anthropic.api_key`,
      Buffer.from('sk-ant-from-keychain'),
    )
    await secrets.get('anthropic.api_key')
    expect(redact('key sk-ant-from-keychain here')).toBe(`key ${REDACTED} here`)
    await secrets.set('wistia.token', 'wistia-written-token')
    expect(redact({ note: 'wistia-written-token' })).toEqual({ note: REDACTED })
  })

  it('refuses a value longer than one `security -i` line, instead of splitting it', async () => {
    // 4095 characters less the command's 71 leave 2012 bytes for youtube.client.
    await expect(secrets.set('youtube.client', 'x'.repeat(2013))).rejects.toThrow(
      'youtube.client is 2013 bytes; the keychain tool takes at most 2012',
    )
    expect(fake.calls).toEqual([])
    const max = 'y'.repeat(2012)
    await secrets.set('youtube.client', max)
    expect(fake.calls[0]?.stdin.length).toBe(4096)
    expect(await secrets.get('youtube.client')).toBe(max)
  })

  it('rejects names that could break the command line, and empty values', async () => {
    for (const name of ['', 'Wistia.Token', 'a b', 'x -a y', 'a"b', 'x\ny', 'a..b']) {
      await expect(secrets.set(name, 'v')).rejects.toThrow(/isn't a secret name/)
    }
    await expect(secrets.set('wistia.token', '')).rejects.toThrow(KeychainError)
    expect(fake.calls).toEqual([])
  })

  it("reports security's error without the password line", async () => {
    const failing = new KeychainSecrets(async () => ({
      code: 51,
      stdout: '',
      stderr:
        'password: "leaked-value"\nsecurity: SecKeychainItemCopyContent: User interaction is not allowed.\n',
    }))
    const err = await failing.get('wistia.token').catch((e: Error) => e)
    expect(String(err)).toContain('User interaction is not allowed')
    expect(String(err)).not.toContain('leaked-value')
  })
})

describe('parsePasswordLine', () => {
  it('reads both forms security prints', () => {
    expect(parsePasswordLine('password: "plain-token"\n')).toBe('plain-token')
    expect(parsePasswordLine('password: "{"a":"b"}"\n')).toBe('{"a":"b"}')
    expect(parsePasswordLine('password: 0x6C696E65310A6C696E6532  "line1\\012line2"\n')).toBe(
      'line1\nline2',
    )
    expect(() => parsePasswordLine('keychain: "x"\n')).toThrow(KeychainError)
  })
})

describe('MemorySecrets', () => {
  it('has the same interface and registers what it reads', async () => {
    const store = new MemorySecrets({ 'vista.api_key': 'vista-key-xyz' })
    expect(await store.has('vista.api_key')).toBe(true)
    expect(await store.get('vista.api_key')).toBe('vista-key-xyz')
    expect(redact('vista-key-xyz')).toBe(REDACTED)
    expect(await store.delete('vista.api_key')).toBe(true)
    expect(await store.get('vista.api_key')).toBeNull()
  })
})
