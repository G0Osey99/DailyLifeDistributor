import { existsSync, statSync } from 'node:fs'
import { backup } from 'node:sqlite'
import type { Command } from 'commander'
import {
  dbPath,
  getLock,
  listSeries,
  migrate,
  migrationStatus,
  open,
  recoverOrphans,
  resolveSeriesId,
  STORE_TABLES,
  tableCounts,
  taskCountsByState,
} from '../engine/db/index.js'
import { ensureDataDir } from '../engine/paths.js'
import { bytes, table, tilde } from './format.js'
import type { CommandContext } from './program.js'

// `dld db migrate | status | backup` (§S3).

function ago(fromIso: string, now: Date): string {
  const s = Math.max(0, Math.round((now.getTime() - Date.parse(fromIso)) / 1000))
  return s < 120 ? `${s} s ago` : `${Math.round(s / 60)} min ago`
}

export function registerDb(program: Command, ctx: CommandContext): void {
  const db = program.command('db').description('the local database (§S3): migrate, status, backup')

  db.command('migrate')
    .description('create the database or apply pending migrations')
    .action(() => {
      ensureDataDir()
      const path = dbPath()
      const store = open(path, { recover: false })
      try {
        const applied = migrate(store)
        const { recovered } = recoverOrphans(store)
        const { current, latest } = migrationStatus(store)
        for (const m of applied) ctx.io.out(`Applied ${m.name}.\n`)
        ctx.io.out(
          `${applied.length ? '' : 'Nothing to apply. '}Schema ${current} of ${latest}: up to date (${tilde(path)}).\n`,
        )
        if (recovered.length) ctx.io.out(`${recovered.length} interrupted tasks are ready again.\n`)
      } finally {
        store.close()
      }
    })

  db.command('status')
    .description('show the schema version, row counts, task states and the writer lock')
    .action(() => {
      const path = dbPath()
      if (!existsSync(path)) {
        ctx.io.out(`No database yet at ${tilde(path)}. Create it with: dld db migrate\n`)
        return
      }
      const store = open(path)
      try {
        const status = migrationStatus(store)
        const journal =
          store.one<{ journal_mode: string }>('PRAGMA journal_mode')?.journal_mode ?? '?'
        const rows: string[][] = [
          ['database', `${tilde(path)} (${bytes(statSync(path).size)}, ${journal})`],
          [
            'schema',
            `${status.current} of ${status.latest}${status.pending.length ? `; pending: ${status.pending.map((m) => m.name).join(', ')} (dld db migrate)` : ': up to date'}`,
          ],
        ]
        if (status.current >= 1) {
          const counts = tableCounts(store)
          const series = listSeries(store)
          const preferred = series.length ? resolveSeriesId(store) : undefined
          rows.push([
            'series',
            series
              .map((s) => `${s.name} (${s.id})${s.id === preferred ? ' · default' : ''}`)
              .join(', ') || 'none (dld series import <file>)',
          ])
          rows.push(['rows', STORE_TABLES.map((t) => `${t} ${counts[t]}`).join(' · ')])
          const states = Object.entries(taskCountsByState(store))
          rows.push(['tasks', states.map(([state, n]) => `${state} ${n}`).join(' · ') || 'none'])
          const lock = getLock(store)
          rows.push([
            'writer',
            lock
              ? `held by ${lock.purpose ?? 'dld'} (pid ${lock.pid} on ${lock.host}) since ${lock.acquired_at}; heartbeat ${ago(lock.heartbeat_at, store.clock())}`
              : 'free',
          ])
        }
        ctx.io.out(`${table(rows)}\n`)
      } finally {
        store.close()
      }
    })

  db.command('backup')
    .description('copy the database to a new file (a consistent snapshot, safe while dld runs)')
    .argument('<path>', 'where to write the copy; must not exist')
    .action(async (target: string) => {
      const path = dbPath()
      if (!existsSync(path))
        throw new Error(`no database at ${tilde(path)} yet: nothing to back up`)
      if (existsSync(target)) throw new Error(`${target} exists: choose a new path`)
      const store = open(path, { recover: false })
      try {
        const pages = await backup(store.sql, target)
        ctx.io.out(
          `Backed up ${tilde(path)} to ${target} (${pages} pages, ${bytes(statSync(target).size)}).\n`,
        )
      } finally {
        store.close()
      }
    })
}
