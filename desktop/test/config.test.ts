import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'
import {
  checkSeries,
  DESTINATIONS,
  draftsClashes,
  findTodos,
  formatSeries,
  parseSeries,
  parseSeriesJson,
  type Series,
  SeriesError,
  seriesHosts,
} from '../src/engine/config.js'

const EXAMPLE_PATH = new URL('../examples/daily-life.series.json', import.meta.url)
const EXAMPLE_TEXT = readFileSync(EXAMPLE_PATH, 'utf8')

function example(): Series {
  return parseSeriesJson(EXAMPLE_TEXT)
}

/** The example as plain JSON, to edit before validating. */
// biome-ignore lint/suspicious/noExplicitAny: tests edit the raw JSON freely, typos included
function raw(): Record<string, any> {
  return JSON.parse(EXAMPLE_TEXT)
}

function issuesOf(value: unknown): Array<{ path: string; message: string }> {
  try {
    parseSeries(value)
  } catch (err) {
    if (err instanceof SeriesError) return err.issues
    throw err
  }
  throw new Error('expected the series to be invalid')
}

describe('series config (§S2)', () => {
  it("validates the example with today's legacy values", () => {
    const s = example()
    expect(s.id).toBe('daily-life')
    expect(s.times).toEqual({ youtube: '14:00', vista: '07:00', spotify: 'TODO' })
    expect(s.destinations.youtube.categoryId).toBe('22')
    expect(s.destinations.wistia).toMatchObject({
      projectName: 'Daily Life App',
      mediaName: 'app YYMMDD',
    })
    expect(s.destinations.vista.profiles.map((p) => p.network)).toEqual(['facebook', 'instagram'])
    expect(s.destinations['rock-daily'].channels.spotlight).toEqual({
      guid: '3199ba17-c18a-4a0e-82d6-6ff2160c145c',
      id: 125,
    })
    expect(s.destinations['rock-email'].channel.id).toBe(24)
    expect(s.waits['youtube.url']).toEqual({ pollS: 1, capS: 7200 })
    expect(s.limits.youtubeQuota.dailyCap).toBe(10000)
    expect(s.limits.youtubeQuota.costs['videos.insert']).toBe(100)
    expect(s.limits.breaker).toEqual({ failureThreshold: 3, recoveryS: 120 })
    expect(s.templates.youtube?.footer).toBe(
      'Share this daily devotional with someone to encourage and give them hope today!',
    )
  })

  it('formats exactly as the example file is written, so import → export is unchanged', () => {
    expect(formatSeries(example())).toBe(EXAMPLE_TEXT)
  })

  it('names the path of a misspelled key, with a suggestion', () => {
    const top = raw()
    top.timeZone = top.timezone
    delete top.timezone
    expect(issuesOf(top)).toEqual([
      { path: 'timezone', message: 'missing (expected string)' },
      { path: 'timeZone', message: 'unknown key (did you mean "timezone"?)' },
    ])

    const nested = raw()
    nested.destinations['rock-daily'].channels.spotlight.giud = 'x'
    expect(issuesOf(nested)).toContainEqual({
      path: 'destinations.rock-daily.channels.spotlight.giud',
      message: 'unknown key (did you mean "guid"?)',
    })

    const inArray = raw()
    inArray.destinations.vista.profiles[1].netwrok = 'instagram'
    expect(issuesOf(inArray)).toContainEqual({
      path: 'destinations.vista.profiles[1].netwrok',
      message: 'unknown key (did you mean "network"?)',
    })

    const inDrafts = raw()
    inDrafts.drafts.wistia = {
      projectName: 'DLD rebuild test',
      projectId: 'TODO',
      projectID: 'abc123',
    }
    expect(issuesOf(inDrafts)).toContainEqual({
      path: 'drafts.wistia.projectID',
      message: 'unknown key (did you mean "projectId"?)',
    })

    const unknownDestination = raw()
    unknownDestination.times.simplecast = '00:00'
    expect(issuesOf(unknownDestination)).toEqual([
      { path: 'times.simplecast', message: 'unknown key' },
    ])
  })

  it('names the path and the expectation for a bad value', () => {
    const s = raw()
    s.times.youtube = '2pm'
    s.destinations['rock-email'].channel.guid = 'not-a-guid'
    s.drafts.youtube = 'unlisted'
    expect(issuesOf(s)).toEqual([
      { path: 'times.youtube', message: 'expected a 24-hour time "HH:MM"' },
      { path: 'destinations.rock-email.channel.guid', message: 'expected a lower-case GUID' },
      { path: 'drafts.youtube', message: 'expected "private" or "plan"' },
    ])
  })

  it('allows youtube_title and podcast_title columns to be left out, but not the others', () => {
    const s = raw()
    expect(s.sheet.columns.youtube_title_column).toBeUndefined()
    s.sheet.columns.podcast_title_column = 'Podcast Title'
    expect(() => parseSeries(s)).not.toThrow()
    delete s.sheet.columns.episode_title_column
    expect(issuesOf(s)).toEqual([
      {
        path: 'sheet.columns.episode_title_column',
        message: 'missing (expected a column header, or "TODO")',
      },
    ])
  })

  it('reports invalid JSON as one problem', () => {
    expect(() => parseSeriesJson('{ "id": ', 'broken.json')).toThrow(
      /broken\.json: a problem in the series\n {2}\(root\): not valid JSON/,
    )
  })

  it('lists every value still TODO, by path', () => {
    const todos = findTodos(example())
    expect(todos).toContain('destinations.wistia.projectId')
    expect(todos).toContain('destinations.spotify.showUrl')
    expect(todos).toContain('times.spotify')
    expect(todos).toContain('destinations.rock-email.attributeKeys.sent')
    expect(todos).toContain('drafts.rock-daily')
    expect(todos).not.toContain('destinations.rock-email.channel.id')
  })

  it('accepts "plan" as a drafts target for every destination', () => {
    const s = raw()
    for (const d of DESTINATIONS) s.drafts[d] = 'plan'
    expect(draftsClashes(parseSeries(s))).toEqual([])
  })
})

