import { existsSync, readFileSync, writeFileSync } from 'node:fs'
import type { Command } from 'commander'
import {
  checkSeries,
  DESTINATIONS,
  type Destination,
  findTodos,
  formatIssues,
  formatSeries,
  type Series,
  TODO,
} from '../engine/config.js'
import {
  getSeries,
  listSeries,
  openStore,
  resolveSeriesId,
  saveSeries,
} from '../engine/db/index.js'
import { table } from './format.js'
import type { CommandContext } from './program.js'

// `dld series import | export | show | check` (§S2).

function readText(file: string): string {
  try {
    return readFileSync(file, 'utf8')
  } catch (err) {
    throw new Error(
      `can't read ${file}: ${(err as NodeJS.ErrnoException).code ?? (err as Error).message}`,
    )
  }
}

function todoLine(todos: string[]): string {
  return todos.length
    ? `${todos.length} values are still ${TODO}:\n${todos.map((t) => `  ${t}`).join('\n')}\n`
    : ''
}

function draftsText(series: Series, d: Destination): string {
  const target = series.drafts[d]
  if (typeof target === 'string') return target
  if ('projectName' in target) return `project "${target.projectName}" (${target.projectId})`
  return new URL(target.baseUrl).host
}

function settingsText(series: Series, d: Destination): string {
  const dest = series.destinations
  switch (d) {
    case 'youtube':
      return `category ${dest.youtube.categoryId}${dest.youtube.madeForKids ? ', made for kids' : ''}`
    case 'wistia':
      return `project "${dest.wistia.projectName}" (${dest.wistia.projectId}), media "${dest.wistia.mediaName}"`
    case 'vista':
      return `profiles ${dest.vista.profiles.map((p) => p.network).join(', ')}`
    case 'spotify':
      return `show ${dest.spotify.showUrl}`
    case 'rock-daily': {
      const c = dest['rock-daily'].channels
      return `${new URL(dest['rock-daily'].baseUrl).host}, channels parent ${c.parent.id} · reflection ${c.reflection.id} · vista ${c.vista.id} · spotlight ${c.spotlight.id}`
    }
    case 'rock-email':
      return `${new URL(dest['rock-email'].baseUrl).host}, channel ${dest['rock-email'].channel.id}`
  }
}

export function describeSeries(
  series: Series,
  meta?: { updated_at: string; isDefault: boolean },
): string {
  const cols = series.sheet.columns as Record<string, string | undefined>
  const lines = [
    `${series.name} (${series.id})${meta?.isDefault ? ' · default' : ''} · ${series.timezone}`,
    '',
    table([
      ['media', `${series.media.folder}`],
      [
        '',
        `videos ${series.media.videoExtensions.join(' ')} · thumbnails ${series.media.thumbnailExtensions.join(' ')}`,
      ],
      ['sheet', `${series.sheet.path} · sheet ${series.sheet.sheetName}`],
      ...Object.entries(cols).map(([k, v]) => ['', `${k} = ${v === TODO ? TODO : `"${v}"`}`]),
      [
        'titles',
        `${series.titles.model}, ${series.titles.count} suggestions${series.titles.endpoint ? `, local ${series.titles.endpoint}` : ''}`,
      ],
    ]),
    '',
    table([
      ['DESTINATION', 'ON', 'TIME (NY)', 'DRAFTS', 'SETTINGS'],
      ...DESTINATIONS.map((d) => [
        d,
        series.destinations[d].enabled ? 'yes' : 'no',
        series.times[d] ?? '—',
        draftsText(series, d),
        settingsText(series, d),
      ]),
    ]),
    '',
    table([
      [
        'waits',
        Object.entries(series.waits)
          .map(([need, w]) => `${need} every ${w.pollS} s, cap ${w.capS} s`)
          .join(' · ') || 'none',
      ],
      [
        'limits',
        `Shorts ≤ ${series.limits.shortsMaxS} s · aspect ${series.limits.aspect.width}:${series.limits.aspect.height} ± ${series.limits.aspect.tolerance * 100}% · breaker ${series.limits.breaker.failureThreshold} failures / ${series.limits.breaker.recoveryS} s`,
      ],
      [
        'quota',
        `YouTube ${series.limits.youtubeQuota.dailyCap} units a day; ${Object.entries(
          series.limits.youtubeQuota.costs,
        )
          .map(([m, c]) => `${m} ${c}`)
          .join(', ')}`,
      ],
      [
        'templates',
        Object.entries(series.templates)
          .map(([d, set]) => `${d}: ${Object.keys(set ?? {}).join(', ')}`)
          .join(' · ') || 'none yet (step 1.5)',
      ],
    ]),
    '',
  ]
  const todos = findTodos(series)
  let out = lines.join('\n')
  out += todos.length ? todoLine(todos) : 'Nothing is TODO.\n'
  if (meta) out += `Stored ${meta.updated_at}.\n`
  return out
}

