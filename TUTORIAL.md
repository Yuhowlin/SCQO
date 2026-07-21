# SCQO tutorial — measure and find your data

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
`.venv-qblox` for `scqo run`/`scqo state` on the Qblox
cluster, `.venv-qm` on the OPX1000. Cooldowns are no longer a tag you maintain:
the manager registers each cycle (`scqo device cooldown`), and every run you take is
auto-stamped with it — findable via `scqo find --cooldown`.

Everything below works identically on the simulated backend (the practice mode) and
on real hardware: you select a **device** (the sample) — and, when its cycle
declares several named measurement **setups**, which one you measure with
(`scqo user`). Everything behind a setup (instrument, wiring, config files) is
recorded by the manager, never typed into a command.

## 1. The system in one picture

```
you (script / notebook / later: GUI or AI agent)
        │  experiment name + parameters (plain JSON)
        ▼
   scqo.Session ──── catalog() · run() · find_runs() · device_state() · history()
        │
   Experiment  = probe (instrument half)  +  estimator (analysis half, scqat)
        │
   Backend     = Simulated | Qblox (LCHQBDriver) | QM (LCHQMDriver)
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
right venv is active (the old `python scripts\...` wrapper forms were retired in
v0.7.0; the `scqo` command is the one CLI).

First, know where your runs will land — `scqo user` answers before any instrument
time is spent (no arguments = pure diagnosis, it changes nothing):

```bash
scqo user                            # my selection + what a run resolves to (or the exact refusal)
scqo user --device chipA             # select YOUR sample (validated, written to ~\.scqo\user.toml)
scqo user --setup qblox_main         # needed only when the ACTIVE cycle has several setups
```

A single-setup cycle selects itself — most of the time picking the device once is
all there is. Then:

```bash
scqo run                                         # no arguments = show the menu
```

```
qubit_echo                    Hahn echo ... records t2_echo_s (record-only).
qubit_power_rabi              Sweep drive amplitude ... recalibrate pi_amp.
qubit_ramsey                  Two pi/2 pulses ... correct drive_freq and report T2*.
qubit_relaxation              Pi pulse + swept wait ... records t1_s (record-only).
qubit_spectroscopy            Sweep a weak saturation drive ... recalibrates drive_freq.
qubit_spectroscopy_flux       2D flux map ... proposes sweet spot / Ej_sum (physical parameters).
readout_frequency             Per-shot fidelity vs freq ... updates readout_freq.
readout_power                 Per-shot fidelity vs amp ... updates readout_amp.
resonator_spectroscopy        Sweep readout frequency ... updates readout_freq; proposes f_r_hz + kappa_tot_hz (on the resonator component).
resonator_spectroscopy_flux   2D resonator flux map ... proposes sweet spot / g (physical parameters).
resonator_spectroscopy_power_amp    Fast punchout (FPGA amplitude sweep) ... proposes readout_power_dbm + readout_freq.
resonator_spectroscopy_power_chain  Careful punchout (steps the output chain per point) ... proposes readout_power_dbm + readout_freq.
single_shot_readout           IQ blobs ... records readout fidelity (record-only).
```

Start with **resonator spectroscopy** — always the first measurement on a device: you
have to find the readout resonance before any qubit experiment means anything. Tag it
so you can find it later:

```bash
scqo run resonator_spectroscopy --targets q1 --tag mytest --note "first try"
```

You get the structured result as JSON — extracted physics, not raw traces:

```json
{
  "outcomes": { "q1": "successful" },
  "fit": { "q1": { "readout_freq": 5907471431.6,       // dip position (suggested update)
                    "dip_detuning_hz": -1795822.3,      // how far the dip sat from the old value
                    "old_readout_freq": 5909267253.9,
                    "f_r_hz": 5907471431.6,             // the dip IS the dressed resonator freq
                    "kappa_tot_hz": 1327410.5 } },      // fitted FWHM = total resonator linewidth
  "error": null,
  "run_id": "20260704-225450-SQ_demo-resonator_spectroscopy-01",
  "data_path": "D:\\qpu_data\\SQ_demo\\2026-07-04\\...-01",
  "suggestions": [ { "component": "q1", "field": "readout_freq", "store": "instrument",
                     "before": 5909267253.9, "after": 5907471431.6, "status": "pending" },
                   { "component": "q1_res", "field": "f_r_hz", "store": "physical", "..." : "..." },
                   { "component": "q1_res", "field": "kappa_tot_hz", "store": "physical", "..." : "..." } ]
}
```

> **Coming from v0.6.0?** Setups are NAMED now, and the commands regrouped. (1) A
> cycle's setups are `[<cycle>.setup.<name>]` sub-tables in the registry; a run
> refuses when the ACTIVE cycle has no setups yet, or has several and none is
> selected — `scqo user --setup <name>` picks yours (personal, validated; a
> single-setup cycle auto-selects). (2) The calibration view `scqo device` is now
> `scqo state` (same flags); `scqo devices` → `scqo device list`, `scqo cooldown` →
> `scqo device cooldown`, `scqo sample new` → `scqo device add` — no aliases.
> (3) Every run stamps (cooldown, setup name) — filter with `scqo find --setup`.
> Details in INSTALL §2's v0.7.0 note.

**Nothing is applied automatically.** The fitted `readout_freq` is a *suggested
update*: after the JSON, `scqo run` shows the suggestion table and asks you —

```
suggested updates (3 pending):
    #  component  field              store           current ->      suggested   status
    1  q1         readout_freq       instrument   5.90927e+09 ->   5.90747e+09 Hz   pending
    2  q1_res     f_r_hz             physical     (unmeasured) ->   5.90747e+09 Hz   pending
    3  q1_res     kappa_tot_hz       physical     (unmeasured) ->   1.32741e+06 Hz   pending
