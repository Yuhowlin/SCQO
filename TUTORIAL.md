# SCQO tutorial — measure, calibrate, and find your data

The student guide to the lab's measurement system. You run experiments by *physics
name* (resonator spectroscopy, Ramsey, power Rabi), get fitted device parameters back,
and can find every dataset you ever took. You never touch instrument code, and you
never edit anything in the repos.

**Prerequisites** (done once per machine — see [INSTALL.md](INSTALL.md), or ask
whoever set up the PC): a venv activated and your personal `~\.scqo\config.toml` in
place. **Which venv? One rule:** activate **view** to look at data — the run-viewer,
browsing, `find_runs.py`, `tag_run.py` (`.venv-view\Scripts\Activate.ps1`, prompt
`(view)`; macOS/Linux `source .venv-view/bin/activate`) — and an instrument env only
to measure: `.venv-qblox` for `run_experiment.py`/`calibrate.py`/`device.py` on the
Qblox cluster, `.venv-qm` on the OPX1000. The one
thing to keep updated yourself: `default_tags = ["cooldown..."]` in that file — edit
it once per cooldown and every run you take is automatically findable by cooldown.

Everything below works identically on the simulated backend, the **virtual twin** of
your real chip (`qblox_sim`/`qm_sim` — the recommended practice mode), and real
hardware: the backend choice lives in the config, not in the commands.

## 1. The system in one picture

```
you (script / notebook / later: GUI or AI agent)
        │  experiment name + parameters (plain JSON)
        ▼
   scqo.Session ──── catalog() · run() · find_runs() · device_state() · history()
        │
   Experiment  = probe (instrument half)  +  estimator (analysis half, scqat)
        │
   Backend     = Simulated | virtual twin | Qblox (LCHQBDriver) | QM (LCHQMDriver)
        │
   DataStore   = every run saved to a folder + searchable SQLite index
```

- **You think in physics**: qubit names, spans, idle times, π-amplitudes.
- **The estimator** (scqat) fits the data and reports extracted quantities + a
  per-qubit `successful/failed` verdict.
- **The datastore** saves *every* run — raw data, parameters, fit result, device
  snapshots, and the fit figures — under one folder per run, and indexes it so you
  can ask "what T2* did q1 get this week?" without remembering any filename.

## 2. Your first measurement

```bash
cd LCHQBDriver          # D:\github\LCHQBDriver on the lab PC, ~/github/LCHQBDriver on a Mac
python scripts/run_experiment.py                 # no arguments = show the menu
```

```
qubit_power_rabi              Sweep drive amplitude ... recalibrate pi_amp.
qubit_ramsey                  Two pi/2 pulses ... correct drive_freq and report T2*.
qubit_spectroscopy            Sweep a weak saturation drive ... recalibrates drive_freq.
resonator_spectroscopy        Sweep readout frequency ... updates readout_freq.
resonator_spectroscopy_power  2D punchout ... updates readout_amp and readout_freq.
t1_relaxation                 Pi pulse + swept wait ... reports T1 (no writeback).
```

Start with **resonator spectroscopy** — always the first measurement on a device: you
have to find the readout resonance before any qubit experiment means anything, and its
writeback (`readout_freq`) is the most benign one. Tag it so you can find it later:

```bash
python scripts/run_experiment.py resonator_spectroscopy --qubits q1 --tag mytest --note "first try"
```

You get the structured result as JSON — extracted physics, not raw traces:

```json
{
  "outcomes": { "q1": "successful" },
  "fit": { "q1": { "readout_freq": 5907471431.6,       // dip position, written back
                    "dip_detuning_hz": -1795822.3,      // how far the dip sat from the old value
                    "old_readout_freq": 5909267253.9 } },
  "error": null,
  "run_id": "20260704-225450-SQ_demo-resonator_spectroscopy-01",
  "data_path": "D:\\qpu_data\\SQ_demo\\2026-07-04\\20260704-225450-SQ_demo-resonator_spectroscopy-01"
}
```

Because the fit succeeded, `readout_freq` was **written back** to the device state
(with a history record linking it to this run). Once the readout is in place, the
qubit experiments follow the same one-liner pattern:

```bash
python scripts/run_experiment.py qubit_ramsey --qubits q1 --set num_points=201   # drive_freq + T2*
python scripts/run_experiment.py qubit_power_rabi                 # all qubits, defaults
python scripts/run_experiment.py resonator_spectroscopy --no-update ...   # analyze only, no writeback
python scripts/run_experiment.py qubit_ramsey --params my.json    # parameters from a file
```

