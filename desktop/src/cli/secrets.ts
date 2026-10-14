import { readFileSync } from 'node:fs'
import type { Command } from 'commander'
import { checkSecretName, KNOWN_SECRETS } from '../engine/secrets.js'
import { table } from './format.js'
import type { CommandContext } from './program.js'

// `dld secrets set | list | delete` (§S4). Values go into the keychain and never come out here.

export function registerSecrets(program: Command, ctx: CommandContext): void {
  const secrets = program
    .command('secrets')
    .description('keys and tokens in the macOS keychain (§S4)')

  secrets
    .command('set')
    .description('store a secret: typed at a hidden prompt, piped in, or read from a file')
    .argument('<name>', 'e.g. wistia.token (dld secrets list shows the names)')
    .option('--from-file <path>', 'read the value from this file (e.g. the YouTube client JSON)')
    .action(async (name: string, opts: { fromFile?: string }) => {
      checkSecretName(name)
      const known = KNOWN_SECRETS.find((s) => s.name === name)
      let value: string
      if (opts.fromFile) {
        try {
          value = readFileSync(opts.fromFile, 'utf8').replace(/\r?\n$/, '')
        } catch (err) {
          throw new Error(
            `can't read ${opts.fromFile}: ${(err as NodeJS.ErrnoException).code ?? (err as Error).message}`,
          )
        }
      } else {
        value = await ctx.readSecret(`${name} (typing is hidden): `)
      }
      if (!value) throw new Error(`nothing to store for ${name}: the value is empty`)
      if (known?.json) {
        try {
          JSON.parse(value)
        } catch {
          throw new Error(`${name} must be JSON (${known.what}); nothing stored`)
        }
      }
      await ctx.secrets().set(name, value)
      ctx.io.out(`Stored ${name} in the keychain (${Buffer.byteLength(value)} bytes).\n`)
      if (opts.fromFile) ctx.io.out(`The keychain now holds it: you can delete ${opts.fromFile}.\n`)
      if (!known)
        ctx.io.err(`note: dld doesn't read ${name}; dld secrets list shows the names it uses.\n`)
    })

  secrets
    .command('list')
    .description("show which of dld's secrets are stored (never their values)")
    .action(async () => {
      const store = ctx.secrets()
      const rows = await Promise.all(
        KNOWN_SECRETS.map(async (s) => {
          const present = await store.has(s.name)
          return [s.name, present ? 'present' : 'missing', s.what, present ? '' : s.fix]
        }),
      )
      const present = rows.filter((r) => r[1] === 'present').length
      ctx.io.out(
        `${table([['NAME', 'STATUS', 'WHAT', 'HOW TO SET'], ...rows])}\n\n${present} present · ${rows.length - present} missing\n`,
      )
    })

  secrets
    .command('delete')
    .description('remove a secret from the keychain')
    .argument('<name>', 'e.g. wistia.token')
    .action(async (name: string) => {
      const deleted = await ctx.secrets().delete(name)
      ctx.io.out(
        deleted ? `Deleted ${name} from the keychain.\n` : `${name} was not in the keychain.\n`,
      )
    })
}
