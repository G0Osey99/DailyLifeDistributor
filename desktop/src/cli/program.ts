import { Command, CommanderError } from 'commander'
import {
  type DoctorSystem,
  defaultProbes,
  doctorExitCode,
  formatTable,
  type Probe,
  realSystem,
  runDoctor,
  type SeriesInfo,
} from '../engine/doctor.js'
import { redact } from '../engine/log.js'
import { KeychainSecrets, type Secrets } from '../engine/secrets.js'
import { VERSION } from '../engine/version.js'
import { registerDb } from './db.js'
import { readHidden } from './prompt.js'
import { registerSecrets } from './secrets.js'
import { registerSeries } from './series.js'

export interface CliIo {
  out(text: string): void
  err(text: string): void
}

export interface CliDeps {
  io?: CliIo
  system?: () => DoctorSystem
  probes?: (series: SeriesInfo[]) => Probe[]
  secrets?: () => Secrets
  /** Reads a secret without echoing it (a hidden prompt, or piped stdin). */
  readSecret?: (prompt: string) => Promise<string>
}

/** What each command group gets from runCli. */
export interface CommandContext {
  io: CliIo
  secrets: () => Secrets
  readSecret: (prompt: string) => Promise<string>
  setExit(code: number): void
}

const stdio: CliIo = {
  out: (text) => process.stdout.write(text),
  err: (text) => process.stderr.write(text),
}

/** Runs `dld <argv>` and resolves to the exit code. Nothing here calls process.exit. */
export async function runCli(argv: string[], deps: CliDeps = {}): Promise<number> {
  const io = deps.io ?? stdio
  let exitCode = 0
  const ctx: CommandContext = {
    io,
    secrets: deps.secrets ?? (() => new KeychainSecrets()),
    readSecret: deps.readSecret ?? ((prompt) => readHidden(prompt)),
    setExit: (code) => {
      exitCode = code
    },
  }

  const program = new Command('dld')
    .description('Daily Life Distributor: ship each day to every platform from this Mac')
    .version(VERSION, '-V, --version', 'print the dld version')
    .exitOverride()
    .configureOutput({ writeOut: io.out, writeErr: io.err })

  program
    .command('doctor')
    .description('check this Mac can run dld: one row per check, with a fix for each problem')
    .action(async () => {
      const sys = (deps.system ?? realSystem)()
      // The series supplies its own hosts (Rock); the series row reports a read failure.
      const series = await sys.series().catch(() => [])
      const rows = await runDoctor((deps.probes ?? defaultProbes)(series), sys)
      io.out(String(redact(formatTable(rows))))
      exitCode = doctorExitCode(rows)
    })

  registerSeries(program, ctx)
  registerSecrets(program, ctx)
  registerDb(program, ctx)

  try {
    await program.parseAsync(argv, { from: 'user' })
  } catch (err) {
    if (err instanceof CommanderError) return err.exitCode
    const { message } = redact(err) as { message: string }
    io.err(`dld: ${message}\n`)
    return 1
  }
  return exitCode
}
