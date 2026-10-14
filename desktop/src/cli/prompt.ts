// A hidden prompt for `dld secrets set`: nothing typed is echoed. Piped input is read whole.

export class PromptCancelled extends Error {
  constructor() {
    super('cancelled')
    this.name = 'PromptCancelled'
  }
}

export async function readHidden(
  prompt: string,
  input: NodeJS.ReadStream = process.stdin,
  output: NodeJS.WriteStream = process.stderr,
): Promise<string> {
  if (!input.isTTY) {
    let text = ''
    input.setEncoding('utf8')
    for await (const chunk of input) text += chunk
    return text.replace(/\r?\n$/, '')
  }
  output.write(prompt)
  input.setRawMode(true)
  input.setEncoding('utf8')
  input.resume()
  try {
    return await new Promise<string>((resolve, reject) => {
      let value = ''
      const onData = (chunk: string) => {
        for (const ch of chunk) {
          if (ch === '\r' || ch === '\n') {
            input.off('data', onData)
            return resolve(value)
          }
          if (ch === '\u0003' || ch === '\u0004') {
            input.off('data', onData)
            return reject(new PromptCancelled())
          }
          if (ch === '\u007f' || ch === '\b') value = value.slice(0, -1)
          else if (ch >= ' ') value += ch
        }
      }
      input.on('data', onData)
    })
  } finally {
    input.setRawMode(false)
    input.pause()
    output.write('\n')
  }
}
