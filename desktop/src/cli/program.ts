import { Command, CommanderError } from 'commander'
import {
  type DoctorSystem,
  defaultProbes,
  doctorExitCode,
  formatTable,
  type Probe,
  realSystem,
  runDoctor,
} from '../engine/doctor.js'
import { redact } from '../engine/log.js'
import { VERSION } from '../engine/version.js'

export interface CliIo {
  out(text: string): void
  err(text: string): void
}

export interface CliDeps {
  io?: CliIo
  system?: () => DoctorSystem
  probes?: () => Probe[]
}

const stdio: CliIo = {
  out: (text) => process.stdout.write(text),
  err: (text) => process.stderr.write(text),
}

/** Runs `dld <argv>` and resolves to the exit code. Nothing here calls process.exit. */
export async function runCli(argv: string[], deps: CliDeps = {}): Promise<number> {
  const io = deps.io ?? stdio
  let exitCode = 0

  const program = new Command('dld')
    .description('Daily Life Distributor: ship each day to every platform from this Mac')
    .version(VERSION, '-V, --version', 'print the dld version')
    .exitOverride()
    .configureOutput({ writeOut: io.out, writeErr: io.err })

  program
    .command('doctor')
    .description('check this Mac can run dld: one row per check, with a fix for each problem')
    .action(async () => {
      const rows = await runDoctor((deps.probes ?? defaultProbes)(), (deps.system ?? realSystem)())
      io.out(String(redact(formatTable(rows))))
      exitCode = doctorExitCode(rows)
    })

  try {
    await program.parseAsync(argv, { from: 'user' })
  } catch (err) {
    if (err instanceof CommanderError) return err.exitCode
    throw err
  }
  return exitCode
}
