# SCQO installation & verification

One-time setup per machine: build the Python environment, write the lab config, and
verify the stack — first offline, then against your instrument's real config files.
Done once (usually by the lab manager); students then follow [TUTORIAL.md](TUTORIAL.md).

The stack is cross-platform: the full test suite runs on **Windows, macOS and Linux**
in CI on every push (`.github/workflows/tests.yml`). Windows commands are shown first;
macOS/Linux equivalents follow where they differ.

## 1. The Python environments

**Policy: every environment is a plain venv managed by `uv`.** Conda is retired: an
audit (2026-07-05) showed the lab's conda envs used conda only as a Python installer
(all 180+ scientific/vendor packages came from pip) — uv does that job faster, with
lockfiles, and without licensing questions.

Three environments, named by **role**, each with its own shell prompt so you always
see which one is active. **The one rule: activate `view` for everything except
actually running a measurement.**

| venv | prompt | contents | activate when you… |
|---|---|---|---|
| `D:\github\.venv-view` | `(view)` | scqo `[viewer]` + scqat + datasette + pytest — **no instrument libraries** | look at data (the common case): run-viewer, SQL browser, `find_runs.py`, `tag_run.py`. Works identically on an analysis-only laptop/Mac. |
| `D:\github\.venv-qblox` | `(qblox)` | the view stack + LCHQBDriver + `qblox-scheduler==1.0.0b4` (hardware-proven) + scqo-contrib | measure on the Qblox cluster: `run_experiment.py`, `calibrate.py`, `device.py` |
| `D:\github\.venv-qm` | `(.venv-qm)` | pinned QM stack, py3.11 (`LCHQMDriver\requirements-qm.lock.txt`) + scqo/scqat/LCHQMDriver editables | measure on the OPX1000 or use qualibrate — `qm.bat` activates it for you |

All three import scqo/scqat from the same editable checkouts, so they never drift on
the neutral layer. `uv` creates standard venvs and downloads Python itself if the
machine has none.

The repos must sit next to each other in one folder (`SCQO` and `SCqubit-analysis-tool`
as siblings) — on the lab PC that folder is `D:\github`; on your own Mac clone them:

```bash
mkdir -p ~/github && cd ~/github
git clone https://github.com/shiau109/SCQO.git
git clone https://github.com/shiau109/SCqubit-analysis-tool.git
git clone https://github.com/shiau109/LCHQBDriver.git    # only if this machine will measure
git clone https://github.com/shiau109/scqo-contrib.git   # optional: the Tier-2 sandbox
```

**Windows (PowerShell)** — on the lab PC all three envs already exist under `D:\github`:

```powershell
cd D:\github

# view — data browsing, no instrument (the daily default)
uv venv .venv-view --python 3.12 --prompt view
uv pip install --python .venv-view\Scripts\python.exe -e ".\SCQO[viewer]" -e .\SCqubit-analysis-tool datasette pytest httpx

# qblox — measurement env for the Qblox cluster
uv venv .venv-qblox --python 3.12 --prompt qblox
uv pip install --python .venv-qblox\Scripts\python.exe -e ".\SCQO[viewer]" -e .\SCqubit-analysis-tool -e .\LCHQBDriver -e .\scqo-contrib datasette pytest httpx
uv pip install --python .venv-qblox\Scripts\python.exe "qblox-scheduler==1.0.0b4"   # exact hardware-proven build (see note)

# qm — measurement env for the OPX1000 (pinned, py3.11)
uv venv .venv-qm --python 3.11
uv pip install --python .venv-qm\Scripts\python.exe -r .\LCHQMDriver\requirements-qm.lock.txt
uv pip install --python .venv-qm\Scripts\python.exe -e .\SCqubit-analysis-tool -e .\SCQO -e .\LCHQMDriver --no-deps

.venv-view\Scripts\Activate.ps1     # daily default — prompt shows (view)
```

