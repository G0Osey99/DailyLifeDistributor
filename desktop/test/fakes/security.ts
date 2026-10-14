import { EventEmitter } from 'node:events'
import { PassThrough, Writable } from 'node:stream'

// A stand-in for /usr/bin/security that behaves like the real one where dld depends on it
// (checked on macOS 26.6 in step 1.2): `-i` reads commands from stdin, one per line of at most
// 4095 characters (a longer line is split and its tail run as a command); `-X` takes hex;
// `find-generic-password -g` prints `password: "text"` for printable values and
// `password: 0xHEX  "escaped"` otherwise, on stderr; a missing item exits 44.

export interface SpawnCall {
  cmd: string
  args: string[]
  stdin: string
}

const NOT_FOUND = {
  code: 44,
  stdout: '',
  stderr:
    'security: SecKeychainSearchCopyNext: The specified item could not be found in the keychain.\n',
}

function isPrintable(bytes: Buffer): boolean {
  return bytes.every((b) => b >= 0x20 && b <= 0x7e)
}

export class FakeKeychain {
  readonly items = new Map<string, Buffer>()
  readonly calls: SpawnCall[] = []

  private key(service: string, account: string) {
    return `${service}\u0000${account}`
  }

  private flag(tokens: string[], name: string): string | undefined {
    const i = tokens.indexOf(name)
    return i >= 0 ? tokens[i + 1] : undefined
  }

  private command(tokens: string[]): { code: number; stdout: string; stderr: string } {
    const [cmd, ...rest] = tokens
    const service = this.flag(rest, '-s') ?? ''
    const account = this.flag(rest, '-a') ?? ''
    const key = this.key(service, account)
    switch (cmd) {
      case 'add-generic-password': {
        const hex = this.flag(rest, '-X')
        const plain = this.flag(rest, '-w')
        if (this.items.has(key) && !rest.includes('-U')) {
          return {
            code: 45,
            stdout: '',
            stderr: 'security: The specified item already exists in the keychain.\n',
          }
        }
        this.items.set(
          key,
          hex !== undefined ? Buffer.from(hex, 'hex') : Buffer.from(plain ?? '', 'utf8'),
        )
        return { code: 0, stdout: '', stderr: '' }
      }
      case 'delete-generic-password':
        if (!this.items.delete(key))
          return {
            ...NOT_FOUND,
            stderr: `${NOT_FOUND.stderr}delete-generic-password: returned -25300\n`,
          }
        return { code: 0, stdout: 'password has been deleted.\n', stderr: '' }
      case 'find-generic-password': {
        const value = this.items.get(key)
        if (!value) return NOT_FOUND
        const attributes = `keychain: "/Users/test/Library/Keychains/login.keychain-db"\nclass: "genp"\n    "acct"<blob>="${account}"\n    "svce"<blob>="${service}"\n`
        if (rest.includes('-w')) {
          return {
            code: 0,
            stdout: `${isPrintable(value) ? value.toString('latin1') : value.toString('hex')}\n`,
            stderr: '',
          }
        }
        if (!rest.includes('-g')) return { code: 0, stdout: attributes, stderr: '' }
        const shown = isPrintable(value)
          ? `"${value.toString('latin1')}"`
          : `0x${value.toString('hex').toUpperCase()}  "${[...value].map((b) => (b >= 0x20 && b <= 0x7e ? String.fromCharCode(b) : `\\${b.toString(8).padStart(3, '0')}`)).join('')}"`
        return { code: 0, stdout: attributes, stderr: `password: ${shown}\n` }
      }
      default:
        return { code: 1, stdout: '', stderr: `security: unknown command "${cmd}"\n` }
    }
  }

  /** What `security <args>` with `stdin` would print and exit with. */
  run(args: string[], stdin: string): { code: number; stdout: string; stderr: string } {
    if (args[0] !== '-i') return this.command(args)
    let last = { code: 0, stdout: '', stderr: '' }
    const out = { stdout: '', stderr: '' }
    for (const line of stdin.split('\n')) {
      for (let at = 0; at < line.length; at += 4095) {
        last = this.command(
          line
            .slice(at, at + 4095)
            .split(' ')
            .filter(Boolean),
        )
        out.stdout += last.stdout
        out.stderr += last.stderr
      }
    }
    return { code: last.code, ...out }
  }

  /** A `spawn` replacement: a child process whose stdin, stdout and stderr act like security's. */
  spawn = (cmd: string, args: string[]) => {
    const child = new EventEmitter() as EventEmitter & {
      stdin: Writable
      stdout: PassThrough
      stderr: PassThrough
      kill: () => void
    }
    child.stdout = new PassThrough()
    child.stderr = new PassThrough()
    child.kill = () => {}
    let stdin = ''
    child.stdin = new Writable({
      write: (chunk, _enc, done) => {
        stdin += chunk.toString()
        done()
      },
      final: (done) => {
        this.calls.push({ cmd, args: [...args], stdin })
        const result = this.run(args, stdin)
        child.stdout.end(result.stdout)
        child.stderr.end(result.stderr)
        setImmediate(() => child.emit('close', result.code))
        done()
      },
    })
    return child
  }
}
