import { spawn } from 'node:child_process'
import { registerSecret } from './log.js'

// §S4 (D8): secrets live only in the macOS keychain, as generic passwords with service
// "daily-life-distributor" and the secret's name as the account. Nothing else stores them.

export const KEYCHAIN_SERVICE = 'daily-life-distributor'
export const SECURITY = '/usr/bin/security'

export interface Secrets {
  /** The value, or null when it isn't stored. Every value read is registered with redact(). */
  get(name: string): Promise<string | null>
  set(name: string, value: string): Promise<void>
  /** False when there was nothing to delete. */
  delete(name: string): Promise<boolean>
  /** Whether it's stored, without reading the value. */
  has(name: string): Promise<boolean>
}

export interface KnownSecret {
  name: string
  what: string
  /** How to set it (guide Part 4). */
  fix: string
  json?: boolean
}

/** Guide Part 5 §C. */
export const KNOWN_SECRETS: readonly KnownSecret[] = [
  {
    name: 'youtube.client',
    what: 'YouTube Desktop OAuth client (JSON)',
    fix: 'M5: dld secrets set youtube.client --from-file <client.json>',
    json: true,
  },
  {
    name: 'youtube.token',
    what: 'YouTube refresh token',
    fix: 'dld connect youtube (step 1.6, after M5)',
  },
  {
    name: 'wistia.token',
    what: 'Wistia API token (upload + read)',
    fix: 'M4: dld secrets set wistia.token',
  },
  { name: 'vista.api_key', what: 'Vista Social API key', fix: 'M3: dld secrets set vista.api_key' },
  { name: 'rock.rest_key', what: 'Rock REST key', fix: 'M1: dld secrets set rock.rest_key' },
  {
    name: 'anthropic.api_key',
    what: 'Claude API key',
    fix: 'M6: dld secrets set anthropic.api_key',
  },
  {
    name: 'unsplash.access_key',
    what: 'Unsplash access key',
    fix: 'M7: dld secrets set unsplash.access_key',
  },
  { name: 'pexels.api_key', what: 'Pexels API key', fix: 'M7: dld secrets set pexels.api_key' },
]

/** Lower-case words joined by . _ or -: safe as an argument and as a `security -i` token. */
const NAME = /^[a-z0-9]+(?:[._-][a-z0-9]+)*$/
const MAX_NAME = 64

export function checkSecretName(name: string): void {
  if (!NAME.test(name) || name.length > MAX_NAME) {
    throw new Error(
      `"${name}" isn't a secret name: use lower-case words joined by dots, e.g. wistia.token`,
    )
  }
}

/** The longest line `security -i` reads; a longer one is split and its tail run as a command. */
export const SECURITY_MAX_LINE = 4095

export class KeychainError extends Error {
  constructor(message: string) {
    super(message)
    this.name = 'KeychainError'
  }
}

export interface SecurityResult {
  code: number | null
  stdout: string
  stderr: string
}

/** Runs /usr/bin/security with `args`, writing `stdin` to it. Values travel only on stdin. */
export type SecurityRunner = (args: string[], stdin?: string) => Promise<SecurityResult>

export const SECURITY_TIMEOUT_MS = 120_000

export const runSecurity: SecurityRunner = (args, stdin) =>
  new Promise((resolve, reject) => {
    const child = spawn(SECURITY, args, { stdio: ['pipe', 'pipe', 'pipe'] })
    let stdout = ''
    let stderr = ''
    // A keychain prompt waits for a click; give up eventually rather than hang a run.
    const timer = setTimeout(() => child.kill('SIGTERM'), SECURITY_TIMEOUT_MS)
    child.stdout.setEncoding('utf8').on('data', (chunk: string) => {
      stdout += chunk
    })
    child.stderr.setEncoding('utf8').on('data', (chunk: string) => {
      stderr += chunk
    })
    child.on('error', (err) => {
      clearTimeout(timer)
      reject(new KeychainError(`can't run ${SECURITY}: ${err.message}`))
    })
    child.on('close', (code) => {
      clearTimeout(timer)
      resolve({ code, stdout, stderr })
    })
    child.stdin.on('error', () => {})
    child.stdin.end(stdin ?? '')
  })

/** `security` exits 44 (errSecItemNotFound) when there is no such item. */
const NOT_FOUND = 44

