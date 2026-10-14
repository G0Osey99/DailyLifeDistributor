import { describe, expect, it } from 'vitest'
import { KeychainSecrets } from '../src/engine/secrets.js'

// Rule 6: touches the real login keychain, so it runs only with DLD_INTEGRATION=1 on a Mac.
// It writes, reads back and deletes one dummy item, dld.selftest; nothing else is touched.

const NAME = 'dld.selftest'

describe.skipIf(process.env.DLD_INTEGRATION !== '1' || process.platform !== 'darwin')(
  'real keychain',
  () => {
    it.each([
      [
        'JSON with quotes',
        JSON.stringify({ selftest: true, quote: 'it\'s "quoted"', spaced: 'a b  c' }),
      ],
      ['multi-line, non-ASCII JSON', '{\n  "café": "✓",\n  "tab": "a\tb"\n}'],
      ['a printable value that looks like hex', 'ABCDEF12'],
    ])('stores, reads back and deletes %s', async (_, value) => {
      const keychain = new KeychainSecrets()
      try {
        await keychain.set(NAME, value)
        expect(await keychain.has(NAME)).toBe(true)
        expect(await keychain.get(NAME)).toBe(value)
        await keychain.set(NAME, `${value}!`)
        expect(await keychain.get(NAME)).toBe(`${value}!`)
      } finally {
        await keychain.delete(NAME)
      }
      expect(await keychain.has(NAME)).toBe(false)
      expect(await keychain.get(NAME)).toBeNull()
      expect(await keychain.delete(NAME)).toBe(false)
    })
  },
)
