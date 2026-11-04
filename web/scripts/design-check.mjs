// Machine-checkable part of DESIGN.md: no raw Tailwind text sizes, no arbitrary font sizes, no color literals in components.
import { readdirSync, readFileSync, statSync } from 'node:fs'
import { join } from 'node:path'
const root = new URL('../src', import.meta.url).pathname
const files = []
const walk = d => { for (const n of readdirSync(d)) { const p = join(d, n); statSync(p).isDirectory() ? walk(p) : /\.(tsx?|css)$/.test(n) && files.push(p) } }
walk(root)
const rules = [
  [/\btext-(\d*xl|xs|sm|base|lg)\b/, 'raw Tailwind text size; use text-ui-*'],
  [/\btext-\[[^\]]+\]/, 'arbitrary font size; use text-ui-* or text-code'],
  [/\bfont(-size)?\s*:/i, 'inline font size; use text-ui-*'],
  [/fontSize\s*:/, 'inline font size; use text-ui-*'],
  [/#[0-9a-fA-F]{3,8}\b|\b(rgba?|hsla?|oklch|oklab)\(/, 'color literal in a component; add a token to theme.css'],
  [/\b(bg|text|border|ring|fill|stroke|from|to|via)-(white|black|slate|gray|zinc|neutral|stone|red|orange|amber|yellow|lime|green|emerald|teal|cyan|sky|blue|indigo|violet|purple|fuchsia|pink|rose)(-\d{2,3})?\b/, 'Tailwind palette color; use a semantic token'],
]
const boundary = [/from '\.\.\/\.\.\/plugins\//, 'a plugin imports another plugin directly; share through the kernel, the store or src/ui']
let bad = 0
for (const f of files) {
  if (f.endsWith('theme.css')) continue
  const lines = readFileSync(f, 'utf8').split('\n')
  lines.forEach((l, i) => {
    for (const [re, msg] of rules) if (re.test(l)) { console.log(`${f.replace(root, 'src')}:${i + 1}: ${msg}\n    ${l.trim()}`); bad++ }
    if (f.includes('/plugins/') && boundary[0].test(l) && !/from '\.\.\/attention'/.test(l)) { console.log(`${f.replace(root, 'src')}:${i + 1}: ${boundary[1]}\n    ${l.trim()}`); bad++ }
  })
}
console.log(bad ? `${bad} design-system defects` : `design check ok (${files.length} files)`)
process.exit(bad ? 1 : 0)
