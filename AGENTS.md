# AGENTS.md — SCQO

**You are reading the contributor brief.** If you are working in the maintainer's lab
tree, stop and read [CLAUDE.md](CLAUDE.md) instead — that is the lab-operations
document, and two of its rules are the *opposite* of the ones below.

## What this repo is

SCQO is the vendor-neutral core: `Experiment` / `Parameters` / `Session`. It depends on
**scqat** (estimators + fitters) and is depended on by two backends, **scqo-qblox** and
**scqo-qm**. A working install is always a combo:

- Qblox: `SCQO` + `scqo-qblox` + `scqat`
- QM: `SCQO` + `scqo-qm` + `scqat`

SCQO depends on **no instrument library**. Never add one.

## Layout: folder names are the dependency graph

`pyproject.toml` resolves scqat as `{ path = "../scqat", editable = true }`, and each
backend resolves scqo as `{ path = "../SCQO" }`. Clone every repo you touch as a
**sibling** under one parent and **do not rename the folders**:

```
<parent>/
  SCQO/
  scqat/
  scqo-qblox/     # only if you touch the Qblox backend
  scqo-qm/        # only if you touch the QM backend
```

## Setup

```bash
cd SCQO
uv run --extra viewer pytest -q     # builds SCQO/.venv from uv.lock on first use
```

That is the whole setup. `[tool.uv.sources]` resolves `scqat` as `{ path = "../scqat",
editable = true }`, so `uv run` reproduces the sibling editable **from the tracked
lockfile** — but the clone layout above is still a real prerequisite: without `../scqat`
present, uv fails with a resolution error naming the missing path.

Which environment for which repo — the answers legitimately differ, and scqo-qm forbids
`uv run` outright: [ENVIRONMENTS.md](ENVIRONMENTS.md).

**Editable installs freeze their recorded version at install time** — but only the
hand-built kind (`uv pip install -e`, the lab's shared `.venv-*`). After changing branches
or pulling, re-run those install lines, or the metadata reports the old version while the
code is new and you will debug a tree that isn't there. `uv run` re-syncs every invocation
and does not have this problem.

## Branch and PR rules — these OVERRIDE CLAUDE.md

`CLAUDE.md` tells maintainers never to switch branches and to commit by explicit
pathspec. That protects a shared, live checkout with editable installs and sometimes a
running hardware session. **In your fork none of that applies.** Instead:

1. Work on `feature/<slug>`. Never commit to `main`.
2. **Use the same branch name in every repo the change touches.** CI uses it to pair
   your SCQO branch with your scqat branch; without it, CI tests your change against
   upstream scqat and the result is meaningless.
3. **One feature is several PRs.** No shims or migrations ship here, so a cross-repo
   change cannot be atomic. One PR per repo, cross-linked, merging in the order
   **scqat → SCQO → drivers**. Your SCQO PR is **red until the scqat PR merges** — that
   is expected; do not vendor or pin around it.

Full detail: [CONTRIBUTING.md](CONTRIBUTING.md).

## Testing

Default to the targeted run; the selection map is in [CLAUDE.md](CLAUDE.md) under
*Testing discipline*:

```bash
uv run pytest tests/test_model_experiments.py -k <stem> -q
```

Run it from the repo root: `uv run` builds and keeps `SCQO/.venv` from `uv.lock`, and that
env is DRIVER-FREE — which this suite requires. Which environment for which repo, and why
the answers differ: [ENVIRONMENTS.md](ENVIRONMENTS.md).

`-k` takes the distinctive **stem**, not the registered name. **0 collected means the
filter was wrong** — widen it, never skip.

Run the **full suite** only for a release or a shared-core edit: `catalog.py`,
`entities.py`, `roster.py`, `stores.py`, `device.py`, `experiment.py`, `session.py`, or
a `_capabilities/` mixin — as `uv run --extra viewer pytest -q`, and expect ZERO skips
(without the extra it silently collects 917 of 984).

**Always report the exact command you ran.** Never describe a targeted run as though it
proved the whole suite.

## Adding an experiment

1. Subclass the backend-free experiment in `scqo/experiments/`; `@register` it.
2. A driver implements only `probe()`. Parameters, Result, `estimate`, `simulate` and
   `update` are inherited.
3. Run `python scripts/update_docs.py` so both generated blocks in `CLAUDE.md` — the
   census and the estimator map — include it; `tests/test_docs_current.py` fails otherwise.
4. Work the promotion checklist in [CLAUDE.md](CLAUDE.md) (*Experiment governance*).

## What you can and cannot verify

You **can** prove offline: the test suites, and `scripts/check_real_config.py` in either
driver against your own lab's vendor config (it works on a temporary copy and never
writes to your originals).

You **cannot** prove anything about the maintainer's hardware. Say so. Every PR records
`offline`, `hardware <chip> <date>`, or `unverified` — an honest `unverified` is useful,
a vague claim is not.

## Read these sections of CLAUDE.md before writing code

They transfer to a fork unchanged, and getting them wrong is the most common review
failure:

| Topic | Why it matters |
|---|---|
| **Terminology** | `Experiment` = probe + **exactly one** estimator, and an estimator is bound by exactly one experiment; "protocol" is retired. Consistent across all four repos. |
| **The estimator binding** | Why that 1:1 is a claim about MODELS, not filing — plus the decision procedure for whether two candidate experiments are really one. Share math through `scqat.tools`, never a second binding. |
| **The placement rule** | Decides whether a quantity is a per-run parameter, a `fact`, a `knob` or a `monitor` — i.e. which store owns it. Full text: TUTORIAL §10. |
| **The readout schema** | `state` is always a per-shot integer level, `population` an averaged marginal, `joint_population` the multi-qubit joint. Never blur them. Full text: TUTORIAL §11. |
| **How a driver adds an experiment** | Subclass, implement only `probe()`, `@register`. |

## Do not

- Do not add an instrument library to SCQO's dependencies.
- Do not add shims, aliases or compatibility layers. Breaking changes ship as a clean
  cutover with an upgrade note — a deliberate house rule, not an oversight.
- Do not add a CLI wrapper or launcher. `scqo run <name>` is the single entry point.
- Do not hand-maintain a list that can be derived. Two lists in `CLAUDE.md` are
  generated (`scripts/update_docs.py`) precisely because the hand-kept versions rotted.
- Do not use absolute paths from `CLAUDE.md` or `INSTALL.md` (`D:\...`) — they describe
  the lab machine, not yours.
