import { homedir } from 'node:os'

/** ~/… for paths in the home dir. */
export function tilde(path: string, home: string = homedir()): string {
  return path === home || path.startsWith(`${home}/`) ? `~${path.slice(home.length)}` : path
}

/** Left-aligned columns, two spaces apart, no trailing spaces. */
export function table(rows: string[][]): string {
  const widths: number[] = []
  for (const row of rows) {
    for (const [i, cell] of row.entries()) widths[i] = Math.max(widths[i] ?? 0, cell.length)
  }
  return rows
    .map((row) =>
      row
        .map((cell, i) => cell.padEnd(widths[i] ?? 0))
        .join('  ')
        .trimEnd(),
    )
    .join('\n')
}

export function bytes(n: number): string {
  if (n < 1024) return `${n} B`
  if (n < 1024 ** 2) return `${(n / 1024).toFixed(1)} KB`
  return `${(n / 1024 ** 2).toFixed(1)} MB`
}
