import { execFile } from 'node:child_process'
import { constants } from 'node:fs'
import { access, mkdir, rm, statfs, writeFile } from 'node:fs/promises'
import { homedir } from 'node:os'
import { delimiter, dirname, join } from 'node:path'
import { dataDir } from './paths.js'
import { MIN_NODE_MAJOR } from './version.js'

// `dld doctor`: one small probe per row. Later steps append probes to defaultProbes().

export type Status = 'ok' | 'warn' | 'fail' | 'info'

export interface ProbeResult {
  status: Status
  detail: string
  /** One line telling the person what to do. Shown for every row that isn't ok. */
  fix?: string
}

export interface Row extends ProbeResult {
  check: string
}

export interface Probe {
  check: string
  run(sys: DoctorSystem): Promise<ProbeResult>
}

/** Everything the probes touch, so tests can replace it. */
export interface DoctorSystem {
  platform: NodeJS.Platform
  arch: string
  nodeVersion: string
  env: NodeJS.ProcessEnv
  homedir: string
  /** Runs a command; null when it can't be started. */
  run(cmd: string, args: string[]): Promise<{ code: number; stdout: string } | null>
  isExecutable(path: string): Promise<boolean>
  findOnPath(cmd: string): Promise<string | null>
  /** Creates `dir` if needed, writes and removes a file in it; throws why it can't. */
  checkWritable(dir: string): Promise<void>
  freeBytes(dir: string): Promise<number>
  reach(
    host: string,
    timeoutMs: number,
  ): Promise<{ ok: true; ms: number } | { ok: false; error: string }>
}

export const MIN_FREE_BYTES = 10 * 1024 ** 3
export const REACH_TIMEOUT_MS = 5000
export const KEYCHAIN_TOOL = '/usr/bin/security'

/** Hosts from guide Part 5 §B. Rock's host moves to the series config in step 1.2. */
export const SERVICE_HOSTS: ReadonlyArray<{ host: string; service: string }> = [
  { host: 'www.googleapis.com', service: 'YouTube' },
  { host: 'upload.wistia.com', service: 'Wistia upload' },
  { host: 'api.wistia.com', service: 'Wistia' },
  { host: 'rock.lcbcchurch.com', service: 'Rock' },
  { host: 'api.vistasocial.com', service: 'Vista Social' },
  { host: 'creators.spotify.com', service: 'Spotify for Creators' },
  { host: 'api.anthropic.com', service: 'Claude' },
  { host: 'api.unsplash.com', service: 'Unsplash' },
  { host: 'api.pexels.com', service: 'Pexels' },
]

const GB = 1024 ** 3

function tilde(path: string, home: string): string {
  return path === home || path.startsWith(`${home}/`) ? `~${path.slice(home.length)}` : path
}

export const macosProbe: Probe = {
  check: 'macOS',
  async run(sys) {
    if (sys.platform !== 'darwin') {
      return {
        status: 'fail',
        detail: `this is ${sys.platform}, not macOS`,
        fix: 'dld runs on macOS only (D2): use the Mac that ships Daily Life',
      }
    }
    const out = await sys.run('/usr/bin/sw_vers', ['-productVersion'])
    const version = out?.code === 0 ? out.stdout.trim() : ''
    return { status: 'ok', detail: `macOS ${version || '(version unknown)'} (${sys.arch})` }
  },
}

export const nodeProbe: Probe = {
  check: 'Node',
  async run(sys) {
    const major = Number(sys.nodeVersion.replace(/^v/, '').split('.')[0])
    const fix = `Install Node ${MIN_NODE_MAJOR} LTS: brew install node@${MIN_NODE_MAJOR} (M9)`
    if (!(major >= MIN_NODE_MAJOR)) {
      return {
        status: 'fail',
        detail: `Node ${sys.nodeVersion}; dld needs ${MIN_NODE_MAJOR} or newer`,
        fix,
      }
    }
    if (major % 2 === 1) {
      return { status: 'warn', detail: `Node ${sys.nodeVersion} is not an LTS release`, fix }
    }
    return { status: 'ok', detail: `Node ${sys.nodeVersion}` }
  },
}

