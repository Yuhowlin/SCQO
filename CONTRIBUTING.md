# Contributing to the scqo stack

This is the hub for all four repos. The siblings link here rather than keeping their
own copy, because a release is a **combo** across them and three drifting copies would
disagree within one cycle.

| repo | what it is |
|---|---|
| [SCQO](https://github.com/shiau109/SCQO) | the vendor-neutral core: `Experiment` / `Parameters` / `Session`. Depends on no instrument library, ever. |
| [scqat](https://github.com/shiau109/scqat) | analysis: estimators + fitters. The base of the import arrow. |
| [scqo-qblox](https://github.com/shiau109/scqo-qblox) | the Qblox backend (`qblox_scheduler`) |
| [scqo-qm](https://github.com/shiau109/scqo-qm) | the Quantum Machines backend (qm-qua / QUAM / qualibrate) |

A working install is always a combo: **SCQO + scqat + one driver**.

---

## First: which lane are you in?

### Lane A — you want to change the code and send it back

That is this document. Start at *Layout*.

### Lane B — you run your own chip and want your own experiments

**You probably do not need a fork at all.** Nothing about your device lives in these
repos. Your roster (`components.toml`), your datasheet (`design.toml`), your vendor
config and all your data live under your `data_root`, described in
[INSTALL.md](INSTALL.md). Install the released combo, write those files, and you are
running your own hardware with an unmodified stack.

Fork only when you must change code that ships:

- **QM**: lab-specific QUAM classes in `scqo-qm/quam_builder/` and `scqo_qm/components/`.
- **Qblox**: the vendored element types in `scqo-qblox/scqo_qblox/elements.py`.

If you do fork for that reason, know one trap up front: those classes are persisted
**by dotted path** inside your saved `state.json`. Renaming the package or moving a
class strands every state file you have already written, and recovering needs a
migration script (`scqo-qm/scripts/migrate_state_scqo_qm.py` is the worked example).
Keep the module paths stable.

Track upstream by **combo tag**, not by `main`. [RELEASES.toml](RELEASES.toml) records
which tag of each repo belongs together, plus the required upgrade action for that
release. And know the house rule you are inheriting: **there are no shims, aliases or
migrations.** Breaking changes ship as a clean cutover with an upgrade note. Read the
notes line of each release before taking it.

---

## Layout: the folder names *are* the dependency graph

`SCQO/pyproject.toml` resolves scqat as `{ path = "../scqat", editable = true }`, and
each driver resolves scqo as `{ path = "../SCQO" }`. So:

> **A standalone fork of one driver cannot install.** It needs `../SCQO` next to it.
> This surprises people; it is not a bug.

Fork what you will change, then clone every repo the change touches as **siblings under
one parent, under their own names**:

```
<parent>/
  SCQO/
  scqat/
  scqo-qblox/     # only if you touch the Qblox backend
  scqo-qm/        # only if you touch the QM backend
```

Do not rename the folders and do not nest them. Then:

```bash
cd <parent>
uv venv .venv --python 3.12
uv pip install --python .venv/bin/python -e "./SCQO[viewer]" -e ./scqat pytest httpx
```

On Windows the interpreter is `.venv\Scripts\python.exe`. Full environment detail,
including the driver venvs, is in [INSTALL.md](INSTALL.md) §1.

**Editable installs freeze their recorded version at install time.** After switching
branches or pulling, re-run the install line — otherwise `scqo doctor` and
`importlib.metadata.version` keep reporting the old number while the code is new. This
is the single most common source of "impossible" behaviour in this stack.

---

## Which repos does your change touch?

| you are changing | repos |
|---|---|
| a fitter or an estimator | scqat |
| an experiment's physics, Parameters, or writeback | SCQO (+ scqat if the estimator moves) |
| how an experiment runs on hardware (`probe()`) | the driver, + SCQO if the neutral surface changes |
| a new experiment, end to end | scqat → SCQO → both drivers (a new experiment always means a new estimator — reusing an existing one is a merge or a `tools/` reduction instead, see SCQO's *The estimator binding*) |

---

## Branch and pull request

**1. Branch, and use the same branch name in every repo you touch.**

```bash
git checkout -b feature/<slug>
```

That shared name is not cosmetic: SCQO's CI uses it to find the matching branch in your
scqat fork. Without it, CI tests your SCQO change against *upstream* scqat and the
green tick means nothing.

**2. One feature is several pull requests.** There are no compatibility shims here, so a
change crossing repos cannot be atomic. Open one PR per repo, cross-link them in each
body, and expect this merge order:

> **scqat → SCQO → scqo-qblox / scqo-qm**

Your SCQO PR will be **red until the scqat PR merges**. That is expected. Do not
work around it by vendoring code or pinning a commit.

**3. Say what you actually verified.** Every PR records one of:

- `offline` — the test suites pass
- `hardware <chip> <date>` — it ran on real hardware
- `unverified` — you changed it but could not run it

An honest `unverified` is useful. A vague claim is not. This string goes straight into
the release ledger, so it has to be true.

---

## What CI proves, and what you must paste

| repo | CI | what you paste in the PR |
|---|---|---|
| SCQO | full suite on Linux, macOS, Windows | the targeted command you ran |
| scqat | own suite + SCQO's against your scqat | the targeted command you ran |
| scqo-qblox | offline suite | the full-suite result |
| scqo-qm | **none** — git-sourced deps and a pinned py3.11 lockfile make it impractical | the full-suite output, and which venv produced it |

Each repo's `CLAUDE.md` has its testing-discipline section: SCQO has a selection map
(run the targeted subset, full suite only for shared-core edits), while the drivers and
scqat are small enough that the full suite *is* the targeted run.

**Report the exact command you ran.** Never describe a targeted run as though it proved
the whole suite.

### What you cannot prove

You can run every offline suite, and you can run `scripts/check_real_config.py` in
either driver against your own lab's vendor config files — it works on a temporary copy
and never writes to your originals. You cannot validate against the maintainer's chips.
Say so; that is what `unverified` is for.

---

## Definition of done

- [ ] Targeted tests pass, and the exact command is in the PR body.
- [ ] Sibling PRs cross-linked, with the merge order stated.
- [ ] `validated =` line: `offline`, `hardware <chip> <date>`, or `unverified`.
- [ ] New experiment? Work the promotion checklist in SCQO's
      [CLAUDE.md](CLAUDE.md) (*Experiment governance*) — contract declared, `simulate()`
      implemented, an estimator in scqat bound by THIS experiment only, `update()` writing
      only catalogued fields, a catalog-quality `description`.
- [ ] New experiment? `python scripts/update_docs.py` re-run in SCQO, so both generated
      blocks in `CLAUDE.md` — the census and the estimator map — include it.
      `tests/test_docs_current.py` fails otherwise. scqat has the same script for its
      derived tables.
- [ ] A `RELEASES.d/<slug>.toml` fragment **drafted in the PR body** — format in
      [RELEASES.d/README.md](RELEASES.d/README.md). Do not commit it: the ledger may
      only list complete features, so the maintainer commits it once the *last* repo's
      PR has landed.

---

## House rules worth knowing before you write code

These are not style preferences; breaking them is what gets a PR sent back.

- **No backward compatibility.** No shims, no aliases, no migration layers. A breaking
  change ships as one clean cutover with an upgrade note in `RELEASES.toml`. The only
  exempt thing is already-recorded run data, which is immutable.
- **SCQO depends on no instrument library.** That is what lets the two drivers coexist
  without dragging each other's vendor stack in.
- **scqat's import arrow points one way**: `workflows → estimators → tools`, and
  `tools` imports none of them. Estimators never call estimators.
- **`scqo run <name>` is the single entry point.** No wrappers, no launcher stubs, no
  per-command shims.
- **A knob needs a real vendor home.** A new catalogued field needs an abstract view
  property *and* a fieldmap entry in both drivers, or it is unsettable and invisible.

Each repo's `AGENTS.md` is the short version of this document, scoped to that repo — and
it is what a coding agent reads. If you work with one, point it there.