(`[viewer]` pulls the run-viewer's web extras — fastapi/uvicorn/jinja2/python-multipart —
for `python -m scqo.viewer`; `datasette` powers the SQL browser `python -m scqo.browse`.
**qblox-scheduler pin:** LCHQBDriver's pyproject floors it at `>=1.0.0b4` because PyPI's
only non-prerelease is an empty 0.0.0 placeholder that fails to build; the explicit
`==1.0.0b4` line then holds the env at the exact build proven on the cluster — bump it
deliberately after re-validation, never by accidental rebuild.)

**macOS / Linux** — install uv once with `brew install uv` (or
`curl -LsSf https://astral.sh/uv/install.sh | sh`). An analysis-only machine needs
**just the view env**:

```bash
cd ~/github
uv venv .venv-view --python 3.12 --prompt view
uv pip install --python .venv-view/bin/python -e "./SCQO[viewer]" -e ./SCqubit-analysis-tool datasette pytest httpx
source .venv-view/bin/activate
```

(Add the qblox env — same lines as Windows with `/bin/python` paths — only if the
machine actually drives a cluster; finding/loading/viewing saved data never needs it.)

## 2. The lab config: `~\.scqo\config.toml`

This one small file tells every script where data goes, which device you are on,
and which backend runs. Create it at `~\.scqo\config.toml` (Windows:
`C:\Users\<you>\.scqo\config.toml`; macOS: `/Users/<you>/.scqo/config.toml`).

**Backend modes** — pick how real the setup is:

| `backend =` | device tree | data | writebacks persist to |
|---|---|---|---|
| `"simulated"` | built-in demo qubits | synthetic | scqo state file (use `state_sync="push"`) |
| `"qblox_sim"` | **your REAL dut config** (working copy) | synthetic | the working `dut_config.json` |
| `"qm_sim"` | **your REAL QUAM state** (working copy) | synthetic | the working `state.json` |
| `"qblox"` / `"qm"` | real instrument | real | vendor config (QM: keep `state_sync="pull"`) |

The `*_sim` modes are the **virtual twin**: real qubit names and calibration values as
starting points, no hardware needed — the recommended practice mode for students. Set
them up once by copying your vendor config into a working folder (originals stay
pristine), e.g. `copy dut_config_AS_QRC.json D:\qpu_data\SQ_demo\qblox_state\dut_config.json`.

Windows (virtual-twin example):

```toml
[lab]
data_root   = 'D:\qpu_data'                          # all measurement data lands here
device_name = "SQ_demo"                              # the SAMPLE (chip) name — never the instrument
state_path  = 'D:\qpu_data\SQ_demo\scqo_state.json'  # change history (provenance)
backend     = "qblox_sim"                            # REAL device tree, synthetic data
default_tags = ["cooldown1"]                         # stamped on EVERY run; edit each cooldown

[qblox]
config_dir = 'D:\qpu_data\SQ_demo\qblox_state'       # working copy of dut_config.json (+ hw_config.json for "qblox")

# QM virtual twin instead: backend = "qm_sim" plus
# [qm]
# state_dir = 'D:\qpu_data\SQ_demo\qm_state'         # working copy of state.json + wiring.json
```

**Two instruments, two samples, one PC:** each vendor table may override
`device_name`/`state_path` with the sample mounted on *that* instrument — switching
`backend` then switches the sample automatically, so runs can never land under the
wrong device:

```toml
[qblox]
config_dir  = 'D:\qpu_data\chipA\qblox_state'
device_name = "chipA"
state_path  = 'D:\qpu_data\chipA\scqo_state.json'

[qm]
state_dir   = 'D:\qpu_data\chipB\qm_state'
device_name = "chipB"
state_path  = 'D:\qpu_data\chipB\scqo_state.json'
```

Optionally describe each sample in `<data_root>\devices.toml` (one table per chip:
description, design values, where it is mounted — instrument-independent facts only);
the viewer's Device page shows the matching card. All samples share ONE `data_root`
and ONE index — filter with `--device` / the viewer's device dropdown.