/** security's own error lines, never anything else (`-g` prints the password on stderr). */
function securityErrors(result: SecurityResult): string {
  const lines = `${result.stderr}\n${result.stdout}`
    .split('\n')
    .map((l) => l.trim())
    .filter((l) => /^(security|[a-z-]+-password):/.test(l))
  return lines.join('; ') || `exit ${result.code}`
}

/**
 * Reads the value from `security find-generic-password -g`. It prints `password: "text"` when every
 * byte is printable and `password: 0xHEX  "escaped"` otherwise, so the two forms can't be confused
 * (`-w` prints raw text or bare hex, which can).
 */
export function parsePasswordLine(stderr: string): string {
  const line = stderr.split('\n').find((l) => l.startsWith('password: '))
  if (line === undefined) throw new KeychainError('security printed no password line')
  const rest = line.slice('password: '.length)
  const hex = /^0x([0-9A-Fa-f]*)(\s|$)/.exec(rest)
  if (hex) return Buffer.from(hex[1] as string, 'hex').toString('utf8')
  if (rest.length >= 2 && rest.startsWith('"') && rest.endsWith('"')) return rest.slice(1, -1)
  if (rest === '') return ''
  throw new KeychainError('security printed an unexpected password line')
}

export class KeychainSecrets implements Secrets {
  constructor(
    private readonly run: SecurityRunner = runSecurity,
    private readonly service: string = KEYCHAIN_SERVICE,
  ) {}

  async get(name: string): Promise<string | null> {
    checkSecretName(name)
    const result = await this.run(['find-generic-password', '-s', this.service, '-a', name, '-g'])
    if (result.code === NOT_FOUND) return null
    if (result.code !== 0) throw new KeychainError(`can't read ${name}: ${securityErrors(result)}`)
    const value = parsePasswordLine(result.stderr)
    registerSecret(value)
    return value
  }

  async set(name: string, value: string): Promise<void> {
    checkSecretName(name)
    if (value === '') throw new KeychainError(`${name}: the value is empty`)
    // -X takes the value as hex: JSON, quotes and spaces can't break security's tokenizer.
    const hex = Buffer.from(value, 'utf8').toString('hex')
    const line = `add-generic-password -U -s ${this.service} -a ${name} -X ${hex}`
    if (line.length > SECURITY_MAX_LINE) {
      const max = Math.floor((SECURITY_MAX_LINE - (line.length - hex.length)) / 2)
      throw new KeychainError(
        `${name} is ${Buffer.byteLength(value)} bytes; the keychain tool takes at most ${max}`,
      )
    }
    registerSecret(value)
    const result = await this.run(['-i'], `${line}\n`)
    if (result.code !== 0) throw new KeychainError(`can't store ${name}: ${securityErrors(result)}`)
  }

  async delete(name: string): Promise<boolean> {
    checkSecretName(name)
    const result = await this.run(['-i'], `delete-generic-password -s ${this.service} -a ${name}\n`)
    if (result.code === NOT_FOUND) return false
    if (result.code !== 0)
      throw new KeychainError(`can't delete ${name}: ${securityErrors(result)}`)
    return true
  }

  async has(name: string): Promise<boolean> {
    checkSecretName(name)
    // Without -g or -w, security prints only the item's attributes, never the value.
    const result = await this.run(['find-generic-password', '-s', this.service, '-a', name])
    if (result.code === NOT_FOUND) return false
    if (result.code !== 0)
      throw new KeychainError(`can't look up ${name}: ${securityErrors(result)}`)
    return true
  }
}

/** For tests: the same interface over a Map. */
export class MemorySecrets implements Secrets {
  readonly values = new Map<string, string>()

  constructor(initial: Record<string, string> = {}) {
    for (const [name, value] of Object.entries(initial)) this.values.set(name, value)
  }

  async get(name: string): Promise<string | null> {
    checkSecretName(name)
    const value = this.values.get(name) ?? null
    registerSecret(value)
    return value
  }

  async set(name: string, value: string): Promise<void> {
    checkSecretName(name)
    if (value === '') throw new KeychainError(`${name}: the value is empty`)
    registerSecret(value)
    this.values.set(name, value)
  }

  async delete(name: string): Promise<boolean> {
    checkSecretName(name)
    return this.values.delete(name)
  }

  async has(name: string): Promise<boolean> {
    checkSecretName(name)
    return this.values.has(name)
  }
}
