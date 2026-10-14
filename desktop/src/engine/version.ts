import pkg from '../../package.json' with { type: 'json' }

export const VERSION: string = pkg.version

/** The lowest Node major `engines.node` allows (">=24" → 24). */
export const MIN_NODE_MAJOR: number = Number(/\d+/.exec(pkg.engines.node)?.[0] ?? 0)