apply which updates? [a]ll / [n]one (default) / rows, component, field or component.field:
```

Press Enter to apply **nothing** (the default) — the device state is then unchanged
and the next experiment still runs on the OLD calibration; `a` applies everything,
or pick a subset (`1 3`, `q1`, `readout_freq`, `q1_res.kappa_tot_hz`) — partial
acceptance is normal. Every applied value lands in the change history linked to
this run. In a script or a pipe there is no prompt: the run is saved with its
suggestions **pending**, and you decide later — by run id, even days later:

```bash
scqo find --pending                          # runs with undecided suggestions
scqo accept                                  # the same list, decision-oriented
scqo accept <run_id> --list                  # look at the table again
scqo accept <run_id>                         # terminal: interactive picker
scqo accept <run_id> --field readout_freq --comment "matches the punchout map"
scqo accept <run_id> --reject --comment "fit chased a noise spike"
```

Applying goes through the live instrument config, so `scqo accept <run_id>` needs
the device's venv; `--list`, `--reject` and `find --pending` are datastore-only and
run anywhere the data drive is mounted. Two guards protect a deferred apply: a run
from an **older cooldown/setup era**, and a value whose *before* no longer matches
the device (someone recalibrated in between — **stale**). **At a terminal you never
need to know a flag**: a guard trip becomes a warning plus a [y/N] question showing
the exact values involved, and Enter always answers No — nothing changes unless you
explicitly confirm. In scripts nobody can answer, so `--force` pre-answers yes to
the era and stale questions.

**Changed your mind later?** A decided suggestion isn't dead. At a terminal, just
`scqo accept <old_run_id>` and pick the row — the picker asks
*"re-apply (rollback, overwriting the current …)?"* (or, for a rejected item,
*"accept it after all?"*). In a script, pass the answer as a flag:

```bash
scqo accept <old_run_id> --reapply --field readout_freq --comment "rolling back - the newer fit chased a spike"
```

A rollback deliberately overwrites the current value, so re-applied rows get no
stale question (the summary shows exactly what was overwritten); the cooldown-era
guard still applies. Every re-application is a fresh change-history entry linked to
the run it came from, so the viewer's Device page tells the whole story: A applied →
B applied → A re-applied.

**The estimator failed but the figure shows the value?** It happens — the dip is
plainly visible, the fit chased a noise spike past it. Don't write the number into
the device by hand (that loses the link to the data); attach it to the run instead:

```bash
scqo suggest <run_id> q0.readout_freq=5.912e9 --comment "read off the dip, fit missed it"
scqo suggest <run_id> q0.f_r_hz=5.912e9 q0.kappa_hz=1.1e6    # several at once; either store
```

Your value lands on that run as a pending suggestion marked `[operator: <you>]`
(the viewer shows the same badge), and from there everything above applies
unchanged — the interactive picker follows immediately at a terminal, `scqo
accept <run_id>` works later, era + stale guards included. The applied value is
credited to the run whose figure justified it, so trends and `--sources` stay
truthful. Hand-editing the state files instead would skip the instrument push and
show up as `(externally changed)` — the honest label for an untraceable write.

Once the readout is in place, the qubit experiments follow the same pattern:

```bash
scqo run qubit_ramsey --targets q1 --set num_points=201            # drive_freq + T2*
scqo run qubit_power_rabi --accept                                # apply updates immediately
scqo run resonator_spectroscopy --no-update ...                   # analyze only, nothing suggested
scqo run qubit_ramsey --params my.json                            # parameters from a file
```

One more distinction worth knowing: **instrument settings vs sample physics** —
the suggestion table's `store` column says which side each value belongs to. Both
land in YOUR context's `<device>/<cooldown>/<setup>/scqo/` folder, so two users on
two setups of one sample never see (or overwrite) each other's numbers.
Calibration knobs (`readout_freq`, `pi_amp`, ...; `store: instrument`) are pushed
to the instrument on accept and recorded in `scqo_state.json`. Measured physics —
T1, T2*, T2echo, and the flux maps' sweet spot / Ej_sum / f_r0 / g
(`store: physical`) — lands in `physical.json` beside it (same accept flow). Each
values file keeps its full change history in an append-only sidecar
(`scqo_state.history.jsonl` / `physical.history.jsonl`) — never edit any of them
by hand: a hand-edit skips the instrument push and shows as `(externally
changed)`; use `scqo suggest` instead. An estimate is only as clean as the chain it came through (a noisy drive
line shortens the measured T2; flux volts depend on the wiring), so each context's
physics stands on its own — compare across contexts via `scqo find` / the trends
page, never average. The setup-independent "true" sample physics is a future
*inference* over these measurements (`sample.json`, Phase 3).

```bash
scqo state --physical               # this context's measured physics (one row per qubit/field)
scqo state --physical --history     # who accepted what, when, from which run
scqo state --sources                # which run set each CURRENT value (both stores)
```

`--sources` answers *"which runs is my device built from?"* — the values in use
matter more than the pending ones. Every current value names the run that set it,
**strictly**: a value the vendor reseeded or another tool wrote shows
`(externally changed)` and credits no run; direct manual writes — `scqo set
q1.readout_freq=5.912e9` or a notebook assignment — show `(manual)` with the
operator's login. `scqo set` is for values you know from EXPERIENCE (no run to
credit): it previews current -> new with units, asks once, then writes through
the normal recorded path immediately (`--yes` in scripts).

Three tiers of parameters — each overriding the previous:

1. **Code defaults** — every knob ships a sensible built-in default; see them all
   with `... <experiment> --help`.
2. **Your standing defaults** (optional) — put semi-permanent project settings in
   `~\.scqo\parameters.toml`, one table per experiment (format and rules in
   [INSTALL.md](INSTALL.md) §2). Edit it once per project or cooldown and every run
   picks the values up; `--help` marks them like
   `default=15e6 [parameters.toml]`. With this file in place, most runs need no
   parameter flags at all.
3. **The command line** — always wins. **`--set KEY=VALUE`** changes *one* knob
   (repeat it for several), while **`--params`** loads a *whole set* as JSON — a file
   path or an inline object like `--params "{""num_points"": 201}"`. Don't mix the
   two syntaxes.

See every knob an experiment has — with your standing defaults marked — via
`scqo run <experiment> --help`.

```bash
scqo run resonator_spectroscopy --targets q1 --set frequency_span_hz=15e6
scqo run resonator_spectroscopy --help
```

The **standard bring-up** is the same command three times — resonator spectroscopy
→ qubit spectroscopy → power Rabi (Ramsey is the fine-tuning follow-up once a pi pulse
exists); accept each run's suggestions so the next step measures with them:

```bash
scqo run resonator_spectroscopy --targets q0 q1 --tag cooldown1
scqo run qubit_spectroscopy     --targets q0 q1 --tag cooldown1
scqo run qubit_power_rabi       --targets q0 q1 --tag cooldown1
```

(The old `scqo calibrate` sequence command was removed in v0.8 — not used at this
phase; a sequence runner returns with the AI loop, where it belongs.)

And the device's calibration state / change log any time (the first output line
names the device, YOUR resolved setup and its state file — state is per setup
since v0.9.0, so that line says whose numbers follow):

```bash
scqo state                      # current values per qubit (your setup)
scqo state --history 20         # who changed what, when, in which run
```

### Readout power — two modes (v0.8)

Behind the readout drive are TWO knobs, and two punchout experiments named for
the knob each one sweeps. They take **identical parameters** (an absolute-dBm
window: `min_power_dbm`/`max_power_dbm`, default −50…−20), report the same
absolute axis, and propose the same fields (`readout_power_dbm` +
`readout_freq`) — they differ only in mechanism, and each figure prints its mode
in the title so you can never confuse the two.

The knobs:

- **`readout_power_dbm`** — the ABSOLUTE readout power (dBm at the instrument
  port). Setting it re-solves the output chain (QM `full_scale_power_dbm` in 3 dB
  steps / Qblox `output_att` in 2 dB steps) so the digital amplitude lands at
  **≤ 0.5 of full scale** — the canonical operating point. `readout_amp` moves as
  a *coupled* side effect (the history marks such echoes with the causing field).
- **`readout_amp`** — the digital amplitude, relative to whatever the chain is
  set to. Fast and fine-grained, but the digital "1.0" means a different dBm on QM
  vs Qblox, which is why the punchouts work in absolute power instead.

The two experiments:

- **`resonator_spectroscopy_power_amp`** (fast) solves the chain for the WINDOW
  TOP once (`readout_power_dbm = max_power_dbm` — a recorded write, auto-reverted
  after the run), then sweeps the digital amplitude down from it in ONE hardware
  program. Every qubit hits the same absolute window exactly, whatever its
  standing power. Minutes fast; the trade-off is SNR — best at the top of the
  window, degrading toward the bottom where the DAC amplitude gets tiny.
- **`resonator_spectroscopy_power_chain`** (careful) steps the chain per power
  point: a Python loop (the chain knobs cannot change inside the FPGA loop)
  re-solves the chain so the digital amplitude stays at ~0.5 full scale for good
  SNR at EVERY point, and runs one 1D detuning scan per point — ascending,
  constant power within each scan, so resonator ring-down from a power jump can
  never contaminate. Wide and cross-backend comparable, but each point is a
  separate compile+run cycle (the default 21 points adds a few minutes).

Both record the boundary set/revert pair honestly (2 change records + coupled
echoes per qubit) and both leave the device exactly as found — accepting the
suggestion is what actually re-centers the chain. Both refuse to run on a qubit
whose `readout_power_dbm` is unknown (an unconfigured chain or zero amplitude:
the revert target would be undefined) — set it once, or fix `readout_amp`, first.

The workflow: run **`_amp`** for the quick look; reach for **`_chain`** when the
low-power end matters (the dispersive dip near the knee is faint) or for a
calibrated cross-backend sweep, then fine-tune with `_amp` again.

Absolute-scale honesty: on QM the dBm axis is exact at the port; on Qblox it is
derived from the nominal +5 dBm module full scale, good to ±a few dB (a per-setup
photon-number anchor is a Phase-3 refinement). BOTH experiments sweep a uniformly
spaced dBm axis on BOTH backends (`_chain` by re-solving the chain per point;
`_amp` with exact geometric amplitudes — on Qblox the amplitude axis is unrolled
point-by-point, since the hardware only loops linearly). Both figures share ONE format: the map
plus a SUBPLOT underneath (shared power axis) showing the per-point **digital
amplitude** and the used `output_att` / `full_scale_power_dbm` — for `_amp` the
chain curve is flat and the amplitude sweeps; for `_chain` the chain steps and
the amplitude sawtooths around 0.5 — so every map records what the instrument
was actually doing. Every run also records the raw chain values (`power_context`
in record.json), so past axes stay interpretable even after the chain changes.

## 3. Finding your data (the whole point)

```bash
scqo find                                   # latest runs, newest first
scqo find --cooldown cd8                    # everything from this cooldown cycle
scqo find --cooldown cd8 --setup qblox_main # ...narrowed to one of its measurement setups
scqo find --experiment resonator_spectroscopy --component q1 --since 2026-07-01
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
- `--setup` filters by the named setup stamped on each run; names are unique per
  cycle only, so combine it with `--cooldown`.