Two ways to pass parameters — don't mix their syntaxes: **`--set KEY=VALUE`** changes
*one* knob (repeat it for several), while **`--params`** loads a *whole set* as JSON —
a file path or an inline object like `--params "{""num_points"": 201}"`. See every
knob an experiment has with `... <experiment> --help`.

Prefer one command per experiment (qualibrate-node style)? Every cataloged experiment
has its own launcher in `scripts/experiments/` — same flags, and `--help` shows that
experiment's full parameter list with defaults and descriptions:

```bash
python scripts/experiments/resonator_spectroscopy.py --qubits q1 --set frequency_span_hz=15e6
python scripts/experiments/resonator_spectroscopy.py --help
```

The **daily workflow** is one command — the bring-up sequence (resonator spectroscopy
→ qubit spectroscopy → power Rabi; Ramsey is the fine-tuning follow-up once a pi pulse
exists — run it explicitly), every step saved + tagged, summary at the end:

```bash
python scripts/calibrate.py --qubits q0 q1 --tag cooldown1
python scripts/calibrate.py --skip resonator_spectroscopy       # drop a step
```

And the device's calibration state / change log any time:

```bash
python scripts/device.py                    # current values per qubit
python scripts/device.py --history 20       # who changed what, when, in which run
```

## 3. Finding your data (the whole point)

```bash
python scripts/find_runs.py                                   # latest runs, newest first
python scripts/find_runs.py --tag cooldown1                   # everything from this cooldown
python scripts/find_runs.py --experiment resonator_spectroscopy --qubit q1 --since 2026-07-01
python scripts/find_runs.py --outcome failed                  # what went wrong lately?
python scripts/find_runs.py --show 20260704-225450-SQ_demo-resonator_spectroscopy-01   # one run, in full
```

```
20260704-225450-SQ_demo-resonator_spectroscopy-01   successful  q1   cooldown1,mytest  SQ_demo/2026-07-04/20260704-225450-SQ_demo-resonator_spectroscopy-01
```

- Dates in filters are **local lab time** and match the folder names; a bare date in
  `--until` includes that whole day.
- `find_runs` touches no instrument — it runs anywhere the data drive is mounted.
- Several samples share one data_root: every run is stamped with its device (= sample)
  name, so `--device chipA` (or the viewer's device dropdown) narrows to one chip.
- Realized a week later that a run mattered? Tag it retroactively:
  `python scripts/tag_run.py 20260704-...-01 --add thesis-fig3 --note "best T2* so far"`
  (also backend-free).

## 4. What's inside a run folder

```
<data_root>/SQ_demo/2026-07-04/20260704-225450-SQ_demo-resonator_spectroscopy-01/
    record.json          run manifest (its absence = run was incomplete/crashed)
    dataset.nc           the raw I/Q dataset (xarray/netCDF, dims: qubit × detuning_hz)
    parameters.json      exactly what you asked for
    result.json          outcomes + fitted quantities + error (if any)
    device_before.json   calibration state before ...
    device_after.json    ... and after the writeback
    analysis/q1/         per-qubit fit artifacts from scqat:
        resonator_spectroscopy.png                         ← the dip + fit, already drawn
        resonator_spectroscopy_metadata.json               fit parameters, fit quality
        resonator_spectroscopy_plotdata.nc                 arrays to redraw without refitting
```

(A Ramsey run looks the same with its own artifacts: `ramsey_time_domain.png`,
`ramsey_fft_spectrum.png`, etc.)

**The folder is the truth.** The SQLite index (`<data_root>/index.sqlite`) is only a
cache — if it is ever missing or stale, rebuild it losslessly:

```powershell
python -m scqo <data_root>
```

### The run viewer — your daily data GUI

One command opens the lab's data as a website (one-time: viewer extras via
`uv pip install fastapi uvicorn jinja2`, already installed on the lab PC):

```powershell
python -m scqo.viewer            # -> http://127.0.0.1:8080
```

Four pages (port convention: **8001 qualibrate · 8080 viewer · 8081 datasette** —
all can run at once):

- **Runs** — filter by experiment / qubit / tag / outcome / date; click any run.
- **Run page** — outcome badges, the fit table, **every figure inline** (the dip,
  the fringe, the 2D power map...), your parameters, and the device before → after
  diff with changed fields highlighted. You can **add/remove tags and edit the
  note right here** — the viewer's only write, equivalent to `tag_run.py`.
