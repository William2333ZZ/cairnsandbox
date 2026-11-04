# Contributing

Thanks for looking. This file is the short version; `docs/README.md` explains how the design documents fit together and
`docs/decisions-index.md` lists every design decision with its reasoning (`docs/decisions.md`).

## Ground rules

* **Sign off your commits** (`git commit -s`). By doing so you certify the Developer Certificate of Origin (https://developercertificate.org).
  The maintainers may change this to a contributor license agreement before accepting large contributions; they will say so in an issue.
* Be kind: see `CODE_OF_CONDUCT.md`. Security problems go through `SECURITY.md`, never a public issue.
* Open an issue before a large change. A design that changes an interface or a guarantee gets a decision record in `docs/decisions.md`
  (context, decision, what is not done, what would overturn it) and a line in `docs/decisions-index.md`.

## Layers

| Layer | Directories | May depend on |
| --- | --- | --- |
| Sandbox cloud | `cp/` `node/` `guest/` `sdk/` `console/` `deploy/` `recipes/` | itself |
| Agent layer | `aios/` `intake/` `eval/` `web/` | the sandbox cloud's HTTP API or the SDK only |

`tests/unit/test_layers.py` enforces this. The control plane may forward to the optional agent services over HTTP; nothing else crosses.

## Running the tests

```bash
uv run --with pytest --with jinja2 --with pyyaml python -m pytest tests/unit      # Python unit tests (a few memchain/uffd tests need Linux)
(cd cp && go vet ./... && go test ./...)                                         # control plane (needs no KVM)
(cd sdk/js && npm ci && npm run build)                                           # JS SDK type check and build
```

End-to-end node tests (`node/test-*.py`) need a Linux host with KVM and the images from `scripts/bootstrap.sh`; they are described in
`docs/010-deployment.md`. A pull request does not need to run them, but say so if you changed `node/` or `guest/` and could not.

## Style

Match the surrounding code: comment density, naming and idiom. Comments say why, not what. Prefer plain standard-library code; every
dependency is a thing we have to keep safe. New modules and top-level files get a row in `docs/011-architecture.md`
(`tests/unit/test_docs_index.py` checks). User-visible changes get a line in `CHANGELOG.md`.
