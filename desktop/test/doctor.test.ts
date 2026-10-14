import { describe, expect, it } from 'vitest'
import { runCli } from '../src/cli/program.js'
import {
  type DoctorSystem,
  defaultProbes,
  doctorExitCode,
  formatTable,
  KEYCHAIN_TOOL,
  MIN_FREE_BYTES,
  type Probe,
  runDoctor,
  SERVICE_HOSTS,
  type SeriesInfo,
} from '../src/engine/doctor.js'
import { KNOWN_SECRETS } from '../src/engine/secrets.js'

const CHROME = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'

const DAILY_LIFE: SeriesInfo = {
  id: 'daily-life',
  name: 'Daily Life',
  hosts: [{ host: 'rock.example.org', service: 'Rock' }],
}

/** A Mac where every check passes; override one field to break one probe. */
function healthyMac(overrides: Partial<DoctorSystem> = {}): DoctorSystem {
  return {
    platform: 'darwin',
    arch: 'arm64',
    nodeVersion: '24.18.0',
    env: { DLD_DATA_DIR: '/tmp/dld-doctor-test' },
    homedir: '/Users/test',
    run: async (cmd) => {
      if (cmd === '/usr/bin/sw_vers') return { code: 0, stdout: '26.6\n' }
      if (cmd === '/usr/bin/plutil') return { code: 0, stdout: '155.0.1\n' }
      if (cmd.endsWith('ffprobe')) return { code: 0, stdout: 'ffprobe version 9.0 Copyright' }
      return null
    },
    isExecutable: async (path) => path === KEYCHAIN_TOOL || path === CHROME,
    findOnPath: async (cmd) => (cmd === 'ffprobe' ? '/opt/homebrew/bin/ffprobe' : null),
    checkWritable: async () => {},
    freeBytes: async () => 2 * MIN_FREE_BYTES,
    reach: async () => ({ ok: true, ms: 42 }),
    hasSecret: async () => true,
    series: async () => [DAILY_LIFE],
    ...overrides,
  }
}

async function doctor(sys: DoctorSystem, probes?: (series: SeriesInfo[]) => Probe[]) {
  let out = ''
  const code = await runCli(['doctor'], {
    io: { out: (text) => (out += text), err: (text) => (out += text) },
    system: () => sys,
    ...(probes ? { probes } : {}),
  })
  return { code, out }
}

function row(rows: Awaited<ReturnType<typeof runDoctor>>, check: string) {
  const found = rows.find((r) => r.check === check)
  if (!found) throw new Error(`no row ${check}`)
  return found
}

