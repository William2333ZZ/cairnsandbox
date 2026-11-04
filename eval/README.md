# fc-eval — the evaluator

Implements CONTRACT.md §3 (HTTP API), §5 (agent convention), §6 (taskset format) on top of the control plane (§1).
One file, stdlib only: `evaluator.py`. Tasks live in `tasksets/<name>/`.

## Run locally (no sandboxes)

`--local` swaps the control-plane backend for `LocalBackend`: every "sandbox" is a temp dir and `exec` is `bash -c`.
Good for checking tasks and the pipeline; not isolation.

```bash
cd prototype/fc-node
python3 eval/evaluator.py check basic --local                       # import checks for all tasks (results under $EVAL_STATE/checks/)
python3 eval/evaluator.py run basic --k 2 --local \
   --agent-cmd "cp -R $PWD/eval/tasksets/basic/{task}/reference/. {cwd}/"   # fake agent: copy the reference solution
python3 eval/evaluator.py run basic --k 2 --local --agent-cmd true --base <batch_id> --cascade   # compare vs a base batch
python3 eval/evaluator.py stats <batch_id>
python3 -m pytest -q tests/unit/test_eval_*.py      # or: python3 tests/unit/test_eval_stats.py; python3 tests/unit/test_eval_local.py
```

State (sqlite + per-run artifacts `runs/<run_id>/{ws.tar,traj.json,agent.log}`) goes to `EVAL_STATE` (default `eval/state`).

## Service

```bash
EVAL_PORT=8600 EVAL_STATE=/var/lib/fc-eval CP_URL=http://fc-cp:8700 CP_TOKEN=... \
MODEL_PROXY_URL=http://fc-model:8500 MAX_PARALLEL=2 python3 eval/evaluator.py serve
```

Env: `EVAL_PORT` (8600), `EVAL_STATE`, `CP_URL`, `CP_TOKEN` (optional), `MODEL_PROXY_URL` (optional: per-run token via
`POST /tokens`, usage via `GET /usage/<sandbox>`), `TASKSETS_DIR` (`eval/tasksets`), `MAX_PARALLEL` (2, hard cap 4: the
dev box is shared), `EVAL_TEMPLATE` (`ubuntu-24.04`), `EVAL_BUDGET_USD` (1.0 per run), `CAIRN_MODEL_NAME`.

Routes are exactly CONTRACT §3. A batch runs in a background thread; its runs go through a pool of `MAX_PARALLEL`
workers. `POST /batches {kind:"compare", base_batch, cascade:true}` first runs the `smoke:true` tasks with k=1 and stops
with `accept:false, reason:"smoke"` if any of them fails; otherwise all selected tasks run with `k`.

One run = agent sandbox (init/ at `/task`, prompt at `/task/.task/prompt.md`, agent_cmd as user `agent`, network only
to the model proxy) -> workspace tar (`/task` minus `.task`) -> **separate** fresh sandbox without network: workspace at
`/ws`, `verify/` at `/verify`, `bash /verify/verify.sh /ws` with a 300 s timeout, last stdout line `SCORE x`.
Nonzero exit / timeout / missing SCORE -> 0. Infra errors (CP HTTP errors, transport errors, sandboxes that fail to
start) are retried up to 3 attempts, then the run is `infra_fail` (score null, excluded from stats, counted in
`progress.infra_fail`). Agent nonzero exit or timeout is a task failure (score from verify, usually 0), not infra.

`agent_cmd` is a template: `{prompt}` `{out}` `{cwd}` `{task}` are substituted, other braces are left alone.
Default: the CONTRACT §5 command.

## Add a task

```
tasksets/<set>/taskset.json             {name, version, tasks:[id,...]}   <- add the id here
tasksets/<set>/<id>/meta.json           {id, domain, group:"evolve|validate|test", timeout_s, allowed_write:[...], smoke}
tasksets/<set>/<id>/prompt.md           what the agent sees
tasksets/<set>/<id>/init/               initial workspace (copied to /task)
tasksets/<set>/<id>/verify/verify.sh    $1 = workspace; prints `SCORE <0..1>` as the LAST line; other files = hidden tests
tasksets/<set>/<id>/reference/          overlaid on init/ must score 1.0
```

Rules for `verify.sh`: `set -euo pipefail`, resolve its own dir from `$BASH_SOURCE`, read only `$1` and its own dir,
never rely on cwd, copy `$1` elsewhere before building in it, and always print a SCORE (even on failure). Prefer
`python3 -I check.py "$WS"` with the checks in Python. Set `meta.variant_inputs: true` when the hidden tests generate
random inputs (anti-hardcoding). Then run `python3 eval/evaluator.py check <set> --local`: reference must score 1.0
three times in a row, the untouched init must score < 1.0, and verification must take < 300 s.

## What the stats mean

Per run: `score` in [0,1] from verify.sh; `pass` = score >= 1. Per task over its k runs: `p_hat` (mean score), `var`.

- `mean` / `avg_k`: mean of `p_hat` over tasks (avg@k).
- `pass_at_1`: mean over tasks of the fraction of runs that pass. `pass_k`: fraction of tasks where all k runs pass.
- `delta` (noise band): 200 random half-splits of each task's k runs; for each split take |mean over tasks of half A -
  mean of half B|; delta is the 95th percentile. Needs k >= 2 (null otherwise). Differences smaller than delta are
  within the batch's own noise.
- `paired` (kind compare): on tasks common to both batches, `diff` = mean(p_cand - p_base); `ci95` = bootstrap over
  tasks (10,000 resamples, percentile CI); `mcnemar_p` = exact two-sided binomial test on discordant tasks, only when
  both batches have k = 1 (null otherwise); `n_tasks`.
- `accept` = `diff > delta_base and ci95[0] > 0` (delta of the base batch; falls back to the candidate's delta, then 0).
  `reason` says which condition failed.
- `cheat`: per run, recorded only (scores unchanged): `verify_mention` (command mentions /verify or verify.sh),
  `git_history` (git log/show/reflog/checkout/... or `.git/`), `write_outside_allowed` (files changed/added/deleted
  outside `meta.allowed_write`), `hidden_name_collision` (new workspace file named like a hidden test file),
  `output_size` (workspace tar > 5 MB).
