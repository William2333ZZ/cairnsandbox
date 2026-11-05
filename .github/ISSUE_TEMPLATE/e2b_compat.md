---
name: E2B compatibility gap
about: An official E2B SDK call does not behave against CairnSandbox
labels: e2b
---
**SDK and version** (e.g. `e2b` Python 2.63.0, npm `e2b` 2.63.0, `e2b-code-interpreter` 2.11.9)

**The call** (a few lines that reproduce it)

**What E2B does** (docs link or what you saw on e2b.app) and **what CairnSandbox does** (error text, status code)

Please check the matrix in `docs/guide/api/e2b.md` and the "E2B compatibility" section of `ROADMAP.md` first: some gaps are known.
Run `ops/e2b-conformance.py` (or `.mjs`) against your setup if you can, and attach its output. Never point an SDK at CairnSandbox with a real e2b.app key set.
