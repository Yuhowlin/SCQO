# SCQO tutorial — measure, calibrate, and find your data

This is the hands-on guide for the lab's measurement system. You will run experiments
by *physics name* (Ramsey, power Rabi, resonator spectroscopy), get fitted device
parameters back, and be able to find every dataset you ever took. You never touch
instrument code, and you never edit anything in the repos.

Everything below runs **offline on the simulated backend** — no hardware needed — and
the identical commands drive real hardware once the lab config says so.

The stack is cross-platform: the full test suite runs on **Windows, macOS and Linux**
in CI on every push (`.github/workflows/tests.yml`). Windows commands are shown first;
macOS/Linux equivalents follow where they differ.

## 1. The system in one picture

```
you (script / notebook / later: GUI or AI agent)
        │  experiment name + parameters (plain JSON)
        ▼
   scqo.Session ──── catalog() · run() · find_runs() · device_state() · history()
        │
   Experiment  = probe (instrument half)  +  estimator (analysis half, scqat)
        │
   Backend     = SimulatedBackend | Qblox (LCHQBDriver) | QM (LCHQMDriver)
        │
   DataStore   = every run saved to a folder + searchable SQLite index
```

- **You think in physics**: qubit names, spans, idle times, π-amplitudes.
- **The estimator** (scqat) fits the data and reports extracted quantities + a
  per-qubit `successful/failed` verdict.
- **The datastore** saves *every* run — raw data, parameters, fit result, device
  snapshots, and the fit figures — under one folder per run, and indexes it so you
  can ask "what T2* did q1 get this week?" without remembering any filename.

## 2. One-time setup (≈2 minutes)

### 2a. The Python environment

We use a plain **venv** (not conda: every dependency ships wheels for Windows *and*
macOS, so conda adds nothing here; conda stays only on instrument PCs where the vendor
stack was installed that way, e.g. the QM `LCHQM_test` env). `uv` creates a standard
venv and also downloads Python itself if the machine has none.

The repos must sit next to each other in one folder (`SCQO` and `SCqubit-analysis-tool`
as siblings) — on the lab PC that folder is `D:\github`; on your own Mac clone them:

```bash
mkdir -p ~/github && cd ~/github
git clone https://github.com/shiau109/SCQO.git
git clone https://github.com/shiau109/SCqubit-analysis-tool.git
git clone https://github.com/shiau109/LCHQBDriver.git
```

**Windows (PowerShell)** — on the lab PC this env already exists at `D:\github\.venv`:

```powershell
cd D:\github
uv venv .venv --python 3.12
uv pip install --python .venv\Scripts\python.exe -e .\SCqubit-analysis-tool -e .\SCQO pytest
.venv\Scripts\Activate.ps1          # activate (Git Bash: source .venv/Scripts/activate)
```

**macOS / Linux** — install uv once with `brew install uv` (or
`curl -LsSf https://astral.sh/uv/install.sh | sh`), then:

```bash
cd ~/github
uv venv .venv --python 3.12
uv pip install --python .venv/bin/python -e ./SCqubit-analysis-tool -e ./SCQO pytest
source .venv/bin/activate
```

Sanity check on any OS — the full test suite passes with no instrument attached
(CI runs this exact suite on Windows, macOS and Linux):

```bash
cd SCQO
python -m pytest -q        # expect: all passed
```

### 2b. Your lab config: `~\.scqo\config.toml`

This one small file tells every script where data goes, which device you are on,
and which backend runs. Create it at `~\.scqo\config.toml` (Windows:
`C:\Users\<you>\.scqo\config.toml`; macOS: `/Users/<you>/.scqo/config.toml`).

Windows:

```toml
[lab]
data_root   = 'D:\qpu_data'                          # all measurement data lands here
device_name = "SQ_demo"                              # your chip / sample name
state_path  = 'D:\qpu_data\SQ_demo\scqo_state.json'  # calibration state + change history
backend     = "simulated"                            # "qblox" / "qm" on a control PC
default_tags = ["cooldown1"]                         # stamped on EVERY run; edit each cooldown
```

macOS / Linux (`~` is expanded for you):

```toml
[lab]
data_root   = "~/qpu_data"
device_name = "SQ_demo"
state_path  = "~/qpu_data/SQ_demo/scqo_state.json"
backend     = "simulated"
default_tags = ["cooldown1"]
```

