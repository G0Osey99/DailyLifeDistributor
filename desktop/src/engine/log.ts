import { randomBytes } from 'node:crypto'
import { closeSync, openSync, writeSync } from 'node:fs'
import { join } from 'node:path'
import { dataPath } from './paths.js'

// §S5. Everything that reaches a log, the terminal or the UI goes through redact() first.

export const REDACTED = '[REDACTED]'

/** Shorter values are not registered: replacing them everywhere would shred the log. */
const MIN_SECRET_LENGTH = 4

const secrets = new Set<string>()

/** Registers a secret value (§S4 registers every value it reads) so redact() removes it. */
export function registerSecret(value: string | null | undefined): void {
  if (!value || value.length < MIN_SECRET_LENGTH) return
  secrets.add(value)
  const encoded = encodeURIComponent(value)
  if (encoded !== value) secrets.add(encoded)
}

/** Forgets every registered secret. For tests. */
export function forgetSecrets(): void {
  secrets.clear()
}

// Header and field names whose values are always secret, compared lower-case without - and _.
const SECRET_KEYS = new Set([
  'authorization',
  'authorizationtoken',
  'xapikey',
  'apikey',
  'cookie',
  'setcookie',
  'accesstoken',
  'refreshtoken',
  'idtoken',
  'token',
  'clientsecret',
  'secret',
  'password',
])

function isSecretKey(key: string): boolean {
  return SECRET_KEYS.has(key.toLowerCase().replace(/[-_]/g, ''))
}

const HEADER_NAMES = `authorization(?:-token)?|x-api-key|cookie|set-cookie`
const FIELD_NAMES = `access_token|refresh_token|id_token|client_secret|api_?key|password|token|${HEADER_NAMES}`

const STRING_RULES: Array<[RegExp, string]> = [
  // "authorization": "…" inside JSON text
  [new RegExp(String.raw`("(?:${FIELD_NAMES})"\s*:\s*)"(?:[^"\\]|\\.)*"`, 'gi'), `$1"${REDACTED}"`],
  // Authorization: … / x-api-key=… header lines, to the end of the line
  [new RegExp(String.raw`\b(${HEADER_NAMES})(\s*[:=]\s*)[^\r\n]+`, 'gi'), `$1$2${REDACTED}`],
  // Bearer / Basic credentials anywhere
  [/\b(Bearer|Basic)\s+[\w\-.~+/]+=*/gi, `$1 ${REDACTED}`],
  // Google OAuth access and refresh tokens
  [/\bya29\.[\w-]+/g, REDACTED],
  [/(^|[^\w/])1\/\/[\w-]{8,}/g, `$1${REDACTED}`],
  // ?access_token=… and form bodies (refresh_token=…&client_secret=…)
  [
    /(^|[?&#\s])((?:access_token|refresh_token|id_token|client_secret|api_?key|key|token|code)=)[^&#\s"']+/gim,
    `$1$2${REDACTED}`,
  ],
]

function redactString(text: string): string {
  let out = text
  // Longest first, so a secret that contains another is removed whole.
  for (const secret of [...secrets].sort((a, b) => b.length - a.length)) {
    out = out.replaceAll(secret, REDACTED)
  }
  for (const [pattern, replacement] of STRING_RULES) out = out.replace(pattern, replacement)
  return out
}

/**
 * A JSON-safe copy of `value` with secrets removed: registered secret values, secret headers and
 * fields (Authorization, x-api-key, Authorization-Token, cookies, OAuth tokens) at any depth, and
 * the same inside strings. Headers and Maps become plain objects, binary data becomes its size.
 */
export function redact(value: unknown): unknown {
  return redactValue(value, new WeakSet())
}

function redactValue(value: unknown, seen: WeakSet<object>): unknown {
  if (typeof value === 'string') return redactString(value)
  if (typeof value === 'bigint') return value.toString()
  if (typeof value === 'symbol') return value.toString()
  if (typeof value === 'function') return undefined
  if (value === null || typeof value !== 'object') return value
  if (value instanceof Date) return value.toISOString()
  if (value instanceof URL) return redactString(value.href)
  if (ArrayBuffer.isView(value) || value instanceof ArrayBuffer) {
    return `[${value.byteLength} bytes]`
  }
  if (seen.has(value)) return '[Circular]'
  seen.add(value)
  try {
    if (value instanceof Headers || value instanceof Map) {
      return redactEntries(
        [...value.entries()].map(([k, v]) => [String(k), v]),
        seen,
      )
    }
    if (Array.isArray(value)) return value.map((item) => redactValue(item, seen))
    if (value instanceof Error) {
      const out: Record<string, unknown> = {
        name: value.name,
        message: redactString(value.message),
      }
      if (value.stack) out.stack = redactString(value.stack)
      if (value.cause !== undefined) out.cause = redactValue(value.cause, seen)
      return { ...out, ...redactEntries(Object.entries(value), seen) }
    }
    return redactEntries(Object.entries(value), seen)
  } finally {
    seen.delete(value)
  }
}

function redactEntries(
  entries: Array<[string, unknown]>,
  seen: WeakSet<object>,
): Record<string, unknown> {
  const out: Record<string, unknown> = {}
  for (const [key, item] of entries) {
    const hide = isSecretKey(key) && item !== null && item !== undefined && item !== ''
    out[key] = hide ? REDACTED : redactValue(item, seen)
  }
  return out
}

export type Level = 'debug' | 'info' | 'warn' | 'error'

export interface RunLogger {
  readonly runId: string
  readonly file: string
  log(level: Level, msg: string, data?: Record<string, unknown>): void
  debug(msg: string, data?: Record<string, unknown>): void
  info(msg: string, data?: Record<string, unknown>): void
  warn(msg: string, data?: Record<string, unknown>): void
  error(msg: string, data?: Record<string, unknown>): void
  close(): void
}

export interface RunLoggerOptions {
  /** Folder for the log file; default `<data dir>/logs`. */
  dir?: string
  runId?: string
  now?: () => Date
}

/** `20261014T153012Z-1a2b3c`: sortable by start time, unique within a second. */
export function newRunId(now: Date = new Date()): string {
  const stamp = now
    .toISOString()
    .replace(/[-:]/g, '')
    .replace(/\.\d+Z$/, 'Z')
  return `${stamp}-${randomBytes(3).toString('hex')}`
}

/**
 * One JSON-lines log per run (§S5): `<dir>/<runId>.jsonl`, one
 * `{"ts","level","run","msg","data"}` object per line, every line redacted.
 * Lines are written synchronously so a crash never loses the lines before it.
 */
export function createRunLogger(options: RunLoggerOptions = {}): RunLogger {
  const now = options.now ?? (() => new Date())
  const runId = options.runId ?? newRunId(now())
  const file = join(options.dir ?? dataPath('logs'), `${runId}.jsonl`)
  let fd: number | null = openSync(file, 'a', 0o600)

  const log = (level: Level, msg: string, data?: Record<string, unknown>): void => {
    if (fd === null) throw new Error(`run log ${runId} is closed`)
    const line: Record<string, unknown> = { ts: now().toISOString(), level, run: runId, msg }
    if (data !== undefined) line.data = data
    writeSync(fd, `${JSON.stringify(redact(line))}\n`)
  }

  return {
    runId,
    file,
    log,
    debug: (msg, data) => log('debug', msg, data),
    info: (msg, data) => log('info', msg, data),
    warn: (msg, data) => log('warn', msg, data),
    error: (msg, data) => log('error', msg, data),
    close() {
      if (fd !== null) closeSync(fd)
      fd = null
    },
  }
}
