import { mkdtempSync, readFileSync, statSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'
import { createRunLogger, newRunId, REDACTED, redact, registerSecret } from '../src/engine/log.js'

// Every value below is a made-up placeholder.
const KEY = 'placeholder-key-value-123'

describe('redact', () => {
  it('removes registered secret values from nested objects, arrays and strings', () => {
    registerSecret(KEY)
    const input = {
      run: { steps: [{ note: `posted with ${KEY} today` }, KEY], count: 3 },
      ok: true,
      none: null,
    }
    expect(redact(input)).toEqual({
      run: { steps: [{ note: `posted with ${REDACTED} today` }, REDACTED], count: 3 },
      ok: true,
      none: null,
    })
  })

  it('removes the URL-encoded form of a registered secret', () => {
    registerSecret('a b/c+d')
    expect(redact('https://x.test/?q=a%20b%2Fc%2Bd')).toBe(`https://x.test/?q=${REDACTED}`)
  })

  it('ignores secrets too short to redact safely', () => {
    registerSecret('ab')
    registerSecret('')
    registerSecret(undefined)
    expect(redact('abc')).toBe('abc')
  })

  it('hides Authorization, x-api-key and Authorization-Token headers in any case', () => {
    const headers = {
      Authorization: 'Bearer placeholder',
      'x-api-key': 'placeholder',
      'X-API-KEY': 'placeholder',
      'Authorization-Token': 'placeholder',
      authorization_token: 'placeholder',
      'Content-Type': 'application/json',
    }
    expect(redact({ request: { headers } })).toEqual({
      request: {
        headers: {
          Authorization: REDACTED,
          'x-api-key': REDACTED,
          'X-API-KEY': REDACTED,
          'Authorization-Token': REDACTED,
          authorization_token: REDACTED,
          'Content-Type': 'application/json',
        },
      },
    })
  })

  it('hides secret headers in a fetch Headers object and in a Map', () => {
    const headers = new Headers({ authorization: 'Bearer placeholder', accept: 'text/plain' })
    expect(redact(headers)).toEqual({ authorization: REDACTED, accept: 'text/plain' })
    const map = new Map([
      ['Authorization-Token', 'placeholder'],
      ['x', 'y'],
    ])
    expect(redact(map)).toEqual({ 'Authorization-Token': REDACTED, x: 'y' })
  })

  it('hides OAuth fields: access, refresh and id tokens, client secrets', () => {
    expect(
      redact({
        access_token: 'placeholder',
        refresh_token: 'placeholder',
        idToken: 'placeholder',
        client_secret: 'placeholder',
        expires_in: 3599,
        token_type: 'Bearer',
      }),
    ).toEqual({
      access_token: REDACTED,
      refresh_token: REDACTED,
      idToken: REDACTED,
      client_secret: REDACTED,
      expires_in: 3599,
      token_type: 'Bearer',
    })
  })

  it('hides header lines and bearer tokens inside strings', () => {
    expect(redact('GET /v1\nAuthorization: Bearer placeholder.value\nAccept: */*')).toBe(
      `GET /v1\nAuthorization: ${REDACTED}\nAccept: */*`,
    )
    expect(redact('x-api-key=placeholder')).toBe(`x-api-key=${REDACTED}`)
    expect(redact('Authorization-Token: placeholder')).toBe(`Authorization-Token: ${REDACTED}`)
    expect(redact('upload failed for Bearer placeholder.value-1 (401)')).toBe(
      `upload failed for Bearer ${REDACTED} (401)`,
    )
  })

  it('hides Google OAuth tokens, token query parameters and form bodies inside strings', () => {
    expect(redact('token ya29.placeholder-token expired')).toBe(`token ${REDACTED} expired`)
    expect(redact('refresh with 1//placeholder-refresh')).toBe(`refresh with ${REDACTED}`)
    expect(redact('https://x.test/cb?code=placeholder&state=s1')).toBe(
      `https://x.test/cb?code=${REDACTED}&state=s1`,
    )
    expect(redact('grant_type=refresh_token&refresh_token=placeholder&client_secret=p2')).toBe(
      `grant_type=refresh_token&refresh_token=${REDACTED}&client_secret=${REDACTED}`,
    )
  })

  it('hides secret fields inside JSON text', () => {
    const body = JSON.stringify({ access_token: 'placeholder', scope: 'youtube' })
    expect(JSON.parse(redact(body) as string)).toEqual({ access_token: REDACTED, scope: 'youtube' })
  })

  it('keeps errors readable and redacts their message, stack and cause', () => {
    registerSecret(KEY)
    const err = new Error(`rejected ${KEY}`, { cause: { headers: { Authorization: KEY } } })
    const out = redact(err) as Record<string, unknown>
    expect(out.name).toBe('Error')
    expect(out.message).toBe(`rejected ${REDACTED}`)
    expect(String(out.stack)).not.toContain(KEY)
    expect(out.cause).toEqual({ headers: { Authorization: REDACTED } })
  })

  it('handles cycles, URLs, dates, bigints and binary data', () => {
    const loop: Record<string, unknown> = { name: 'loop' }
    loop.self = loop
    const shared = { a: 1 }
    expect(
      redact({
        loop,
        twice: [shared, shared],
        url: new URL('https://x.test/?access_token=placeholder'),
        at: new Date('2026-10-14T12:00:00Z'),
        big: 10n,
        bytes: new Uint8Array(4),
      }),
    ).toEqual({
      loop: { name: 'loop', self: '[Circular]' },
      twice: [{ a: 1 }, { a: 1 }],
      url: `https://x.test/?access_token=${REDACTED}`,
      at: '2026-10-14T12:00:00.000Z',
      big: '10',
      bytes: '[4 bytes]',
    })
  })

  it('leaves ordinary text alone', () => {
    const text = 'Daily Life October 14, 2026: authorization failed, retrying in 5 s'
    expect(redact(text)).toBe(text)
  })
})

describe('createRunLogger', () => {
  it('writes one redacted JSON object per line to <dir>/<runId>.jsonl', () => {
    registerSecret(KEY)
    const dir = mkdtempSync(join(tmpdir(), 'dld-log-'))
    const now = () => new Date('2026-10-14T15:30:12.345Z')
    const log = createRunLogger({ dir, runId: 'run-1', now })
    log.info('upload started', { headers: { 'x-api-key': KEY }, file: 'app 261014.mp4' })
    log.error(`failed with ${KEY}`)
    log.close()

    expect(log.file).toBe(join(dir, 'run-1.jsonl'))
    expect(statSync(log.file).mode & 0o777).toBe(0o600)
    const text = readFileSync(log.file, 'utf8')
    expect(text).not.toContain(KEY)
    expect(
      text
        .trimEnd()
        .split('\n')
        .map((line) => JSON.parse(line)),
    ).toEqual([
      {
        ts: '2026-10-14T15:30:12.345Z',
        level: 'info',
        run: 'run-1',
        msg: 'upload started',
        data: { headers: { 'x-api-key': REDACTED }, file: 'app 261014.mp4' },
      },
      {
        ts: '2026-10-14T15:30:12.345Z',
        level: 'error',
        run: 'run-1',
        msg: `failed with ${REDACTED}`,
      },
    ])
  })

  it('defaults to the logs folder in the data dir and refuses writes after close', () => {
    const log = createRunLogger()
    expect(log.file).toBe(join(process.env.DLD_DATA_DIR ?? '', 'logs', `${log.runId}.jsonl`))
    log.close()
    expect(() => log.warn('late')).toThrow(/closed/)
  })

  it('makes sortable run ids', () => {
    expect(newRunId(new Date('2026-10-14T15:30:12.345Z'))).toMatch(/^20261014T153012Z-[0-9a-f]{6}$/)
  })
})