describe('drafts targets differ from live targets (D5)', () => {
  it('the example has no clash: its unknown targets are TODO', () => {
    expect(checkSeries(EXAMPLE_TEXT).errors).toEqual([])
  })

  it('fails when the Wistia drafts project is the live one, by id or by name', () => {
    const s = raw()
    s.destinations.wistia.projectId = 'abc123def4'
    s.drafts.wistia = { projectName: 'DLD rebuild test', projectId: 'abc123def4' }
    expect(draftsClashes(parseSeries(s))).toEqual([
      {
        path: 'drafts.wistia.projectId',
        message:
          'is the live project id (destinations.wistia.projectId): drafts would land in the live project',
      },
    ])
    s.drafts.wistia = { projectName: 'daily life app', projectId: 'TODO' }
    expect(draftsClashes(parseSeries(s)).map((i) => i.path)).toEqual(['drafts.wistia.projectName'])
  })

  it('fails when a Rock drafts channel is a live channel on the same server', () => {
    const s = raw()
    const live = s.destinations['rock-daily']
    s.drafts['rock-daily'] = { baseUrl: live.baseUrl, channels: structuredClone(live.channels) }
    s.drafts['rock-email'] = {
      baseUrl: 'https://ROCK.lcbcchurch.com/',
      channel: { guid: '11111111-2222-3333-4444-555555555555', id: 24 },
    }
    const paths = draftsClashes(parseSeries(s)).map((i) => i.path)
    expect(paths).toEqual([
      'drafts.rock-daily.channels.parent.guid',
      'drafts.rock-daily.channels.reflection.guid',
      'drafts.rock-daily.channels.vista.guid',
      'drafts.rock-daily.channels.spotlight.guid',
      'drafts.rock-email.channel.id',
    ])
  })

  it('a drafts channel that is a live channel of the other Rock destination also clashes', () => {
    const s = raw()
    s.drafts['rock-email'] = {
      baseUrl: s.destinations['rock-email'].baseUrl,
      channel: { guid: s.destinations['rock-daily'].channels.vista.guid, id: 999 },
    }
    expect(draftsClashes(parseSeries(s))).toEqual([
      {
        path: 'drafts.rock-email.channel.guid',
        message:
          'is a live channel (destinations.rock-daily.channels.vista.guid on https://rock.lcbcchurch.com): use a staging server or test channels (M2)',
      },
    ])
  })

  it('passes for a staging server with the same channels, or test channels on the live server', () => {
    const staging = raw()
    staging.drafts['rock-daily'] = {
      baseUrl: 'https://rock-staging.example.org',
      channels: staging.destinations['rock-daily'].channels,
    }
    expect(draftsClashes(parseSeries(staging))).toEqual([])

    const test = raw()
    test.drafts['rock-email'] = {
      baseUrl: test.destinations['rock-email'].baseUrl,
      channel: { guid: '11111111-2222-3333-4444-555555555555', id: 900 },
    }
    expect(draftsClashes(parseSeries(test))).toEqual([])
  })

  it('collects the Rock hosts, live and drafts, for doctor', () => {
    const s = raw()
    s.drafts['rock-email'] = {
      baseUrl: 'https://rock-staging.example.org',
      channel: s.destinations['rock-email'].channel,
    }
    expect(seriesHosts(parseSeries(s))).toEqual([
      { host: 'rock.lcbcchurch.com', service: 'Rock' },
      { host: 'rock-staging.example.org', service: 'Rock (drafts)' },
    ])
  })
})
