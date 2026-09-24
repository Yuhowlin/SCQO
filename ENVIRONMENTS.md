# Which Python environment — the one authority for all four repos

Four environments live on a lab machine and they are **not** interchangeable. Substituting one
for another is silent in three of the four directions: the suite passes, and it tested something
else.

This file is the single source of truth. The siblings (`scqat`, `scqo-qblox`, `scqo-qm`) carry a
short stub pointing here rather than their own copy, for the reason
[CONTRIBUTING.md](CONTRIBUTING.md) already gives about itself — a release is a combo across the
repos, and separate copies disagree within one cycle.

## The rule

**The environment is a property of the REPO whose tests you are running, not of the machine you
are on.** Each repo has exactly one canonical test command; the table below is the whole answer.
Every *other* environment exists to run **experiments**, not tests.

Two families, both load-bearing, neither mergeable into the other:

- **Repo-local `<repo>/.venv`** — uv's project environment, created and kept in step by `uv run`
  from that repo's tracked `uv.lock`. This is where **tests** run for SCQO, scqat and
  scqo-qblox. It is disposable: delete it and the next `uv run` rebuilds it.
- **Shared `<parent>/.venv-view`, `.venv-qblox`, `.venv-qm`** — hand-built per
  [INSTALL.md](INSTALL.md) §1 with `uv venv` + `uv pip install -e`. This is where
  **measurements** run (`scqo run`, `scqo state`), and for **scqo-qm it is also where tests run**,
  because that repo's pin authority is `requirements-qm.lock.txt`, not `pyproject.toml`.

There is **no `<parent>/.venv`**. If a document tells you to create one, it is stale — say so.

## One vendor version, every environment

A vendor library must be at the **same version in every environment that can compile a schedule
or a program**, repo-local and shared alike.

On **2026-07-26** `scqo-qblox/.venv` held `qblox-scheduler` **b4** while the lab's `.venv-qblox`
held **b6**. The two versions *disagree about whether a schedule is legal*: `readout_frequency`
compiled clean offline and **died on the hardware**. Both are `1.0.0b6` now (with
`qblox_instruments` 1.3.0), pinned in three places that move together — scqo-qblox's
`pyproject.toml` floor, its `uv.lock`, and the
`uv pip install "qblox-scheduler==1.0.0b6" "qblox-instruments==1.3.0"` line in INSTALL.md §1.

**Pin both halves of the pair, not just the scheduler.** Until 2026-09-20 that INSTALL.md line
named only `qblox-scheduler`; a rebuild that day resolved `qblox-instruments` to **1.3.1**,
released after the pair was proven. The suite passed at 1.3.1 in both environments — the
mandatory second run does **not** catch this, because both environments compile fine, they just
compile against different vendor code than the cluster's firmware was proven with. Only an
explicit version comparison catches it.

The consequence is scqo-qblox's **mandatory second test run** (see the table). Compiling in one
environment proves nothing about the other. Run the versions side by side as well:

```bash
<parent>/.venv-qblox/Scripts/python.exe -c "from importlib.metadata import version as V; print(V('qblox-scheduler'), V('qblox-instruments'))"
scqo-qblox/.venv/Scripts/python.exe     -c "from importlib.metadata import version as V; print(V('qblox-scheduler'), V('qblox-instruments'))"
```

The QM side has the same shape with a different mechanism: its stack is pinned by
`scqo-qm/requirements-qm.lock.txt` at Python 3.11, and there is only ONE environment for it —
which is exactly why `uv run` is forbidden there. It would create a second one, resolved from a
different file.

## Per repo