export const dataDirProbe: Probe = {
  check: 'data dir writable',
  async run(sys) {
    const dir = dataDir(sys.env)
    try {
      await sys.checkWritable(dir)
      return { status: 'ok', detail: tilde(dir, sys.homedir) }
    } catch (err) {
      return {
        status: 'fail',
        detail: `${tilde(dir, sys.homedir)}: ${(err as Error).message}`,
        fix: "Fix the folder's permissions, or set DLD_DATA_DIR to a writable folder",
      }
    }
  },
}

export const freeSpaceProbe: Probe = {
  check: 'free space',
  async run(sys) {
    const fix = `Free up disk space: keep at least ${MIN_FREE_BYTES / GB} GB for a week of videos`
    try {
      const free = await sys.freeBytes(dataDir(sys.env))
      const detail = `${(free / GB).toFixed(1)} GB free`
      return free < MIN_FREE_BYTES ? { status: 'warn', detail, fix } : { status: 'ok', detail }
    } catch (err) {
      return { status: 'warn', detail: `can't read free space: ${(err as Error).message}`, fix }
    }
  },
}

export const chromeProbe: Probe = {
  check: 'Google Chrome',
  async run(sys) {
    for (const apps of ['/Applications', join(sys.homedir, 'Applications')]) {
      const app = join(apps, 'Google Chrome.app')
      if (!(await sys.isExecutable(join(app, 'Contents/MacOS/Google Chrome')))) continue
      const out = await sys.run('/usr/bin/plutil', [
        '-extract',
        'CFBundleShortVersionString',
        'raw',
        '-o',
        '-',
        join(app, 'Contents/Info.plist'),
      ])
      const version = out?.code === 0 ? ` ${out.stdout.trim()}` : ''
      return { status: 'ok', detail: `Chrome${version} in ${tilde(apps, sys.homedir)}` }
    }
    return {
      status: 'warn',
      detail: 'not installed',
      fix: 'Install Google Chrome from google.com/chrome (M9); browser logins need it from step 2.4',
    }
  },
}

export const ffprobeProbe: Probe = {
  check: 'ffprobe (optional)',
  async run(sys) {
    const path = await sys.findOnPath('ffprobe')
    if (!path) {
      return {
        status: 'info',
        detail: 'not installed; mp4box.js reads .mp4 and .mov without it (D3)',
        fix: 'Optional, for other video containers: brew install ffmpeg',
      }
    }
    const out = await sys.run(path, ['-version'])
    const version = /ffprobe version (\S+)/.exec(out?.stdout ?? '')?.[1]
    return {
      status: 'info',
      detail: `${version ? `ffprobe ${version} at ` : ''}${tilde(path, sys.homedir)}`,
    }
  },
}

export const keychainProbe: Probe = {
  check: 'keychain tool',
  async run(sys) {
    if (await sys.isExecutable(KEYCHAIN_TOOL)) return { status: 'ok', detail: KEYCHAIN_TOOL }
    return {
      status: 'fail',
      detail: `${KEYCHAIN_TOOL} is missing`,
      fix: 'dld keeps secrets in the macOS keychain through /usr/bin/security (D8): run it on macOS',
    }
  },
}

export function hostProbe(host: string, service: string): Probe {
  return {
    check: `reach ${host}`,
    async run(sys) {
      const result = await sys.reach(host, REACH_TIMEOUT_MS)
      if (result.ok) return { status: 'ok', detail: `${service}: reachable (${result.ms} ms)` }
      return {
        status: 'warn',
        detail: `${service}: ${result.error}`,
        fix: `Check the network, VPN or firewall for ${host}`,
      }
    },
  }
}

export function defaultProbes(): Probe[] {
  return [
    macosProbe,
    nodeProbe,
    dataDirProbe,
    freeSpaceProbe,
    chromeProbe,
    ffprobeProbe,
    keychainProbe,
    ...SERVICE_HOSTS.map(({ host, service }) => hostProbe(host, service)),
  ]
}