export function registerSeries(program: Command, ctx: CommandContext): void {
  const series = program
    .command('series')
    .description('the series config (§S2): import, export, show, check')

  series
    .command('import')
    .description(
      'validate a series JSON file and store it (replaces a stored series with the same id)',
    )
    .argument('<file>', 'series JSON, e.g. examples/daily-life.series.json')
    .action((file: string) => {
      const { series: s, errors, todos } = checkSeries(readText(file), file)
      if (errors.length) {
        ctx.io.err(
          `${file}: not imported: drafts targets must differ from live ones (D5)\n${formatIssues(errors)}\n`,
        )
        ctx.setExit(1)
        return
      }
      const db = openStore()
      try {
        const { created } = saveSeries(db, s)
        ctx.io.out(`${created ? 'Imported' : 'Replaced'} series ${s.id} ("${s.name}").\n`)
        if (todos.length)
          ctx.io.out(
            `${todos.length} values are still ${TODO}: dld series show ${s.id} lists them.\n`,
          )
      } finally {
        db.close()
      }
    })

  series
    .command('export')
    .description('print a stored series as JSON, or write it to a file')
    .argument('[id]', 'series id (default: the default series)')
    .argument('[file]', 'write here instead of printing')
    .option('--force', 'overwrite the file if it exists')
    .action((id: string | undefined, file: string | undefined, opts: { force?: boolean }) => {
      const db = openStore()
      try {
        const sid = resolveSeriesId(db, id)
        const text = formatSeries(getSeries(db, sid) as Series)
        if (!file) return ctx.io.out(text)
        if (existsSync(file) && !opts.force)
          throw new Error(`${file} exists: choose another path or add --force`)
        writeFileSync(file, text)
        ctx.io.out(`Exported series ${sid} to ${file}.\n`)
      } finally {
        db.close()
      }
    })

  series
    .command('show')
    .description('summarise a stored series, with every value still TODO')
    .argument('[id]', 'series id (default: the default series)')
    .action((id: string | undefined) => {
      const db = openStore()
      try {
        const sid = resolveSeriesId(db, id)
        const row = listSeries(db).find((r) => r.id === sid)
        const isDefault = resolveSeriesId(db) === sid
        ctx.io.out(
          describeSeries(getSeries(db, sid) as Series, {
            updated_at: row?.updated_at ?? '',
            isDefault,
          }),
        )
      } finally {
        db.close()
      }
    })

  series
    .command('check')
    .description('validate a series JSON file without storing it; exit 1 on any problem')
    .argument('<file>', 'series JSON')
    .action((file: string) => {
      const { series: s, errors, todos } = checkSeries(readText(file), file)
      if (errors.length) {
        ctx.io.err(
          `${file}: drafts targets must differ from live ones (D5)\n${formatIssues(errors)}\n`,
        )
        ctx.setExit(1)
      } else {
        ctx.io.out(`${file}: series ${s.id} ("${s.name}") is valid.\n`)
      }
      ctx.io.out(todoLine(todos))
    })
}