Notes:
- `default_tags` is the killer feature: set it once per cooldown and every run is
  automatically findable by cooldown, with nobody remembering to type it.
- A temporary alternative config can be selected per shell
  (PowerShell: `$env:SCQO_CONFIG = "path\to\other.toml"`; bash/zsh:
  `export SCQO_CONFIG=path/to/other.toml`) or per command with `--config`.
- A mistyped `$SCQO_CONFIG` **fails loudly** — it will not silently run unsaved.

## 3. Your first measurement

```bash
cd LCHQBDriver          # D:\github\LCHQBDriver on the lab PC, ~/github/LCHQBDriver on a Mac
python scripts/run_experiment.py                 # no arguments = show the menu
```

```
qubit_power_rabi        Sweep drive amplitude ... recalibrate pi_amp.
qubit_ramsey            Two pi/2 pulses ... correct drive_freq and report T2*.
resonator_spectroscopy  Sweep readout frequency ... updates readout_freq.
```

Run a Ramsey on q1, tagged so you can find it later:

```bash
python scripts/run_experiment.py qubit_ramsey --qubits q1 --set num_points=201 --tag mytest --note "first try"
```

You get the structured result as JSON — extracted physics, not raw traces:

```json
{
  "outcomes": { "q1": "successful" },
  "fit": { "q1": { "drive_freq": 4009827345.3, "detuning_error_hz": -172654.7,
                    "t2_star_s": 8.24e-06, "old_drive_freq": 4010000000.0 } },
  "error": null,
  "run_id": "20260704-153041-qubit_ramsey-01",
  "data_path": "D:\\qpu_data\\SQ_demo\\2026-07-04\\20260704-153041-qubit_ramsey-01"
}
```

Because the fit succeeded, `drive_freq` was **written back** to the device state
(with a history record linking it to this run). Useful variations:

```bash
python scripts/run_experiment.py qubit_power_rabi                 # all qubits, defaults
python scripts/run_experiment.py qubit_ramsey --no-update ...     # analyze only, no writeback
python scripts/run_experiment.py qubit_ramsey --params my.json    # parameters from a file
```

## 4. Finding your data (the whole point)

```bash
python scripts/find_runs.py                                   # latest runs, newest first
python scripts/find_runs.py --tag cooldown1                   # everything from this cooldown
python scripts/find_runs.py --experiment qubit_ramsey --qubit q1 --since 2026-07-01
python scripts/find_runs.py --outcome failed                  # what went wrong lately?
python scripts/find_runs.py --show 20260704-153041-qubit_ramsey-01   # one run, in full
```

```
20260704-153041-qubit_ramsey-01   successful  q1   cooldown1,mytest  SQ_demo/2026-07-04/20260704-153041-qubit_ramsey-01
```

- Dates in filters are **local lab time** and match the folder names; a bare date in
  `--until` includes that whole day.
- `find_runs` touches no instrument — it runs anywhere the data drive is mounted.
- Realized a week later that a run mattered? Tag it retroactively from Python:
  `sess.tag_run("20260704-...-01", add=["thesis-fig3"])`.

## 5. What's inside a run folder

```
<data_root>/SQ_demo/2026-07-04/20260704-153041-qubit_ramsey-01/
    record.json          run manifest (its absence = run was incomplete/crashed)
    dataset.nc           the raw I/Q dataset (xarray/netCDF, dims: qubit × sweep)
    parameters.json      exactly what you asked for
    result.json          outcomes + fitted quantities + error (if any)
    device_before.json   calibration state before ...
    device_after.json    ... and after the writeback
    analysis/q1/         per-qubit fit artifacts from scqat:
        ramsey_time_domain.png      ← your figure, already drawn
        ramsey_fft_spectrum.png
        ramsey_metadata.json        fit parameters, fit quality
        ramsey_plotdata.nc          arrays to redraw the figure without refitting
```

**The folder is the truth.** The SQLite index (`<data_root>/index.sqlite`) is only a
cache — if it is ever missing or stale, rebuild it losslessly:

```powershell
python -m scqo <data_root>
```

## 6. Working in Python / Jupyter