- **Trends** — a fitted quantity vs time per qubit (`t1_s`, `t2_star_s`,
  `readout_freq`, `pi_amp`, ...): coherence drift at a glance, every point linking
  to its run.
- **Device** — the last observed calibration and the full change history, each
  entry linking to the run that caused it.

Power users: `python -m scqo.browse` still serves raw datasette on **8081** for
ad-hoc SQL, facets and CSV export (same canned queries as before).

## 5. Working in Python / Jupyter

**Where do my notebooks/scripts live?** Anywhere OUTSIDE the governed repos — e.g. a
personal `lab-notebooks/` folder (make it your own git repo if you want history).
Because `scqo`/`scqat` are installed in every env, imports work from any directory;
just select the right venv as your interpreter/kernel (VS Code: pick
`.venv-view\Scripts\python.exe` for analysis notebooks, `.venv-qblox\...` if the
notebook drives the instrument; or `uv pip install --python <venv-python> jupyterlab
ipykernel`). If a notebook grows into a new *experiment* or *estimator*, it graduates
to the contrib sandbox (section 7) — never straight into SCQO or a driver repo.

**Analyzing saved data needs no backend at all** — this is what most notebooks are:

```python
from scqo import DataStore, load_lab_config

cfg = load_lab_config()
store = DataStore(cfg.data_root, device_name=cfg.device_name)

store.find_runs(experiment="resonator_spectroscopy", qubit="q1", tag="cooldown1")
run = store.load_run("20260704-225450-SQ_demo-resonator_spectroscopy-01")  # record + params + figures
ds = store.open_dataset("20260704-225450-SQ_demo-resonator_spectroscopy-01")
ds["I"].sel(qubit="q1").plot()
store.tag_run("20260704-225450-SQ_demo-resonator_spectroscopy-01", add=["thesis-fig3"])
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

result = sess.run("resonator_spectroscopy", {"qubits": ["q1"]})
sess.find_runs(experiment="resonator_spectroscopy", qubit="q1")  # list of dicts, newest first
sess.load_run(result["run_id"])                          # record + params + figure paths

sess.device_state()   # current calibration of every qubit
sess.history()        # every change ever: who, what, old → new, which run caused it
```

## 6. When things fail (by design)

A failed fit or a bad probe **never crashes and never loses data**: you get
`"error": "..."`, the qubits are marked `failed`/`no_data`, nothing is written back
to the device — and the run (including the misbehaving dataset) is still saved and
searchable via `--outcome failed`, because failed data is exactly what you want to
look at when debugging. Even "measurement fine, instrument rejected the writeback"
comes back as a structured error with the fit intact.

## 7. Rules of the road (who edits what)

1. **Students**: run the scripts, edit only your own `config.toml` and parameters.
   The repos are read-only for you.
2. **Advanced users**: prototype new experiments + estimators in the sandbox
   (`scqo-contrib`, entry-point group `scqo.experiments.contrib`) — your runs land
   in the same datastore, so your evidence is findable.
3. **The manager** promotes proven experiments into `scqo/experiments/` + the driver
   repos (checklist in [CLAUDE.md](CLAUDE.md)).

## 8. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `ModuleNotFoundError` / `lab config not found` / nothing gets saved | setup problem — see [INSTALL.md](INSTALL.md) §5 |
| A run shows `datastore_error` | measurement succeeded; only saving failed (disk full/locked). Fix the disk, rerun |
| `find_runs` misses runs you can see on disk | index stale → `python -m scqo <data_root>` |
| Unknown `run_id` in `--show` | same — rebuild the index |
| Want a clean slate | deleting `index.sqlite*` (all three files) is always safe; the folders are the data |

## 9. What Phase 1 does NOT include yet

- **Real Qblox hardware**: `QbloxBackend._to_canonical()` is still a TODO — Qblox
  runs are simulated/virtual-twin only today. QM hardware runs the three migrated
  experiments via `LCHQMDriver/scripts/run_experiment.py` (with
  `backend = "qm"`, and `state_sync` stays `"pull"` there — see LCHQMDriver's CLAUDE.md).
- **GUI** (Phase 2): the plan is datasette over `index.sqlite`, then a small
  read-only run-browser.
- **Device-level inference** (Phase 3): combining runs into EJ/EC, anharmonicity,
  flux response via scqat + SCQ.jl.