- Realized a week later that a run mattered? Tag it retroactively:
  `scqo tag 20260704-...-01 --add thesis-fig3 --note "best T2* so far"`
  (also backend-free).
- `--pending` narrows to runs whose suggested updates are still undecided —
  `scqo accept` shows the same list and is where you decide (section 2).

## 4. What's inside a run folder

```
<data_root>/SQ_demo/2026-07-04/20260704-225450-SQ_demo-resonator_spectroscopy-01/
    record.json          run manifest (its absence = run was incomplete/crashed)
    dataset.nc           the raw I/Q dataset (xarray/netCDF, dims: qubit × detuning_hz)
    parameters.json      exactly what you asked for
    result.json          outcomes + fitted quantities + error (if any)
    device_before.json   calibration state before ...
    device_after.json    ... and after the run (differs only where updates were applied)
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

- **Runs** — filter by experiment / qubit / tag / outcome / date, plus a
  **pending only** checkbox for runs with undecided suggested updates; click any run.
  Runs whose accepted values are still **LIVE on the device** carry a green
  `live:` line naming those fields — the at-a-glance answer to *"which runs is my
  device built from?"*.
- **Run page** — outcome badges, the fit table, **every figure inline** (the dip,
  the fringe, the 2D power map...), your parameters, the **suggested updates** table
  (pending / accepted / rejected, who decided, comments — deciding stays on the CLI:
  `scqo accept <run_id>`) with an **on device** column (LIVE, or superseded —
  linking the run that superseded it), and the device before → after diff. You can
  **add/remove tags and edit the note right here** — the viewer's only write,
  equivalent to `scqo tag`.
- **Trends** — a fitted quantity vs time per qubit (`t1_s`, `t2_star_s`, `ej_sum_ghz`,
  `readout_freq`, `pi_amp`, ...): coherence drift at a glance, every point linking
  to its run.
- **Device** — the current calibration and the sample's **physical parameters**
  (`physical.json`): every value links to the run that set it (`(manual)` and
  `(externally changed)` marked honestly), plus both change histories, each entry
  linking to the run that caused it.

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

- `~/.scqo/user.toml` — YOUR sample and setup selection plus your project tags.
  No editor needed for the selection: `scqo user --device chipA` writes it
  (validated), and `scqo user --setup <name>` picks a setup when the device's
  ACTIVE cycle has several — the instrument follows the selection via the device's
  cooldown registry. Only personal keys are allowed.
- `~/.scqo/parameters.toml` — your standing experiment parameters (three-tier rule
  in section 2). Applies automatically — no user.toml line needed; the optional
  `parameters_file` key in user.toml exists only to swap in a DIFFERENT file.

**Editing these files from an SSH terminal** (the device/setup selection needs no
editor — `scqo user --device <name>` writes it; hand-editing covers the rest, e.g.
`default_tags`) — GUI editors do NOT work over SSH: `notepad user.toml` starts an
invisible process on the server and no window ever appears (clean strays with
`Get-Process notepad | Stop-Process`). Use one of:

1. **PowerShell here-string** (no tools; writes UTF-8 without BOM — never use
   `Set-Content -Encoding UTF8`, its BOM breaks the TOML parser):

   ```powershell
   type ~\.scqo\user.toml                     # read
   [IO.File]::WriteAllText("$env:USERPROFILE\.scqo\user.toml", @'
   device = "chipA"
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
- Don't know what's available? `scqo device list` prints every known sample, one
  row per setup of its active cooldown cycle (backend, config folder — wiring lives
  inside the vendor config, not here), with `<- selected` marking yours and the
  `scqo user` command to change it. It touches no instrument, so it is always safe.

```
ssh <your-account>@<server>            # password prompt on first login
D:\github\.venv-qblox\Scripts\Activate.ps1     # (or .venv-qm for the OPX1000)
scqo run resonator_spectroscopy --targets q1 --tag mytest    # any directory works
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
store = DataStore(cfg.data_root, device_name=cfg.device)

store.find_runs(experiment="resonator_spectroscopy", qubit="q1", tag="cooldown1")
run = store.load_run("20260704-225450-SQ_demo-resonator_spectroscopy-01")  # record + params + figures
ds = store.open_dataset("20260704-225450-SQ_demo-resonator_spectroscopy-01")
ds["I"].sel(qubit="q1").plot()
store.tag_run("20260704-225450-SQ_demo-resonator_spectroscopy-01", add=["thesis-fig3"])
```

**Running measurements** from a notebook is the same Session the commands use —
let `build_session` do the wiring (it resolves your device → ACTIVE cycle → YOUR
setup, exactly like `scqo run`, and binds the per-setup state file). A one-off
known value needs no notebook: `scqo set q1.readout_freq=5.912e9` is the same
recorded write (`sess.set_values(...)` from code), with a confirmation prompt.

```python
from scqo.cli import build_session

sess, cfg = build_session()          # your config.toml + user.toml decide everything
```

(Hand-building instead — a custom backend object — still works, but a persisted
session needs its context: `make_session(backend, cfg, backend_label=...,
setup_name=..., cooldown_id=...)`, or pass `state_path` to `Session` directly for a
free-form scratch file.)

```python
result = sess.run("resonator_spectroscopy", {"qubits": ["q1"]})
result["suggestions"]                                    # the proposed updates (pending)
sess.accept(result["run_id"], fields=["readout_freq"], comment="looks right")
sess.reject(result["run_id"], comment="noise spike")     # decline the rest (no instrument)
sess.run("qubit_ramsey", {...}, update="apply")          # unattended / AI loop: apply now
sess.find_runs(experiment="resonator_spectroscopy", qubit="q1")  # list of dicts, newest first
sess.find_runs(pending=True)                             # undecided suggestions
sess.load_run(result["run_id"])                          # record + params + figure paths

sess.device_state()             # current calibration of every qubit (this context)
sess.physical_state()           # this context's measured physics
sess.history()                  # every calibration change: who, what, old → new, which run
sess.history(store="physical")  # same, for the physical-parameter ledger
```

## 7. When things fail (by design)

A failed fit or a bad probe **never crashes and never loses data**: you get
`"error": "..."`, the qubits are marked `failed`/`no_data`, nothing is suggested or
applied — and the run (including the misbehaving dataset) is still saved and
searchable via `--outcome failed`, because failed data is exactly what you want to
look at when debugging. Even "measurement fine, but applying an accepted value
failed" comes back structured: the fit stays intact and the item stays *pending*
with the error noted on it, so you can decide again once the cause is fixed.

## 8. Rules of the road (who edits what)

1. **Students**: run the commands; your only writes are your own `config.toml`,
   `parameters.toml` and `user.toml` — and the device/setup selection goes through
   `scqo user` (it writes your user.toml, validated). The repos and the shared
   registries are read-only for you.
2. **Advanced users**: prototype new experiments + estimators in the sandbox
   (`scqo-contrib`, entry-point group `scqo.experiments.contrib`) — your runs land
   in the same datastore, so your evidence is findable.
3. **The manager** owns the shared registries — cooldown cycles
   (`scqo device cooldown start`/`end`), the hand-added `[<cycle>.setup.<name>]`
   blocks in each device's `cooldowns.toml`, and `devices.toml` — and promotes
   proven experiments into `scqo/experiments/` + the driver repos (checklist in
   [CLAUDE.md](CLAUDE.md)). A setup block is just `backend` (+ `note`) — folder
   locations are DERIVED from the keys: put each real setup's vendor files in
   `<cooldown>/<setup>/backend_config/`; SCQO keeps its own state + physics in
   the sibling `<cooldown>/<setup>/scqo/`, auto-created on first save.

## 9. Components and the roster

A device is a set of named COMPONENTS, declared once per device in
`<data_root>/<device>/components.toml` (sibling of `cooldowns.toml` — a SAMPLE
fact, one copy above all cooldowns and setups). Each name binds up to two
CATEGORIES — a physical one ("what it is": fields land in physical.json) and an
instrument one ("how we drive it": fields land in scqo_state.json):

```toml
schema = 1
[components.q1]
physical   = "FixedTransmon"       # t1_s, t2_star_s, f_01_hz, ...
instrument = "ReadableTransmon"    # readout_freq, drive_freq, pi_amp, ...
operations = ["rx", "readout"]     # what experiments may require of it

[components.q1.design]             # DESIGN targets from the chip layout:
f_01_hz = 4.73e9                   # declared (not measured), physical-side
                                   # vocabulary, SI units. Bring-up sweeps
                                   # anchor on these when nothing is measured
                                   # yet (runs get tagged "seeded:...").
[components.q1_res]
physical = "Resonator"             # f_r_hz, f_r0_hz, kappa_tot_hz
[components.q1_ro]
physical = "ReadoutLine"           # g_hz; members name the topology
members  = { transmon = "q1", resonator = "q1_res" }
[components.q1_xy]
physical = "XYControl"             # topology-only (no measurable field yet)
members  = { transmon = "q1" }
```

Addressing is always `component.field` — `scqo set q1.t1_s=...`,
`scqo set q1_res.kappa_tot_hz=...` — and a wrong home answers with the right
one ("`f_r_hz` is a Resonator field — did you mean `q1_res.f_r_hz`?"). One fit
may legally write a knob AND a fact: resonator spectroscopy proposes
`q1.readout_freq` (the setting) and `q1_res.f_r_hz` (the measurement) from the
same dip. Flux-tunable devices add `ZControl` terms (`q1_z`: the volts-to-flux
transfer function `v_offset_v`/`v_per_phi0_v`) and get `idle_flux_v` on the
transmon automatically.

**Qubit pairs (QCQ tunable-coupler chips).** A pair is ONE component with both
slots, named by its sorted members:

```toml
[components.q1_q2]
physical   = "Coupling"            # zz_hz, j_hz — the pair's measured facts
instrument = "TransmonPair"        # coupler_decouple_v / coupler_interaction_v
members    = { high = "q1", low = "q2" }   # idle-frequency ordering — NEVER
                                   # control/target ("which qubit moves" is a
                                   # per-operation vendor fact, not topology)
operations = ["coupler_bias", "iswap"]
```

The pair's knobs are the coupler's two standing biases; `scqo set
q1_q2.coupler_decouple_v=0.081` replaces hand-editing the vendor config. The
`pair_zz_coupler` experiment automates that point: it maps the signed residual
ZZ vs coupler bias (echo fringe) and proposes the zero crossing as
`coupler_decouple_v` plus the residual `zz_hz` on the Coupling slot. A coupler
you want to TRACK physically gets an optional satellite (the resonator
pattern — measured through the pair, standard transmon vocabulary):
`members = { high=..., low=..., coupler="q1_q2_c" }` with `[components.q1_q2_c]
physical = "FluxTunableTransmon"` (+ design values).

**Assignable flux source.** The flux-map experiments take `flux_component`: a
qubit name (its z-line) or a pair name (its tunable coupler) swept INSTEAD of
each target's own z — `scqo run resonator_spectroscopy_flux --targets q1 --set
flux_component=q1_q2`. Such runs are RECORD-ONLY (the fits describe crosstalk /
coupler-induced shift, so nothing is proposed as the target's own physics), and
with a source assigned the targets themselves no longer need `flux_bias`.

TRIAL-PHASE rule: the roster is freely editable and `scqo doctor` only WARNS
about drift; when the component model goes production, names become append-only
forever (add or retire, never rename — trends and history key on them).
Experiments declare `target_category` + `required_operations`, and `scqo run`
refuses mismatched targets BEFORE touching hardware — a flux experiment on a
fixed-frequency chip is machine-refused, which is exactly the gate the AI loop
plans against.

## 10. Where does a value live? (the placement rule)

Every number in this system has exactly one home per role. When you don't know
where a value belongs — or why `scqo set` refuses a name — apply this checklist
**in order; first match wins**. Bench form: `scqo state --rule`. Classify each
*use* of a quantity, not each name: one fit may legally write a knob AND a fact
(`resonator_spectroscopy` writes the `readout_freq` setting and the `f_r_hz`
measurement from the same dip — two roles, two homes, on purpose).

1. **Gone when the run ends?** Sweep windows, shot counts, analysis assumptions,
   `Optional=None` overrides ("keep the vendor's value") → **per-run experiment
   Parameters.** No standing value survives the run (audit records may: the
   punchout's recorded set-then-revert is the sanctioned pattern).
2. **True of the chip in the dark?** The sample would still have it with every
   instrument off and no pulse ever sent (T1, f_r, EJ, the flux arch) → **physical
   property → physical.json.** "Instrument-independent" means *no instrument
   setting realizes it* — a sample fact in setup coordinates (`dv_phi0_v`, volts
   at the DAC) still lives here, with the plane declared in the name. One file
   per (cooldown, setup): each value is conditioned on trusting that instrument,
   and cross-setup disagreement is *information* (instrument systematics).
   Write: estimator suggest→accept, or `scqo set`.
3. **Measured, but a vendor knob realizes the result?** (time of flight) → the
   measurement's product IS the vendor knob's new value: write it at the
   catalogued vendor path, offline, **in the catalog row's unit**. Being
   calibrated by an experiment never changes ownership.
4. **A knob the calibration loop must read/write vendor-neutrally — meaning the
   same signal on every backend?** → **neutral field → scqo_state.json.**
   Defined in the *experiment's frame*: each driver converts instrument ↔
   experiment (hub-and-spoke — N converters, never N×N; instruments never
   convert to each other). Two value conventions:
   - absolute at the closest **declared** calibratable plane (Hz; dBm at the
     instrument output port; s) → `portable=True`;
   - dimensionless fraction of an untracked chain scale → `portable=False`,
     never copied across backends; it must name a portable twin
     (`readout_amp` → `readout_power_dbm`) or have its chain scale catalogued
     (`pi_amp` → the drive-port scale entries).
   Scope limits: emission-side only (acquisition-IQ-frame values — thresholds,
   demod rotation, weights — have no declarable plane, ever), and per-qubit
   only (portable-looking setup plumbing like a TWPA pump is still vendor).
5. **Measured, no knob?** Consulted in `scqo state` as standing state to gate
   the next step → `FIELDS push=False` (`readout_fidelity`; a *workflow
   declaration*, not an ontology). Only compared across runs → **run record
   only** (`p_e_given_g`, `power_context`) — compare across instruments by
   query, with backend provenance, never as state.
6. **Everything else is the instrument's** → vendor config, vendor-native
   units, catalogued (`scqo state --fields`) when relevant, with a kind:
   `[realizer]` realizes a neutral field — change THAT field via `scqo set`;
   `[candidate]` shared concept awaiting promotion (the visible backlog);
   `[vendor]` permanently vendor-owned, reason stated (LO = many splits give
   the same RF, and it is port-shared; discrimination frame = no plane);
   `[unique]` exists on THIS backend only — **any experiment touching it runs
   only on this instrument** (the lock-in corollary; `update()` cannot touch
   these by construction, so lock-in only enters through `probe()`).

**Canonical gauge (why `readout_power_dbm = x` picks ONE att/amp split):** when
a neutral field is realized by several vendor knobs, the driver's setter is a
deterministic policy, not a free choice — the quantized coarse knob (even
0–60 dB att / the −11..+16 dBm full-scale grid) is chosen so the continuous
amplitude lands as high as possible but ≤ 0.5 full scale (linearity + sweep
headroom + SNR), and the amplitude absorbs the exact residual. Same target,
same split, stateless; the policy is documented in the binding's `convert`
text and the realized split is stamped into `power_context` every run.

**Playbook — I hand-touched a vendor value (e.g. the LO):** scqo state is
unaffected and unaware (no history row). Reload sessions so the vendor
re-solves against your value; check the catalog row's constraints (IF range,
band, *fused* cross-references) BEFORE editing; per-run truth is
`power_context` in each record.json. If the row is `[realizer]`, the tracked
neutral value now lies — re-assert it through the front door
(`scqo set q1.readout_power_dbm=...`) or revert your edit.

**Playbook — "set integration time to 2000 ns":** find the row with
`scqo state --fields`; the number you type is in **the row's unit, not yours**
(Qblox `integration_time` is seconds — typing `2000` sets a 33-minute window);
on QM the row says the knob is *fused* with the pulse length — the edit has
side effects. Read the cross-reference before promising the change.

**Playbook — I want to FORCE a realizer knob (e.g. `output_att = 10`):**
hand-edit the vendor config offline (the sanctioned route). Pull-mode startup
recomputes the true `readout_power_dbm` from your forced chain, and
`power_context` stamps it per run. **Precedence: the neutral field owns its
realizers whenever written** — any later `readout_power_dbm` write re-solves
the chain and overwrites your forced value. Want att = 10 AND power = x? Force
att offline, then `scqo set q1.readout_amp=...` for the exact amplitude — that
half is recorded, and the coupled sync writes the true resulting power into
state. (A generic `scqo set --vendor` is deliberately not built.)

**Promotion (vendor `[candidate]` → neutral field):** *eligibility* = one SI
convention, pre-declared in the catalog entry, mapping deterministically onto
every backend (extra vendor granularity is never a blocker — it becomes vendor
fine print defaulting to the neutral value). *Trigger* = a loop experiment
needs it — decided at release review, **never at the bench**. *Interim* = the
fitted value goes in `Result.fit` (run record); hand-apply via vendor tools if
needed. *Cost* = one FieldSpec + one hardware-tested QubitView setter per
driver (conversions live only there) + the fieldmap binding + tests.

## 11. Troubleshooting

**First move, always: `scqo doctor`** — it checks your venv, drivers, the whole
config chain (shared config, user overlay, parameters file), data_root, registries
and the cooldown registry, and tells you what is wrong and how to fix it.

| Symptom | Cause / fix |
|---|---|
| `ModuleNotFoundError` / `lab config not found` / nothing gets saved | setup problem — see [INSTALL.md](INSTALL.md) §1–§2 and the §6 symptom table |
| `scqo: command not found` (or the term is not recognized) | no venv activated — or scqo was upgraded without re-running the INSTALL §1 `uv pip install -e` line (the command registers at install time) |
| `notepad ...` over SSH does nothing | GUI apps have no display in an SSH session (the process starts invisibly on the server) — use the §5 editing methods (here-string / scp / VS Code Remote-SSH) |
| `device ... is on backend 'qblox' ... driver is not registered in this environment` | right command, wrong venv — the message names the venv to activate (or, if you ARE in it, the install line to re-run) |
| `invalid cooldown registry ...` or another refusal naming `cooldowns.toml` at run start | the manager's cycle registry is broken or incomplete (it stamps runs and selects the instrument, so runs refuse BEFORE instrument time) — `scqo device cooldown` (no args) validates it; the message names the fix (INSTALL §6 has the full list) |
| `cycle ... has N setups and none is selected` | the ACTIVE cycle offers several measurement setups and a run will not guess — pick yours once: `scqo user --setup <name>` (a single-setup cycle needs no selection) |
| `setup 'x' ... does not exist in the ACTIVE cycle` | your selection went stale (typically after a new cycle started) — `scqo user --setup <name>` picks a current one, `scqo user --clear-setup` returns to auto-selection; bare `scqo user` always shows what a run would resolve to |
| A run shows `datastore_error` | measurement succeeded; only saving failed (disk full/locked). Fix the disk, rerun |
| `invalid parameter-defaults file ...` (even on `--help`) | your `parameters.toml` has a syntax error — it affects measurements, so it never fails silently. Fix the named file |
| `find_runs` misses runs you can see on disk | index stale → `python -m scqo <data_root>` |
| Unknown `run_id` in `--show` | same — rebuild the index |
| Want a clean slate | deleting `index.sqlite*` (all three files) is always safe; the folders are the data |

## 12. What the system does NOT include yet

Everything above is real: **both instruments are hardware-proven** through this path
(Qblox cluster and OPX1000, since 2026-07-05), the catalog holds 12 experiments, and
the GUI you read about in section 4 (viewer + datasette) is shipped. Still ahead:

- **Device-level inference** (Phase 3): combining runs into EJ/EC, anharmonicity,
  flux response via scqat + SCQ.jl — the physical-parameter ledger (`physical.json`:
  T1/T2, sweet spots, Ej_sum, f_r0, g) is its input.
- **Running measurements — and accepting updates — from the viewer**, plus a
  per-instrument run lock — until then, measuring and deciding stay on the CLI and
  one-measurement-per-instrument stays a social rule.
- **The AI loop**: the catalog/Session JSON surface is built for it, but no agent
  drives it yet.