| repo | run the tests with | which env that is | why not another |
|---|---|---|---|
| **SCQO** | targeted: `uv run pytest tests/test_model_experiments.py -k <stem> -q`<br>full: `uv run --extra viewer pytest -q` | `SCQO/.venv` — uv's project env from `SCQO/uv.lock`: `scqo` + `scqat` editable from `../scqat`, `pytest`, `httpx`. **No driver.** | The suite must be **driver-free**. `experiments/__init__.py` keeps the LAST registration (`_REGISTRY[cls.name] = cls`), so a driver's `@register`ed subclass replaces the core class under the same name: in `.venv-qm` / `.venv-qblox`, `session.run("single_shot_readout", …)` exercises the *driver's* `update()` and passes. `tests/test_cli_backends.py` already `skipif`s a test when a driver is present. |
| **scqat** | `uv run --extra dev pytest tests/test_<name>_estimator.py -q` | `scqat/.venv` | **`--extra dev` is mandatory** — `pytest` is in `[project.optional-dependencies]`, not `[dependency-groups]`, and uv installs only the latter by default; bare `uv run pytest` dies with `Failed to spawn: pytest`. scqat needs no sibling to test itself: it is the base of the import arrow. |
| **scqo-qblox** | `uv run pytest tests/ -q`<br>**then** `<parent>/.venv-qblox/Scripts/python.exe -m pytest tests/ -q` | `scqo-qblox/.venv`, then the shared `.venv-qblox` | Plain `uv run` is right here — `scqo` is a hard dependency so uv's sync keeps it, and `uv.lock` carries `qblox-scheduler==1.0.0b6` plus `scqat` editable from `../scqat`. The **second** run is the vendor-version rule above. It is not a formality: it is the check 2026-07-26 did not have. |
| **scqo-qm** | `<parent>/.venv-qm/Scripts/python.exe -m pytest tests/ -q` | the shared `.venv-qm` (Python **3.11**, from `requirements-qm.lock.txt` + editable `scqat` / `SCQO` / `scqo-qm`) | **`uv run` is forbidden here.** Its sync rebuilds the env from `pyproject.toml` + `uv.lock`, displacing `requirements-qm.lock.txt` — the pin authority for the whole `qm-qua → quam → qualibrate` stack, which is why that lockfile exists. **There is no repo-local venv for this repo**; a `scqo-qm/.venv` on disk is residue of a stray `uv run` and cannot run the suite (it holds no `qm`, no `quam`, no `qualibrate`, no `scqat`). `uv run --no-sync` is meaningful only with `UV_PROJECT_ENVIRONMENT` pointed at `.venv-qm`; calling the interpreter by path is unambiguous. |

**`0 collected`, or ANY skip, is an alarm — not a pass.** A correctly configured SCQO full run
reports **zero skipped** (no count is quoted here on purpose — see CLAUDE.md on how test counts
rot). Without `--extra viewer` the run is silently 67 tests short: the viewer stack is a root
*extra*, `uv run` installs only the default dependency-group, and `tests/test_viewer.py`'s
module-level `pytest.importorskip("fastapi")` takes 54 tests with it while
`tests/test_lab_report.py`'s gates take 13 more. Measured 2026-09-04 in a clean project env
(917 against 984); a machine that once ran `uv pip install -e ".[viewer]"` has them lying around
and will NOT reproduce it, which is exactly what kept this hidden.

## The shared environments (operators)

Three of them, named by role, each with its own shell prompt. **One rule: activate `view` for
everything except actually running a measurement.** Contents, prompts, and the build commands are
in [INSTALL.md](INSTALL.md) §1 — not repeated here, so they cannot drift. Students never run
tests.

## CI is a fourth arrangement, deliberately

SCQO's, scqat's and scqo-qblox's workflows install with bare `pip install -e` into
`actions/setup-python` — no uv, no lockfile. On purpose: CI proves the **sibling layout installs
from a cold registry**, which a lockfile would hide. It is not a recipe to copy locally, and a
green CI does not prove your local environment is right.

`scqo-qm` has **no CI at all** — its git-sourced dependencies and pinned 3.11 lockfile make it
impractical — so a pasted local result is the only evidence a reviewer gets. Always say which
interpreter produced it.

## Editable installs freeze — but only the hand-built kind

`uv pip install -e` records the version **at install time**, so after a checkout or a pull the
metadata describes a tree that is no longer there. That is the single most common source of
"impossible" behaviour in this stack, and the fix is to re-run the §1 lines for `.venv-view` /
`.venv-qblox` / `.venv-qm`.

**`uv run` does not have this problem** — it re-syncs from the lockfile on every invocation.
Conflating the two families on this point is one of the confusions this file exists to end.
Check either with `python scripts/check_contribution.py` (section 2), which compares the tree's
version against the installed metadata and prints the interpreter that answered.

## Python versions

All four repos target **`>=3.10,<3.13`**. The lab standard is **3.12.13**, except `.venv-qm` at
**3.11.15**, because `requirements-qm.lock.txt` is a 3.11 lockfile. CI covers 3.12 and 3.10.
Nothing in the stack is tested above 3.12.