/** Runs every probe concurrently; rows keep the probes' order. A probe that throws is a fail. */
export async function runDoctor(probes: Probe[], sys: DoctorSystem): Promise<Row[]> {
  return Promise.all(
    probes.map(async (probe): Promise<Row> => {
      try {
        return { check: probe.check, ...(await probe.run(sys)) }
      } catch (err) {
        return {
          check: probe.check,
          status: 'fail',
          detail: `probe crashed: ${(err as Error).message}`,
          fix: 'This is a dld bug: report it with this output',
        }
      }
    }),
  )
}

export function doctorExitCode(rows: Row[]): 0 | 1 {
  return rows.some((row) => row.status === 'fail') ? 1 : 0
}

export function formatTable(rows: Row[]): string {
  const header: Row = { status: 'STATUS' as Status, check: 'CHECK', detail: 'DETAIL', fix: 'FIX' }
  const all = [header, ...rows]
  const width = (pick: (row: Row) => string) => Math.max(...all.map((row) => pick(row).length))
  const ws = width((r) => r.status)
  const wc = width((r) => r.check)
  const wd = width((r) => r.detail)
  const lines = all.map((row) => {
    const fix = row === header || row.status !== 'ok' ? (row.fix ?? '') : ''
    return `${row.status.padEnd(ws)}  ${row.check.padEnd(wc)}  ${row.detail.padEnd(wd)}  ${fix}`.trimEnd()
  })
  const count = (status: Status) => rows.filter((row) => row.status === status).length
  const summary = `${count('ok')} ok · ${count('warn')} warn · ${count('fail')} fail · ${count('info')} info`
  return `${lines.join('\n')}\n\n${summary}\n`
}

async function isExecutable(path: string): Promise<boolean> {
  try {
    await access(path, constants.X_OK)
    return true
  } catch {
    return false
  }
}

/** The real Mac. */
export function realSystem(): DoctorSystem {
  return {
    platform: process.platform,
    arch: process.arch,
    nodeVersion: process.versions.node,
    env: process.env,
    homedir: homedir(),
    run: (cmd, args) =>
      new Promise((resolve) => {
        execFile(cmd, args, { timeout: 10_000 }, (err, stdout) => {
          if (err && typeof err.code !== 'number') return resolve(null)
          resolve({ code: err ? Number(err.code) : 0, stdout: String(stdout) })
        })
      }),
    isExecutable,
    async findOnPath(cmd) {
      for (const dir of (process.env.PATH ?? '').split(delimiter).filter(Boolean)) {
        if (await isExecutable(join(dir, cmd))) return join(dir, cmd)
      }
      return null
    },
    async checkWritable(dir) {
      await mkdir(dir, { recursive: true, mode: 0o700 })
      const probe = join(dir, `.dld-doctor-${process.pid}`)
      await writeFile(probe, 'ok')
      await rm(probe)
    },
    async freeBytes(dir) {
      // The data dir may not exist yet: measure the nearest folder that does.
      let at = dir
      for (;;) {
        try {
          const stats = await statfs(at)
          return stats.bavail * stats.bsize
        } catch (err) {
          const parent = dirname(at)
          if ((err as NodeJS.ErrnoException).code !== 'ENOENT' || parent === at) throw err
          at = parent
        }
      }
    },
    async reach(host, timeoutMs) {
      const started = performance.now()
      try {
        // Any HTTP answer, even 4xx, proves DNS, TCP and TLS work.
        const res = await fetch(`https://${host}/`, {
          method: 'HEAD',
          redirect: 'manual',
          signal: AbortSignal.timeout(timeoutMs),
        })
        await res.body?.cancel()
        return { ok: true, ms: Math.round(performance.now() - started) }
      } catch (err) {
        const e = err as Error & { cause?: { code?: string; message?: string } }
        if (e.name === 'TimeoutError') return { ok: false, error: `no answer in ${timeoutMs} ms` }
        return { ok: false, error: e.cause?.code ?? e.cause?.message ?? e.message }
      }
    },
  }
}
