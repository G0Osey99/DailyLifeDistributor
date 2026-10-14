import { z } from 'zod'

// §S2: one series = everything specific to a church and a show (rule 11). Strict: an unknown key
// fails and names its path. Key order here is the order `dld series export` writes.

/** A value nobody knows yet. Valid in a series; `dld series check` lists every one. */
export const TODO = 'TODO'

export const DESTINATIONS = [
  'youtube',
  'wistia',
  'vista',
  'spotify',
  'rock-daily',
  'rock-email',
] as const
export type Destination = (typeof DESTINATIONS)[number]

function todo<T extends z.ZodType>(schema: T, what: string) {
  return z.union([z.literal(TODO), schema], { error: `expected ${what}, or "${TODO}"` })
}

const TimeZone = z.string().refine(
  (tz) => {
    try {
      new Intl.DateTimeFormat('en-US', { timeZone: tz })
      return true
    } catch {
      return false
    }
  },
  { error: 'expected an IANA time zone such as "America/New_York"' },
)
const Path = z.string().regex(/^(\/|~\/)./, { error: 'expected an absolute path (/… or ~/…)' })
const Extension = z
  .string()
  .regex(/^\.[a-z0-9]+$/, { error: 'expected a lower-case extension such as ".mp4"' })
const HHMM = z
  .string()
  .regex(/^([01]\d|2[0-3]):[0-5]\d$/, { error: 'expected a 24-hour time "HH:MM"' })
const HttpsUrl = z.url({ protocol: /^https$/, error: 'expected an https:// URL' })
const Guid = z.string().regex(/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/, {
  error: 'expected a lower-case GUID',
})
const Header = z.string().min(1, { error: 'expected a column header' })
const Enabled = z.boolean()

const SheetColumns = z.strictObject({
  date_column: todo(Header, 'a column header'),
  youtube_title_column: todo(Header, 'a column header').optional(),
  shorts_title_column: todo(Header, 'a column header'),
  podcast_title_column: todo(Header, 'a column header').optional(),
  episode_title_column: todo(Header, 'a column header'),
  description_column: todo(Header, 'a column header'),
  vista_caption_column: todo(Header, 'a column header'),
  tags_column: todo(Header, 'a column header'),
  passage_column: todo(Header, 'a column header'),
  scripture_column: todo(Header, 'a column header'),
  prayer_column: todo(Header, 'a column header'),
  topic_column: todo(Header, 'a column header').optional(),
  transcript_column: todo(Header, 'a column header'),
})

/** Template text per destination; the keys and the {{field}} syntax are step 1.5's (§S9). */
const TemplateSet = z.record(
  z.string().regex(/^[a-z][A-Za-z0-9]*$/, { error: 'expected a camelCase template name' }),
  z.string(),
)
const perDestination = <T extends z.ZodType>(schema: T) =>
  z.strictObject(
    Object.fromEntries(DESTINATIONS.map((d) => [d, schema.optional()])) as {
      [K in Destination]: z.ZodOptional<T>
    },
  )

const RockChannel = z.strictObject({
  guid: Guid,
  id: todo(z.int().positive(), 'a channel id'),
})
const AttributeKey = todo(
  z.string().regex(/^[A-Za-z][A-Za-z0-9_]*$/, { error: 'expected a Rock attribute key' }),
  'a Rock attribute key',
)
const HashedId = z.string().regex(/^[a-z0-9]{6,20}$/i, { error: 'expected a Wistia hashed id' })
const RockDailyChannels = z.strictObject({
  parent: RockChannel,
  reflection: RockChannel,
  vista: RockChannel,
  spotlight: RockChannel,
})
const RockEmailAttributes = z.strictObject({
  emailMessage: AttributeKey,
  smsMessage: AttributeKey,
  youtubeLink: AttributeKey,
  thumbnail: AttributeKey,
  sent: AttributeKey,
})

