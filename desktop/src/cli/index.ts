import { redact } from '../engine/log.js'
import { runCli } from './program.js'

try {
  process.exitCode = await runCli(process.argv.slice(2))
} catch (err) {
  const { message } = redact(err) as { message: string }
  process.stderr.write(`dld: ${message}\n`)
  process.exitCode = 1
}
