import { mkdtempSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { afterEach } from 'vitest'
import { forgetSecrets } from '../src/engine/log.js'

// Tests never touch the real data dir (§S1): each test file gets its own temp one.
process.env.DLD_DATA_DIR = mkdtempSync(join(tmpdir(), 'dld-test-'))

afterEach(() => {
  forgetSecrets()
})