const Destinations = z.strictObject({
  youtube: z.strictObject({
    enabled: Enabled,
    categoryId: z.string().regex(/^\d+$/, { error: 'expected a numeric category id' }),
    madeForKids: z.boolean(),
  }),
  wistia: z.strictObject({
    enabled: Enabled,
    /** The Wistia project; Rock's media picker shows it as a folder. */
    projectName: z.string().min(1),
    projectId: todo(HashedId, 'a hashed project id'),
    /** Media name; YYMMDD is replaced by the day's date ("app 261014"). */
    mediaName: z.string().includes('YYMMDD', { error: 'expected a name containing YYMMDD' }),
  }),
  vista: z.strictObject({
    enabled: Enabled,
    profiles: z
      .array(
        z.strictObject({
          network: z.string().regex(/^[a-z]+$/, { error: 'expected a network such as "facebook"' }),
          id: z.string().min(1).optional(),
        }),
      )
      .min(1),
  }),
  spotify: z.strictObject({
    enabled: Enabled,
    showUrl: todo(HttpsUrl, 'the show URL'),
  }),
  'rock-daily': z.strictObject({
    enabled: Enabled,
    baseUrl: HttpsUrl,
    channels: RockDailyChannels,
    settings: z.strictObject({
      enablePrayer: z.boolean(),
      prayerCount: z.int().nonnegative(),
      vistaHasLikes: z.boolean(),
      spotlightOrientation: z.string().min(1),
      spotlightMediaAccount: z.string().min(1),
    }),
    attributeKeys: z.strictObject({
      parent: z.strictObject({ enablePrayer: AttributeKey, prayerCount: AttributeKey }),
      reflection: z.strictObject({
        publicTitle: AttributeKey,
        promptTitle: AttributeKey,
        prompt: AttributeKey,
      }),
      vista: z.strictObject({
        tagline: AttributeKey,
        backgroundImage: AttributeKey,
        hasLikesEnabled: AttributeKey,
      }),
      spotlight: z.strictObject({
        tagline: AttributeKey,
        videoOrientation: AttributeKey,
        mediaElement: AttributeKey,
      }),
    }),
  }),
  'rock-email': z.strictObject({
    enabled: Enabled,
    baseUrl: HttpsUrl,
    channel: RockChannel,
    attributeKeys: RockEmailAttributes,
  }),
})

const Wait = z.strictObject({ pollS: z.int().positive(), capS: z.int().positive() })

// D5: where a drafts-mode run writes, per destination. "plan" = no safe target: the destination
// runs in plan form during a drafts run.
const PLAN = z.literal('plan')
const Drafts = z.strictObject({
  youtube: z.union([z.literal('private'), PLAN], { error: 'expected "private" or "plan"' }),
  wistia: todo(
    z.union([
      PLAN,
      z.strictObject({
        projectName: z.string().min(1),
        projectId: todo(HashedId, 'a hashed project id'),
      }),
    ]),
    '"plan" or {projectName, projectId}',
  ),
  vista: todo(z.union([z.literal('draft'), PLAN]), '"draft" or "plan"'),
  spotify: z.union([z.literal('draft'), PLAN], { error: 'expected "draft" or "plan"' }),
  'rock-daily': todo(
    z.union([PLAN, z.strictObject({ baseUrl: HttpsUrl, channels: RockDailyChannels })]),
    '"plan" or {baseUrl, channels}',
  ),
  'rock-email': todo(
    z.union([PLAN, z.strictObject({ baseUrl: HttpsUrl, channel: RockChannel })]),
    '"plan" or {baseUrl, channel}',
  ),
})

export const SeriesSchema = z.strictObject({
  id: z.string().regex(/^[a-z0-9][a-z0-9-]{0,62}$/, { error: 'expected a lower-case slug' }),
  name: z.string().min(1),
  timezone: TimeZone,
  media: z.strictObject({
    folder: todo(Path, 'an absolute path'),
    videoExtensions: z.array(Extension).min(1),
    thumbnailExtensions: z.array(Extension).min(1),
  }),
  sheet: z.strictObject({
    path: todo(Path.regex(/\.xlsx$/i, { error: 'expected an .xlsx file' }), 'an .xlsx path'),
    sheetName: todo(z.string().min(1), 'a sheet name'),
    columns: SheetColumns,
  }),
  titles: z.strictObject({
    model: z.string().min(1),
    count: z.int().min(1).max(10),
    endpoint: z.url().optional(),
  }),
  templates: perDestination(TemplateSet),
  times: perDestination(todo(HHMM, 'a 24-hour time "HH:MM"')),
  destinations: Destinations,
  waits: z.record(
    z
      .string()
      .regex(/^[a-z]+(\.[A-Za-z]+)+$/, { error: 'expected an artifact such as "youtube.url"' }),
    Wait,
  ),
  limits: z.strictObject({
    shortsMaxS: z.int().positive(),
    aspect: z.strictObject({
      width: z.int().positive(),
      height: z.int().positive(),
      tolerance: z.number().min(0).max(0.5),
    }),
    youtubeQuota: z.strictObject({
      dailyCap: z.int().positive(),
      costs: z.record(
        z
          .string()
          .regex(/^[a-zA-Z]+\.[a-zA-Z]+$/, { error: 'expected an API method "videos.insert"' }),
        z.int().nonnegative(),
      ),
    }),
    breaker: z.strictObject({
      failureThreshold: z.int().positive(),
      recoveryS: z.int().positive(),
    }),
  }),
  drafts: Drafts,
})

export type Series = z.infer<typeof SeriesSchema>

/** A config problem, named by its path ("destinations.youtube.categoryId"). */
export interface ConfigIssue {
  path: string
  message: string
}