macOS / Linux (`~` is expanded for you; plain-simulated example):

```toml
[lab]
data_root   = "~/qpu_data"
device_name = "SQ_demo"
state_path  = "~/qpu_data/SQ_demo/scqo_state.json"
backend     = "simulated"
state_sync  = "push"
default_tags = ["cooldown1"]
```

State persistence: in the `*_sim` twin modes the working vendor config **is** the
device state — it updates on every successful run, so calibrations persist across
invocations with the default `state_sync = "pull"`. Only the plain `"simulated"` demo
needs `state_sync = "push"` to persist, since its device is created fresh in memory
each time. On QM control PCs `"pull"` is mandatory — see LCHQMDriver's CLAUDE.md.

Other notes:
- A temporary alternative config can be selected per shell
  (PowerShell: `$env:SCQO_CONFIG = "path\to\other.toml"`; bash/zsh:
  `export SCQO_CONFIG=path/to/other.toml`) or per command with `--config`.
- A mistyped `$SCQO_CONFIG` **fails loudly** — it will not silently run unsaved.

## 3. Offline verification

The full test suite passes with no instrument attached (CI runs this exact suite on
Windows, macOS and Linux):

```bash
cd SCQO
python -m pytest -q        # expect: all passed (any env works; view is enough)
```

## 4. Self-test against your REAL device config (no hardware needed)

Before ever touching an instrument, verify the whole stack against your lab's actual
config files: each driver has a `check_real_config.py` that loads them, runs the full
pipeline with **simulated data over the real device tree** (read neutral fields → run
experiments → fit → write back → save in vendor format → reload and compare), and
prints PASS/FAIL. It works on a **temporary copy** — your originals are never opened
for writing, and nothing lands in your real data_root.

**Qblox** — needs the qblox env (`.venv-qblox\Scripts\Activate.ps1`). Point it at any
folder holding `dut_config*.json` + `hw_config*.json`:

```powershell
cd D:\github\LCHQBDriver
python scripts\check_real_config.py D:\qpu_data\SQ_demo\QBLOX_config
```

**QM / OPX1000** — needs the qm env (`.venv-qm\Scripts\Activate.ps1`). Point it at
any folder holding `state.json` + `wiring.json`:

```powershell
cd D:\github\LCHQMDriver
python scripts\check_real_config.py D:\qpu_data\SQ_demo\QM_OPX1000_config
```

Expected output: 5 numbered steps, each OK, ending in
`PASS - scqo works against this real config`. A qubit whose state is uncalibrated
(fields `None`) is skipped automatically; on the Qblox device the coupler (`c12`) is
excluded by the `q*` default — pass `--qubits` to choose explicitly. Both lab configs
passed on 2026-07-04 (and this test caught three real integration bugs before any
hardware time was spent — that's its job).

## 5. Install troubleshooting

| Symptom | Cause / fix |
|---|---|
| `ModuleNotFoundError: scqo` | no venv activated — Windows: `.venv-view\Scripts\Activate.ps1`; macOS/Linux: `source .venv-view/bin/activate` |
| viewer: `missing package: uvicorn` (or fastapi/jinja2) | **wrong venv activated** — the viewer lives in every section-1 env; check the prompt says `(view)`, `(qblox)` or `(.venv-qm)`, not something stale |
| `ModuleNotFoundError: lchqb` / `qblox_scheduler` from a run script | you're in the view env (by design it has no instrument libs) — activate `.venv-qblox` to measure |
| `lab config not found` | your `--config`/`$SCQO_CONFIG` path is wrong (intentional loud failure — better than silently unsaved) |
| `# lab config: built-in defaults ...` in the catalog header | no `~\.scqo\config.toml` yet: runs work but are **not saved** — do section 2 |
| self-test: `missing package: qblox_scheduler` | install the driver into this env (section 1, second install line) or use the lab conda env |

Setup done → hand the machine to the student and point them at
[TUTORIAL.md](TUTORIAL.md).
