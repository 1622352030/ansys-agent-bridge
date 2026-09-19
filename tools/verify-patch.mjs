/**
 * Offline check of cordis.patch.yml against the constraints the harness
 * actually imposes.
 *
 * Two harness lines disagree about what a `!!js` expression may use. Measured:
 *
 *   * Desktop app 0.1.5-rc.2 — evaluates `!!js` from a module that imports
 *     `createRequire` at top level, and tolerates async expressions.
 *   * CLI 0.1.1-rc.1 (`npm i -g @deepseek-ai/dsh`) — evaluates
 *     `new Function('ctx', 'expr', 'with (ctx) { return eval(expr) }')` from a
 *     module WITHOUT `createRequire`, and does not await the result.
 *
 * So a patch can boot on the Desktop and leave the CLI with a profile that will
 * not start. This script refuses any `!!js` expression that would depend on the
 * newer line's extra scope: it evaluates in a deliberately bare context — only
 * `ctx`, exactly as the loader builds it — and rejects a promise result.
 *
 * Run: node tools/verify-patch.mjs
 */
import assert from 'node:assert/strict'
import { existsSync, readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const repoRoot = dirname(here)
const patchPath = join(repoRoot, 'cordis.patch.yml')
const patch = readFileSync(patchPath, 'utf8')

/** Evaluate exactly as the loader does, with nothing else in scope. */
function evaluate(expr, baseUrl) {
  const run = new Function('ctx', 'expr', 'with (ctx) { return eval(expr) }')
  return run({ baseUrl }, expr)
}

const expressions = [...patch.matchAll(/!!js\s+"((?:[^"\\]|\\.)*)"/g)].map((m) =>
  JSON.parse(`"${m[1]}"`),
)

for (const [index, expr] of expressions.entries()) {
  let value
  try {
    value = evaluate(expr, pathToFileURL(join(repoRoot, 'package.json')).href)
  } catch (error) {
    assert.fail(
      `!!js expression #${index + 1} threw in a bare loader scope: ${error.message}\n` +
        `It may rely on something only the Desktop harness exposes.\n  ${expr}`,
    )
  }
  assert.ok(
    !(value instanceof Promise),
    `!!js expression #${index + 1} returned a promise; the CLI's interpolate does not await it`,
  )
  if (typeof value === 'string' && value !== '' && value.includes('uv')) {
    assert.ok(existsSync(value), `!!js expression #${index + 1} resolved to a missing path: ${value}`)
  }
}

// The MCP row's command must be resolvable without any `!!js`, which is what
// makes this patch portable across both harness lines.
assert.match(patch, /command:\s*uv\s*$/m, 'the MCP command should be the bare `uv`')

// The Python entry point the row launches must exist in this repo, so a typo in
// the console-script name is caught here rather than at connect time.
const pyproject = readFileSync(join(repoRoot, 'python', 'pyproject.toml'), 'utf8')
const script = /--from[\s\S]*?'ansys-bridge-mcp'/.exec(patch)
assert.ok(script, 'the patch should launch ansys-bridge-mcp')
assert.match(
  pyproject,
  /^ansys-bridge-mcp\s*=\s*"ansys_bridge_mcp\.server:main"/m,
  'pyproject.toml must declare the ansys-bridge-mcp console script the patch launches',
)

console.log(`cordis.patch.yml: ${expressions.length} !!js expression(s), all bare-scope safe`)
console.log('verify-patch: OK')
