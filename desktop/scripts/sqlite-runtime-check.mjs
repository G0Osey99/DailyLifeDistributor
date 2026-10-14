// D3 evidence for step 1.2: does this runtime expose node:sqlite without a flag, with what dld needs?
// Run: node scripts/sqlite-runtime-check.mjs
//      ELECTRON_RUN_AS_NODE=1 npx electron scripts/sqlite-runtime-check.mjs
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { DatabaseSync } from 'node:sqlite'

const dir = mkdtempSync(join(tmpdir(), 'dld-sqlite-check-'))
try {
  const db = new DatabaseSync(join(dir, 'check.db'))
  const journal = db.prepare('PRAGMA journal_mode = WAL').get().journal_mode
  db.exec('PRAGMA foreign_keys = ON; PRAGMA busy_timeout = 5000')
  db.exec('CREATE TABLE a (id INTEGER PRIMARY KEY); CREATE TABLE b (a_id INTEGER REFERENCES a(id))')
  let fkEnforced = false
  try {
    db.prepare('INSERT INTO b (a_id) VALUES (?)').run(42)
  } catch {
    fkEnforced = true
  }
  db.exec('BEGIN IMMEDIATE')
  db.prepare('INSERT INTO a (id) VALUES (?)').run(1)
  db.exec('ROLLBACK')
  const rolledBack = db.prepare('SELECT count(*) AS n FROM a').get().n === 0
  const json = db.prepare("SELECT json_extract('{\"x\":[1,2]}', '$.x[1]') AS v").get().v
  db.close()
  console.log(
    JSON.stringify({
      runtime: process.versions.electron ? `electron ${process.versions.electron}` : 'node',
      node: process.versions.node,
      modules: process.versions.modules,
      sqlite: process.versions.sqlite,
      execArgv: process.execArgv,
      journal,
      fkEnforced,
      rolledBack,
      json,
    }),
  )
} finally {
  rmSync(dir, { recursive: true, force: true })
}