describe('dld doctor', () => {
  it("has one row per check, every secret, the series and every host (the series' own last)", async () => {
    const rows = await runDoctor(defaultProbes([DAILY_LIFE]), healthyMac())
    expect(rows.map((r) => r.check)).toEqual([
      'macOS',
      'Node',
      'data dir writable',
      'free space',
      'Google Chrome',
      'ffprobe (optional)',
      'keychain tool',
      ...KNOWN_SECRETS.map(({ name }) => `secret ${name}`),
      'series',
      ...SERVICE_HOSTS.map(({ host }) => `reach ${host}`),
      'reach rock.example.org',
    ])
  })

  it('takes the Rock host from the series, not from code (rule 11)', async () => {
    expect(SERVICE_HOSTS.map((h) => h.host)).not.toContain('rock.lcbcchurch.com')
    const { out } = await doctor(healthyMac())
    expect(out).toMatch(/^ok\s+reach rock\.example\.org\s+Rock: reachable \(42 ms\)$/m)
    const none = await doctor(healthyMac({ series: async () => [] }))
    expect(none.out).not.toContain('reach rock.')
    expect(none.out).toMatch(/^warn\s+series\s+none imported yet\s+dld series import /m)
  })

  it('warns, with its fix, for each missing secret, and never fails on one', async () => {
    const sys = healthyMac({ hasSecret: async (name) => name !== 'wistia.token' })
    const { code, out } = await doctor(sys)
    expect(code).toBe(0)
    expect(out).toMatch(
      /^warn\s+secret wistia\.token\s+Wistia API token \(upload \+ read\): missing\s+M4: dld secrets set wistia\.token$/m,
    )
    expect(out).toMatch(/^ok\s+secret rock\.rest_key\s+Rock REST key: in the keychain$/m)
    const locked = await runDoctor(
      defaultProbes(),
      healthyMac({
        hasSecret: async () => {
          throw new Error('keychain locked')
        },
      }),
    )
    expect(row(locked, 'secret youtube.client')).toMatchObject({ status: 'warn' })
    expect(doctorExitCode(locked)).toBe(0)
  })

  it('reports a database it cannot read on the series row', async () => {
    const sys = healthyMac({
      series: async () => {
        throw new Error('database is from a newer dld')
      },
    })
    const { code, out } = await doctor(sys)
    expect(code).toBe(0)
    expect(out).toMatch(/^warn\s+series\s+can't read the database: database is from a newer dld/m)
  })

  it('exits 0 when every probe is ok', async () => {
    const { code, out } = await doctor(healthyMac())
    expect(code).toBe(0)
    expect(out).toMatch(/^STATUS\s+CHECK\s+DETAIL\s+FIX$/m)
    expect(out).toMatch(/^ok\s+macOS\s+macOS 26\.6 \(arm64\)$/m)
    expect(out).toMatch(/^ok\s+Google Chrome\s+Chrome 155\.0\.1 in \/Applications$/m)
    expect(out).toMatch(
      /^info\s+ffprobe \(optional\)\s+ffprobe 9\.0 at \/opt\/homebrew\/bin\/ffprobe$/m,
    )
    expect(out).toContain('24 ok · 0 warn · 0 fail · 1 info')
  })

  it('fails and exits 1 when /usr/bin/security is missing', async () => {
    const { code, out } = await doctor(healthyMac({ isExecutable: async (p) => p === CHROME }))
    expect(code).toBe(1)
    expect(out).toMatch(/^fail\s+keychain tool\s+\/usr\/bin\/security is missing\s+.*keychain/m)
    expect(out).toContain('1 fail')
  })

  it('fails on anything but macOS', async () => {
    const rows = await runDoctor(defaultProbes(), healthyMac({ platform: 'linux' }))
    expect(row(rows, 'macOS')).toMatchObject({ status: 'fail', detail: 'this is linux, not macOS' })
    expect(doctorExitCode(rows)).toBe(1)
  })

  it('fails on an old Node and warns on a non-LTS one', async () => {
    const old = await runDoctor(defaultProbes(), healthyMac({ nodeVersion: '22.12.0' }))
    expect(row(old, 'Node').status).toBe('fail')
    const odd = await runDoctor(defaultProbes(), healthyMac({ nodeVersion: '25.1.0' }))
    expect(row(odd, 'Node').status).toBe('warn')
  })

  it('fails when the data dir is not writable', async () => {
    const sys = healthyMac({
      checkWritable: async () => {
        throw new Error('EACCES: permission denied')
      },
    })
    const rows = await runDoctor(defaultProbes(), sys)
    expect(row(rows, 'data dir writable')).toMatchObject({
      status: 'fail',
      detail: '/tmp/dld-doctor-test: EACCES: permission denied',
    })
  })

  it('only warns for low disk space, missing Chrome and unreachable hosts, so it exits 0', async () => {
    const sys = healthyMac({
      freeBytes: async () => MIN_FREE_BYTES - 1,
      isExecutable: async (p) => p === KEYCHAIN_TOOL,
      reach: async (host) =>
        host === 'api.pexels.com' ? { ok: false, error: 'ENOTFOUND' } : { ok: true, ms: 1 },
    })
    const rows = await runDoctor(defaultProbes(), sys)
    expect(row(rows, 'free space').status).toBe('warn')
    expect(row(rows, 'Google Chrome')).toMatchObject({ status: 'warn', detail: 'not installed' })
    expect(row(rows, 'reach api.pexels.com')).toMatchObject({
      status: 'warn',
      detail: 'Pexels: ENOTFOUND',
    })
    expect(row(rows, 'reach api.wistia.com').status).toBe('ok')
    expect(doctorExitCode(rows)).toBe(0)
  })

  it('reports a missing ffprobe as info, never a problem', async () => {
    const rows = await runDoctor(defaultProbes(), healthyMac({ findOnPath: async () => null }))
    expect(row(rows, 'ffprobe (optional)')).toMatchObject({ status: 'info' })
    expect(doctorExitCode(rows)).toBe(0)
  })

  it('turns a probe that throws into a fail row', async () => {
    const broken: Probe = {
      check: 'broken',
      run: async () => {
        throw new Error('boom')
      },
    }
    const { code, out } = await doctor(healthyMac(), () => [broken])
    expect(code).toBe(1)
    expect(out).toMatch(/^fail\s+broken\s+probe crashed: boom/m)
  })

  it('gives every non-ok row a one-line fix and hides fixes on ok rows', () => {
    const table = formatTable([
      { check: 'a', status: 'ok', detail: 'fine', fix: 'never shown' },
      { check: 'b', status: 'warn', detail: 'meh', fix: 'do this' },
    ])
    expect(table).not.toContain('never shown')
    expect(table).toMatch(/^warn\s+b\s+meh\s+do this$/m)
  })
})

describe('dld --version', () => {
  it('prints the package version and exits 0', async () => {
    let out = ''
    const code = await runCli(['--version'], { io: { out: (t) => (out += t), err: () => {} } })
    expect(code).toBe(0)
    expect(out.trim()).toMatch(/^\d+\.\d+\.\d+$/)
  })

  it('exits non-zero on an unknown command', async () => {
    let err = ''
    const code = await runCli(['nope'], { io: { out: () => {}, err: (t) => (err += t) } })
    expect(code).toBe(1)
    expect(err).toContain('nope')
  })
})