export class SeriesError extends Error {
  constructor(
    readonly issues: ConfigIssue[],
    readonly source?: string,
  ) {
    super(
      `${source ? `${source}: ` : ''}${issues.length === 1 ? 'a problem' : `${issues.length} problems`} in the series\n${formatIssues(issues)}`,
    )
    this.name = 'SeriesError'
  }
}

export function formatIssues(issues: ConfigIssue[]): string {
  return issues.map((issue) => `  ${issue.path || '(root)'}: ${issue.message}`).join('\n')
}

export function formatPath(path: ReadonlyArray<PropertyKey>): string {
  let out = ''
  for (const part of path) {
    if (typeof part === 'number') out += `[${part}]`
    else out += out ? `.${String(part)}` : String(part)
  }
  return out
}

/** The object schema inside optionals and unions (the "TODO" | "plan" | {…} shapes), if any. */
function objectIn(schema: z.ZodType | undefined): z.ZodObject | undefined {
  if (schema instanceof z.ZodObject) return schema
  if (schema instanceof z.ZodOptional) return objectIn(schema.unwrap() as z.ZodType)
  if (schema instanceof z.ZodUnion) {
    for (const option of schema.options as z.ZodType[]) {
      const found = objectIn(option)
      if (found) return found
    }
  }
  return undefined
}

/** The schema's known keys at `path`, for "did you mean" on an unknown key. */
function knownKeys(path: ReadonlyArray<PropertyKey>): string[] {
  let schema: z.ZodType | undefined = SeriesSchema
  for (const part of path) {
    let inner: z.ZodType | undefined = schema
    while (inner instanceof z.ZodOptional) inner = inner.unwrap() as z.ZodType
    if (inner instanceof z.ZodArray) schema = inner.element as z.ZodType
    else if (inner instanceof z.ZodRecord) schema = inner.valueType as z.ZodType
    else schema = (objectIn(inner)?.shape as Record<string, z.ZodType> | undefined)?.[String(part)]
    if (!schema) return []
  }
  return Object.keys(objectIn(schema)?.shape ?? {})
}

function distance(a: string, b: string): number {
  const row = Array.from({ length: b.length + 1 }, (_, i) => i)
  for (let i = 1; i <= a.length; i++) {
    let prev = row[0] as number
    row[0] = i
    for (let j = 1; j <= b.length; j++) {
      const cur = row[j] as number
      row[j] = Math.min(
        cur + 1,
        (row[j - 1] as number) + 1,
        prev + (a[i - 1]?.toLowerCase() === b[j - 1]?.toLowerCase() ? 0 : 1),
      )
      prev = cur
    }
  }
  return row[b.length] as number
}

function suggest(key: string, candidates: string[]): string | undefined {
  let best: string | undefined
  let bestDistance = Math.max(2, Math.floor(key.length / 3)) + 1
  for (const candidate of candidates) {
    const d = distance(key, candidate)
    if (d < bestDistance) {
      best = candidate
      bestDistance = d
    }
  }
  return best
}

function toIssues(error: z.ZodError): ConfigIssue[] {
  const issues: ConfigIssue[] = []
  for (const issue of error.issues) {
    if (issue.code === 'unrecognized_keys') {
      for (const key of issue.keys) {
        const hint = suggest(key, knownKeys(issue.path))
        issues.push({
          path: formatPath([...issue.path, key]),
          message: `unknown key${hint ? ` (did you mean "${hint}"?)` : ''}`,
        })
      }
    } else if (issue.code === 'invalid_type' && /received undefined$/.test(issue.message)) {
      issues.push({ path: formatPath(issue.path), message: `missing (expected ${issue.expected})` })
    } else if (
      issue.code === 'invalid_union' &&
      issue.errors.flat().some((e) => e.path.length === 0 && /received undefined$/.test(e.message))
    ) {
      issues.push({ path: formatPath(issue.path), message: `missing (${issue.message})` })
    } else {
      issues.push({ path: formatPath(issue.path), message: issue.message })
    }
  }
  return issues
}

/** Validates a parsed JSON value as a series. Throws SeriesError naming every bad path. */
export function parseSeries(value: unknown, source?: string): Series {
  const result = SeriesSchema.safeParse(value)
  if (!result.success) throw new SeriesError(toIssues(result.error), source)
  return result.data
}

/** Parses series JSON text (a file's contents). */
export function parseSeriesJson(text: string, source?: string): Series {
  let value: unknown
  try {
    value = JSON.parse(text)
  } catch (err) {
    throw new SeriesError(
      [{ path: '', message: `not valid JSON: ${(err as Error).message}` }],
      source,
    )
  }
  return parseSeries(value, source)
}

/** The series as `dld series export` writes it: 2-space JSON in schema order, newline at the end. */
export function formatSeries(series: Series): string {
  return `${JSON.stringify(series, null, 2)}\n`
}