**Where do my notebooks/scripts live?** Anywhere OUTSIDE the governed repos — e.g. a
personal `lab-notebooks/` folder (make it your own git repo if you want history).
Because `scqo`/`scqat` are installed in the venv, imports work from any directory;
just select the venv as your interpreter/kernel (VS Code: pick
`.venv\Scripts\python.exe`; or `uv pip install --python <venv-python> jupyterlab
ipykernel`). If a notebook grows into a new *experiment* or *estimator*, it graduates
to the contrib sandbox (section 8) — never straight into SCQO or a driver repo.

**Analyzing saved data needs no backend at all** — this is what most notebooks are:

```python
from scqo import DataStore, load_lab_config

cfg = load_lab_config()
store = DataStore(cfg.data_root, device_name=cfg.device_name)

store.find_runs(experiment="qubit_ramsey", qubit="q1", tag="cooldown1")
run = store.load_run("20260704-153041-qubit_ramsey-01")  # record + params + figure paths
ds = store.open_dataset("20260704-153041-qubit_ramsey-01")
ds["I"].sel(qubit="q1").plot()
store.tag_run("20260704-153041-qubit_ramsey-01", add=["thesis-fig3"])
```

**Running measurements** from a notebook is the same Session the scripts use:

```python
from scqo import load_lab_config, make_session
from scqo.testing import InMemoryDevice, SimulatedBackend

cfg = load_lab_config()
backend = SimulatedBackend(InMemoryDevice({          # or QbloxBackend / QMBackend
    "q0": {"readout_freq": 5.95e9, "drive_freq": 3.87e9, "pi_amp": 0.20},
    "q1": {"readout_freq": 6.05e9, "drive_freq": 4.01e9, "pi_amp": 0.18},
}))
sess = make_session(backend, cfg)

result = sess.run("qubit_ramsey", {"qubits": ["q1"], "num_points": 201})
sess.find_runs(experiment="qubit_ramsey", qubit="q1")   # list of dicts, newest first
sess.load_run(result["run_id"])                          # record + params + figure paths

sess.device_state()   # current calibration of every qubit
sess.history()        # every change ever: who, what, old → new, which run caused it
```

## 7. When things fail (by design)

A failed fit or a bad probe **never crashes and never loses data**: you get
`"error": "..."`, the qubits are marked `failed`/`no_data`, nothing is written back
to the device — and the run (including the misbehaving dataset) is still saved and
searchable via `--outcome failed`, because failed data is exactly what you want to
look at when debugging. Even "measurement fine, instrument rejected the writeback"
comes back as a structured error with the fit intact.

## 8. Rules of the road (who edits what)

1. **Students**: run the scripts, edit only your own `config.toml` and parameters.
   The repos are read-only for you.
2. **Advanced users**: prototype new experiments + estimators in the sandbox
   (`scqo-contrib`, entry-point group `scqo.experiments.contrib`) — your runs land
   in the same datastore, so your evidence is findable.
3. **The manager** promotes proven experiments into `scqo/experiments/` + the driver
   repos (checklist in [CLAUDE.md](CLAUDE.md)).

## 9. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `ModuleNotFoundError: scqo` | venv not activated — Windows: `.venv\Scripts\Activate.ps1`; macOS/Linux: `source .venv/bin/activate` |
| `lab config not found` | your `--config`/`$SCQO_CONFIG` path is wrong (this error is intentional — better loud than silently unsaved) |
| `# lab config: built-in defaults ...` in the catalog header | no `~\.scqo\config.toml` yet: runs work but are **not saved** |
| A run shows `datastore_error` | measurement succeeded; only saving failed (disk full/locked). Fix the disk, rerun |
| `find_runs` misses runs you can see on disk | index stale → `python -m scqo <data_root>` |
| Unknown `run_id` in `--show` | same — rebuild the index |
| Want a clean slate | deleting `index.sqlite*` (all three files) is always safe; the folders are the data |

## 10. What Phase 1 does NOT include yet

- **Real Qblox hardware**: `QbloxBackend._to_canonical()` is still a TODO — Qblox
  runs are simulated-only today. QM hardware runs the three migrated experiments via
  `LCHQMDriver/customized/scqo/scripts/run_experiment.py` (with `backend = "qm"`,
  and `state_sync` stays `"pull"` there — see LCHQMDriver's CLAUDE.md).
- **GUI** (Phase 2): the plan is datasette over `index.sqlite`, then a small
  read-only run-browser.
- **Device-level inference** (Phase 3): combining runs into EJ/EC, anharmonicity,
  flux response via scqat + SCQ.jl.
