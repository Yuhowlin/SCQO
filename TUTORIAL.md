# SCQO tutorial — measure, calibrate, and find your data

The student guide to the lab's measurement system. You run experiments by *physics
name* (resonator spectroscopy, Ramsey, power Rabi), get fitted device parameters back,
and can find every dataset you ever took. You never touch instrument code, and you
never edit anything in the repos.

**Prerequisites** (done once per machine — see [INSTALL.md](INSTALL.md), or ask
whoever set up the PC): a venv activated and a lab config in place (your own
`~\.scqo\config.toml`, or the server's shared one). **Which venv? One rule:** activate
**view** to look at data — the run-viewer, browsing, `scqo find`, `scqo tag`
(`D:\github\.venv-view\Scripts\Activate.ps1`, prompt `(view)`; macOS/Linux
`source ~/github/.venv-view/bin/activate` — the venvs live NEXT TO the repos, not
inside them, so use the full path or run from the repos' parent folder) — and an
instrument env only to measure:
`.venv-qblox` for `scqo run`/`scqo calibrate`/`scqo device` on the Qblox
cluster, `.venv-qm` on the OPX1000. Cooldowns are no longer a tag you maintain:
the manager registers each cycle (`scqo cooldown`), and every run you take is
auto-stamped with it — findable via `scqo find --cooldown`.

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

Every command below is the **`scqo` command** — it works from ANY directory once the
right venv is active (the old `python scripts\...` forms still work inside a driver
repo; they are thin wrappers around the same engine).

```bash
scqo run                                         # no arguments = show the menu
```

```
qubit_echo                    Hahn echo ... records t2_echo_s (record-only).
qubit_power_rabi              Sweep drive amplitude ... recalibrate pi_amp.
qubit_ramsey                  Two pi/2 pulses ... correct drive_freq and report T2*.
qubit_relaxation              Pi pulse + swept wait ... records t1_s (record-only).
qubit_spectroscopy            Sweep a weak saturation drive ... recalibrates drive_freq.
qubit_spectroscopy_flux       2D flux map ... reports sweet spot / Ej_sum (no writeback).
readout_frequency             Per-shot fidelity vs freq ... updates readout_freq.
readout_power                 Per-shot fidelity vs amp ... updates readout_amp.
resonator_spectroscopy        Sweep readout frequency ... updates readout_freq.
resonator_spectroscopy_flux   2D resonator flux map ... reports sweet spot / g (no writeback).
resonator_spectroscopy_power  2D punchout ... updates readout_amp and readout_freq.
single_shot_readout           IQ blobs ... records readout fidelity (record-only).
```

Start with **resonator spectroscopy** — always the first measurement on a device: you
have to find the readout resonance before any qubit experiment means anything, and its
writeback (`readout_freq`) is the most benign one. Tag it so you can find it later:

```bash
scqo run resonator_spectroscopy --qubits q1 --tag mytest --note "first try"
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
scqo run qubit_ramsey --qubits q1 --set num_points=201            # drive_freq + T2*
scqo run qubit_power_rabi                                         # all qubits, defaults
scqo run resonator_spectroscopy --no-update ...                   # analyze only, no writeback
scqo run qubit_ramsey --params my.json                            # parameters from a file
```

Three tiers of parameters — each overriding the previous:

1. **Code defaults** — every knob ships a sensible built-in default; see them all
   with `... <experiment> --help`.
2. **Your standing defaults** (optional) — put semi-permanent project settings in
   `~\.scqo\parameters.toml`, one table per experiment (format and rules in
   [INSTALL.md](INSTALL.md) §2). Edit it once per project or cooldown and every run —
   including `calibrate.py`'s steps — picks the values up; `--help` marks them like
   `default=15e6 [parameters.toml]`. With this file in place, most runs need no
   parameter flags at all.
3. **The command line** — always wins. **`--set KEY=VALUE`** changes *one* knob
   (repeat it for several), while **`--params`** loads a *whole set* as JSON — a file
   path or an inline object like `--params "{""num_points"": 201}"`. Don't mix the
   two syntaxes.

See every knob an experiment has — with your standing defaults marked — via
`scqo run <experiment> --help`. (Inside a driver repo the per-experiment launchers
`scripts/experiments/<name>.py` still exist with the same flags.)

```bash
scqo run resonator_spectroscopy --qubits q1 --set frequency_span_hz=15e6
scqo run resonator_spectroscopy --help
```

The **daily workflow** is one command — the bring-up sequence (resonator spectroscopy
→ qubit spectroscopy → power Rabi; Ramsey is the fine-tuning follow-up once a pi pulse
exists — run it explicitly), every step saved + tagged, summary at the end:

```bash
scqo calibrate --qubits q0 q1 --tag cooldown1
scqo calibrate --skip resonator_spectroscopy       # drop a step
```

And the device's calibration state / change log any time:

```bash
scqo device                     # current values per qubit
scqo device --history 20        # who changed what, when, in which run
```

## 3. Finding your data (the whole point)

```bash
scqo find                                   # latest runs, newest first
scqo find --cooldown cd8                    # everything from this cooldown cycle
scqo find --experiment resonator_spectroscopy --qubit q1 --since 2026-07-01
scqo find --outcome failed                  # what went wrong lately?
scqo find --show 20260704-225450-SQ_demo-resonator_spectroscopy-01   # one run, in full
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
  `scqo tag 20260704-...-01 --add thesis-fig3 --note "best T2* so far"`
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
  note right here** — the viewer's only write, equivalent to `scqo tag`.
- **Trends** — a fitted quantity vs time per qubit (`t1_s`, `t2_star_s`,
  `readout_freq`, `pi_amp`, ...): coherence drift at a glance, every point linking
  to its run.
- **Device** — the last observed calibration and the full change history, each
  entry linking to the run that caused it.

Power users: `python -m scqo.browse` still serves raw datasette on **8081** for
ad-hoc SQL, facets and CSV export (same canned queries as before).

## 5. Working from your own laptop (nothing to install)

Once the lab server is running, your own laptop needs **no Python, no venv, no
config file** — just two addresses:

**To see data — the browser.** Open `http://<server>:8080` (ask the manager for the
server's name/IP). Everything in the viewer section above works from any machine on
the lab network, including tag/note editing.

**To measure — SSH.** Every OS ships an ssh client (Windows PowerShell, macOS
Terminal, Linux). Ask the manager for an account on the server, then a session looks
like this:

Your account carries your own settings — no shared file to fight over:

- `~/.scqo/user.toml` — pick YOUR instrument (`backend = "qm"`; the sample follows
  it) and your project tags. Only personal keys are allowed.
- `~/.scqo/parameters.toml` — your standing experiment parameters (three-tier rule
  in section 2). Applies automatically — no user.toml line needed; the optional
  `parameters_file` key in user.toml exists only to swap in a DIFFERENT file.

**Editing these files from an SSH terminal** — GUI editors do NOT work over SSH:
`notepad user.toml` starts an invisible process on the server and no window ever
appears (clean strays with `Get-Process notepad | Stop-Process`). Use one of:

1. **PowerShell here-string** (no tools; writes UTF-8 without BOM — never use
   `Set-Content -Encoding UTF8`, its BOM breaks the TOML parser):

   ```powershell
   type ~\.scqo\user.toml                     # read
   [IO.File]::WriteAllText("$env:USERPROFILE\.scqo\user.toml", @'
   backend = "qblox"
   default_tags = ["projA"]
   '@)
   ```

   PowerShell shows `>>` until the closing `'@` — type it at the start of the line.
   To ADD an experiment table to parameters.toml, use `AppendAllText` the same way.
2. **scp round-trip** from your laptop (OpenSSH lands in your profile, so relative
   paths work): `scp <you>@<server>:.scqo/parameters.toml .` → edit locally →
   `scp parameters.toml <you>@<server>:.scqo/`.
3. **VS Code Remote-SSH** if you edit these often — a real editor saving directly
   on the server, correct encoding by default.
- Don't know what's available? `scqo devices` prints every configured
  backend with its sample, instrument address, active cooldown and wiring — plus the
  exact user.toml line to select it. It touches no instrument, so it is always safe.

```
ssh <your-account>@<server>            # password prompt on first login
D:\github\.venv-qblox\Scripts\Activate.ps1     # (or .venv-qm for the OPX1000)
scqo run resonator_spectroscopy --qubits q1 --tag mytest    # any directory works
scqo find --limit 5
exit
```

The run executes on the server (which owns the instruments and the data), your
laptop is only the keyboard — closing the lid mid-run kills the run, so let a
measurement finish before disconnecting. Figures appear in the viewer seconds later.

Rules that keep shared instruments sane:

- Every run records **you** as its operator (your login name) — visible in the
  viewer and `scqo find --operator <name>`. Your work is attributable; so are
  your mistakes. Both are fine — failed runs are searchable on purpose.
- **One measurement at a time per instrument.** Check the viewer's latest runs (or
  ask in the lab chat) before starting a long sweep; a second program on the same
  instrument will fail or corrupt both.
- SSH is for *measuring*. For looking at data, use the browser — it can't break
  anything.

## 6. Working in Python / Jupyter

**Where do my notebooks/scripts live?** Anywhere OUTSIDE the governed repos — e.g. a
personal `lab-notebooks/` folder (make it your own git repo if you want history).
Because `scqo`/`scqat` are installed in every env, imports work from any directory;
just select the right venv as your interpreter/kernel (VS Code: pick
`.venv-view\Scripts\python.exe` for analysis notebooks, `.venv-qblox\...` if the
notebook drives the instrument; or `uv pip install --python <venv-python> jupyterlab
ipykernel`). If a notebook grows into a new *experiment* or *estimator*, it graduates
to the contrib sandbox (section 8) — never straight into SCQO or a driver repo.

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

## 7. When things fail (by design)

A failed fit or a bad probe **never crashes and never loses data**: you get
`"error": "..."`, the qubits are marked `failed`/`no_data`, nothing is written back
to the device — and the run (including the misbehaving dataset) is still saved and
searchable via `--outcome failed`, because failed data is exactly what you want to
look at when debugging. Even "measurement fine, instrument rejected the writeback"
comes back as a structured error with the fit intact.

## 8. Rules of the road (who edits what)

1. **Students**: run the scripts, edit only your own `config.toml`,
   `parameters.toml` and `user.toml`. The repos are read-only for you.
2. **Advanced users**: prototype new experiments + estimators in the sandbox
   (`scqo-contrib`, entry-point group `scqo.experiments.contrib`) — your runs land
   in the same datastore, so your evidence is findable.
3. **The manager** promotes proven experiments into `scqo/experiments/` + the driver
   repos (checklist in [CLAUDE.md](CLAUDE.md)).

## 9. Troubleshooting

**First move, always: `scqo doctor`** — it checks your venv, drivers, the whole
config chain (shared config, user overlay, parameters file), data_root, registries
and the cooldown registry, and tells you what is wrong and how to fix it.

| Symptom | Cause / fix |
|---|---|
| `ModuleNotFoundError` / `lab config not found` / nothing gets saved | setup problem — see [INSTALL.md](INSTALL.md) §1–§2 and the §6 symptom table |
| `scqo: command not found` (or the term is not recognized) | no venv activated — or scqo was upgraded without re-running the INSTALL §1 `uv pip install -e` line (the command registers at install time) |
| `notepad ...` over SSH does nothing | GUI apps have no display in an SSH session (the process starts invisibly on the server) — use the §5 editing methods (here-string / scp / VS Code Remote-SSH) |
| `backend 'qblox' needs the 'qblox' driver...` | right command, wrong venv — the message names the venv to activate |
| A run shows `datastore_error` | measurement succeeded; only saving failed (disk full/locked). Fix the disk, rerun |
| `invalid parameter-defaults file ...` (even on `--help`) | your `parameters.toml` has a syntax error — it affects measurements, so it never fails silently. Fix the named file |
| `find_runs` misses runs you can see on disk | index stale → `python -m scqo <data_root>` |
| Unknown `run_id` in `--show` | same — rebuild the index |
| Want a clean slate | deleting `index.sqlite*` (all three files) is always safe; the folders are the data |

## 10. What the system does NOT include yet

Everything above is real: **both instruments are hardware-proven** through this path
(Qblox cluster and OPX1000, since 2026-07-05), the catalog holds 12 experiments, and
the GUI you read about in section 4 (viewer + datasette) is shipped. Still ahead:

- **Device-level inference** (Phase 3): combining runs into EJ/EC, anharmonicity,
  flux response via scqat + SCQ.jl — the recorded T1/T2/fidelity ledger is its input.
- **Running measurements from the viewer** (run-forms with an approval gate) and a
  per-instrument run lock — until then, measuring stays on the CLI and
  one-measurement-per-instrument stays a social rule.
- **The AI loop**: the catalog/Session JSON surface is built for it, but no agent
  drives it yet.