/** Every path whose value is still "TODO". */
export function findTodos(series: Series): string[] {
  const out: string[] = []
  const walk = (value: unknown, path: PropertyKey[]) => {
    if (value === TODO) out.push(formatPath(path))
    else if (Array.isArray(value)) {
      for (const [i, v] of value.entries()) walk(v, [...path, i])
    } else if (value && typeof value === 'object') {
      for (const [k, v] of Object.entries(value)) walk(v, [...path, k])
    }
  }
  walk(series, [])
  return out
}

function known<T>(value: T | typeof TODO): value is T {
  return value !== TODO
}

function origin(url: string): string {
  return new URL(url).origin.toLowerCase()
}

interface RockTarget {
  path: string
  origin: string
  guid: string
  id: number | typeof TODO
}

function rockTargets(
  base: string,
  baseUrl: string,
  channels: Record<string, z.infer<typeof RockChannel>>,
): RockTarget[] {
  return Object.entries(channels).map(([name, channel]) => ({
    path: `${base}.${name}`,
    origin: origin(baseUrl),
    guid: channel.guid,
    id: channel.id,
  }))
}

/**
 * D5: a drafts target must differ from every live target of the same platform, so a drafts run
 * can never write where the public sees it. Returns one issue per clash; TODO values never clash.
 */
export function draftsClashes(series: Series): ConfigIssue[] {
  const issues: ConfigIssue[] = []
  const live = series.destinations
  const drafts = series.drafts

  if (typeof drafts.wistia === 'object') {
    if (
      drafts.wistia.projectName.trim().toLowerCase() ===
      live.wistia.projectName.trim().toLowerCase()
    ) {
      issues.push({
        path: 'drafts.wistia.projectName',
        message: `is the live project "${live.wistia.projectName}" (destinations.wistia.projectName): drafts would land in the live project and in Rock's live folder`,
      })
    }
    if (
      known(drafts.wistia.projectId) &&
      known(live.wistia.projectId) &&
      drafts.wistia.projectId.toLowerCase() === live.wistia.projectId.toLowerCase()
    ) {
      issues.push({
        path: 'drafts.wistia.projectId',
        message: `is the live project id (destinations.wistia.projectId): drafts would land in the live project`,
      })
    }
  }

  const liveRock = [
    ...rockTargets(
      'destinations.rock-daily.channels',
      live['rock-daily'].baseUrl,
      live['rock-daily'].channels,
    ),
    ...rockTargets('destinations.rock-email', live['rock-email'].baseUrl, {
      channel: live['rock-email'].channel,
    }),
  ]
  const draftRock: RockTarget[] = []
  const rd = drafts['rock-daily']
  if (typeof rd === 'object')
    draftRock.push(...rockTargets('drafts.rock-daily.channels', rd.baseUrl, rd.channels))
  const re = drafts['rock-email']
  if (typeof re === 'object')
    draftRock.push(...rockTargets('drafts.rock-email', re.baseUrl, { channel: re.channel }))

  for (const d of draftRock) {
    for (const l of liveRock) {
      if (d.origin !== l.origin) continue
      if (d.guid === l.guid) {
        issues.push({
          path: `${d.path}.guid`,
          message: `is a live channel (${l.path}.guid on ${l.origin}): use a staging server or test channels (M2)`,
        })
      } else if (known(d.id) && known(l.id) && d.id === l.id) {
        issues.push({
          path: `${d.path}.id`,
          message: `is a live channel id (${l.path}.id on ${l.origin}): use a staging server or test channels (M2)`,
        })
      }
    }
  }
  return issues
}

export interface SeriesCheck {
  series: Series
  /** Problems that stop an import (D5 clashes). */
  errors: ConfigIssue[]
  /** Paths still "TODO": fine to import, but a destination that needs one can't ship yet. */
  todos: string[]
}

/** `dld series check`: the schema (throws SeriesError), then D5, then the TODO list. */
export function checkSeries(text: string, source?: string): SeriesCheck {
  const series = parseSeriesJson(text, source)
  return { series, errors: draftsClashes(series), todos: findTodos(series) }
}

/** Hosts this series talks to that aren't fixed platform hosts: Rock, live and drafts. */
export function seriesHosts(series: Series): Array<{ host: string; service: string }> {
  const out = new Map<string, string>()
  const add = (url: string, service: string) => {
    const host = new URL(url).host.toLowerCase()
    if (!out.has(host)) out.set(host, service)
  }
  add(series.destinations['rock-daily'].baseUrl, 'Rock')
  add(series.destinations['rock-email'].baseUrl, 'Rock')
  for (const target of [series.drafts['rock-daily'], series.drafts['rock-email']]) {
    if (typeof target === 'object') add(target.baseUrl, 'Rock (drafts)')
  }
  return [...out].map(([host, service]) => ({ host, service }))
}
