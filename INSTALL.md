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
| `D:\github\.venv-view` | `(view)` | scqo `[viewer]` + scqat + datasette + pytest — **no instrument libraries** | look at data (the common case): run-viewer, SQL browser, `scqo find`, `scqo tag`. Works identically on an analysis-only laptop/Mac. |
| `D:\github\.venv-qblox` | `(qblox)` | the view stack + LCHQBDriver + `qblox-scheduler==1.0.0b4` (hardware-proven) + scqo-contrib | measure on the Qblox cluster: `scqo run`, `scqo state` |
| `D:\github\.venv-qm` | `(.venv-qm)` | pinned QM stack, py3.11 (`LCHQMDriver\requirements-qm.lock.txt`) + scqo/scqat/LCHQMDriver editables | measure on the OPX1000 or use qualibrate — `qm.bat` activates it for you |

All three import scqo/scqat from the same editable checkouts, so they never drift on
the neutral layer. `uv` creates standard venvs and downloads Python itself if the
machine has none.

The repos must sit next to each other in one folder (`SCQO` and `scqat`
as siblings) — on the lab PC that folder is `D:\github`; on your own Mac clone them:

```bash
mkdir -p ~/github && cd ~/github
git clone https://github.com/shiau109/SCQO.git
git clone https://github.com/shiau109/scqat.git
git clone https://github.com/shiau109/LCHQBDriver.git    # only if this machine drives the Qblox cluster
git clone https://github.com/shiau109/LCHQMDriver.git    # only if this machine drives the OPX1000
git clone https://github.com/shiau109/scqo-contrib.git   # optional: the Tier-2 sandbox
```

(A repo that is still **private** answers `Repository not found` when the active
GitHub credential cannot see it — sign in with an account that has access.)

**Windows: install uv once per machine** (no admin needed):

```powershell
powershell -ExecutionPolicy Bypass -Command "irm https://astral.sh/uv/install.ps1 | iex"
```

The installer updates the registry PATH, so only **new** terminals see `uv`; in the
same shell call it by full path — `& "$env:USERPROFILE\.local\bin\uv.exe" venv ...`.

**Server reached via SSH (multi-account)?** Install the Pythons to a shared folder
and create the venvs with the EXPLICIT patch-directory interpreter, not a bare
version number:

```powershell
$env:UV_PYTHON_INSTALL_DIR = 'D:\uv\python'   # shared — NOT inside one user's profile
uv python install 3.12 3.11
uv venv .venv-view --python D:\uv\python\cpython-3.12.13-windows-x86_64-none\python.exe --prompt view
```

Two reasons, both learned on the lab server (2026-07-06): (a) uv's default Python
location is inside the installing user's profile — every other account (the SSH
students) gets `uv trampoline failed to spawn Python child process`; (b) a bare
`--python 3.12` bakes uv's minor-version *junction* path into the venv trampoline,
and SSH logon sessions can fail to traverse those junctions — same error over SSH
while the console works. The explicit patch path sidesteps both and pins the Python
patch level, which is what a tagged-release server wants anyway (section 6 has the
symptom table).

**Windows (PowerShell)** — on the lab PC all three envs already exist under `D:\github`:

```powershell
cd D:\github

# view — data browsing, no instrument (the daily default)
uv venv .venv-view --python 3.12 --prompt view
uv pip install --python .venv-view\Scripts\python.exe -e ".\SCQO[viewer]" -e .\scqat datasette pytest httpx

# qblox — measurement env for the Qblox cluster
uv venv .venv-qblox --python 3.12 --prompt qblox
uv pip install --python .venv-qblox\Scripts\python.exe -e ".\SCQO[viewer]" -e .\scqat -e .\LCHQBDriver -e .\scqo-contrib datasette pytest httpx
uv pip install --python .venv-qblox\Scripts\python.exe "qblox-scheduler==1.0.0b4"   # exact hardware-proven build (see note)

# qm — measurement env for the OPX1000 (pinned, py3.11)
uv venv .venv-qm --python 3.11
uv pip install --python .venv-qm\Scripts\python.exe -r .\LCHQMDriver\requirements-qm.lock.txt
uv pip install --python .venv-qm\Scripts\python.exe -e .\scqat -e .\SCQO -e .\LCHQMDriver --no-deps

.venv-view\Scripts\Activate.ps1     # daily default — prompt shows (view)
```

(Each scqo install line also puts the **`scqo` command** on that venv's PATH — the
whole Tier-1 surface (`scqo run/find/accept/tag/state/user/device/doctor`)
works from any directory. `[viewer]` pulls the run-viewer's web extras —
fastapi/uvicorn/jinja2/python-multipart —
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
uv pip install --python .venv-view/bin/python -e "./SCQO[viewer]" -e ./scqat datasette pytest httpx
source .venv-view/bin/activate
```

(Add the qblox env — same lines as Windows with `/bin/python` paths — only if the
machine actually drives a cluster; finding/loading/viewing saved data never needs it.)

## 2. The lab config: `~\.scqo\config.toml`

Since v0.5.0 this file is TINY: it says where the data lives — everything else
follows the DEVICE. Which instrument a sample hangs on is a NAMED setup in the
sample's own cooldown registry (`<data_root>\<device>\cooldowns.toml`, below); ALL
folder locations are pure convention, DERIVED from the registry keys — no path key
anywhere: each (cooldown, setup) context has
`<data_root>\<device>\<cooldown>\<setup>\backend_config\` (the vendor config files)
and the sibling `...\<setup>\scqo\` (SCQO's `scqo_state.json` calibration +
`physical.json` measured physics; the sibling split keeps SCQO files out of the QM
backend's QUAM state-directory load). Create the
config at `~\.scqo\config.toml` (Windows: `C:\Users\<you>\.scqo\config.toml`;
macOS: `/Users/<you>/.scqo/config.toml`). Save it as **UTF-8 without BOM** —
Python's `tomllib` rejects UTF-16 and BOM'd files. Normal editors do this by
default; PowerShell 5.1's `Out-File`/`Set-Content -Encoding UTF8` writes a BOM, so
when scripting the file use `[IO.File]::WriteAllText($path, $text)` instead.

```toml
[lab]
data_root   = 'D:\qpu_data'      # all measurement data + registries land here
device      = "chipA"            # OPTIONAL lab-default sample (omit on multi-user servers)
default_tags = ["projX"]         # optional; stamped on every run
# state_sync = "pull"            # optional; real backends only ("simulated" always persists)
```

(macOS/Linux: `data_root = "~/qpu_data"` — `~` is expanded.)

**Two backend realities** (which one runs is decided by the device's current setup,
not by this file):

| setup `backend =` | device tree | data | notes |
|---|---|---|---|
| `"simulated"` | built-in demo qubits (q0/q1) | synthetic | the practice mode; state always persists |
| `"qblox"` / `"qm"` | real instrument | real | needs the driver repo's venv; QM keeps `state_sync="pull"` |

Optionally describe each sample in `<data_root>\devices.toml` (one table per chip:
description, design values — instrument-independent facts only); the viewer's Device
page shows the matching card. All samples share ONE `data_root` and ONE index —
filter with `--device` / the viewer's device dropdown.

**Moving a sample to the other instrument** (e.g. chipA from Qblox to the OPX1000)
needs **no data action at all** — the folder, index, history and trends follow the
sample name; runs before/after the move stay distinguishable by their `backend` and
setup era. The operator's checklist:

1. Record the change in chipA's cooldown registry: hand-add a new NAMED setup block
   to the ACTIVE cycle (a new insertion = a new cycle via `scqo device cooldown
   start`) — `[<cycle>.setup.qm_main]` with `backend = "qm"`, and create its DERIVED
   `<cycle>\qm_main\backend_config\` vendor folder. Users then switch with
   `scqo user --setup qm_main`.
2. Build the new vendor config as usual (wiring/attenuation are new-fridge facts).
   **Seed the frequencies from the sample's last known values**: open the viewer's
   Device page (it reads saved snapshots, so it works after the old instrument is
   disconnected) and copy `readout_freq` / `drive_freq` per qubit — these are sample
   properties and transfer well.
3. Do **NOT** transfer `pi_amp` / `readout_amp` — they encode the setup (line
   attenuation, output gain). Re-derive them with the standard bring-up runs on the
   new instrument (`scqo run resonator_spectroscopy` → `qubit_spectroscopy` →
   `qubit_power_rabi`, accepting each step).
4. Keep **qubit names identical across instruments** — `q1` must mean the same
   physical qubit in `dut_config.json` AND in the QUAM `state.json`. Names belong to
   the sample; different names would split its trends and history.

State persistence: `"simulated"` setups always persist (push is forced — an
in-memory demo device has no vendor truth to pull). Real backends default to
`state_sync = "pull"`: the vendor config is the truth at startup and scqo pushes
only values it freshly measures. On QM control PCs `"pull"` is mandatory — see
LCHQMDriver's CLAUDE.md.

Other notes:
- A temporary alternative config can be selected per shell
  (PowerShell: `$env:SCQO_CONFIG = "path\to\other.toml"`; bash/zsh:
  `export SCQO_CONFIG=path/to/other.toml`) or per command with `--config`.
- A mistyped `$SCQO_CONFIG` **fails loudly** — it will not silently run unsaved.

### Standing parameter defaults: `~\.scqo\parameters.toml`

A second, optional TOML file holds your **semi-permanent experiment settings** — the
knobs you would otherwise retype on every run. One top-level table per experiment
(names as shown by `scqo run`'s catalog listing); precedence is always
**code defaults < this file < whatever you pass on the CLI / in the caller dict**,
so the command line keeps the last word:

```toml
# Standing per-experiment defaults. Precedence: code defaults < this file < CLI/caller.
[resonator_spectroscopy]
frequency_span_hz = 15e6
num_points = 201

[single_shot_readout]
qubits = ["q1"]          # even required knobs may get a standing default
```

- With the file in place, a project's daily commands shrink to
  `scqo run resonator_spectroscopy`.
- `scqo run <experiment> --help` marks file-sourced values, e.g. `default=15e6 [parameters.toml]`.
- Working on several projects? Point `parameters_file` in `[lab]` — or in your
  personal `user.toml` (next subsection) — at a different file to swap the whole set.
- Same encoding rule as the config: **UTF-8 without BOM**.
- Failure rules match the config's: a *named* `parameters_file` that is missing, or a
  file that does not parse, **fails loudly** (this file changes what gets measured);
  only the implicit `~\.scqo\parameters.toml` may be absent — code defaults apply. A
  typo'd knob NAME inside a table is caught when that experiment runs, as a structured
  failure naming the key and this file. Tables for experiments not installed in the
  current env (e.g. contrib) load silently and are simply unused.
- Every run still records its **fully-resolved** parameters in `parameters.json`, so
  saved runs stay reproducible no matter which tier a value came from.
- TOML has no `null`: a knob whose code default is `None` (e.g. `readout_amplitude`)
  can be *set* here but not reset to `None` — override per run with
  `--set readout_amplitude=null` instead.
- Not supported (by design): per-qubit defaults — parameters are per-run scalars
  shared by every qubit in the run's list.

### Per-user overlay: `~\.scqo\user.toml`

On a multi-account server with ONE machine-wide shared config (`SCQO_CONFIG`), each
account may keep a small personal overlay — flat keys, no tables:

```toml
device = "chipA"                  # which SAMPLE I work on — the instrument follows it
setup = "qblox_main"              # which of its setups I measure with (only needed when
#                                 # the ACTIVE cycle has several — one auto-selects)
default_tags = ["projA"]          # appended to the shared tags, deduped
# parameters_file = "~/projB.toml"     # OPTIONAL — only to use a DIFFERENT file;
#                                      # your ~\.scqo\parameters.toml applies automatically
```

You pick the **sample** — and, when its ACTIVE cycle declares several measurement
setups, WHICH one you measure with. Everything behind a setup (which instrument,
where its config files live) is the manager's fact, recorded in the device's own
cooldown registry (next subsection). Note the relationship to the parameters section
above: your own `~\.scqo\parameters.toml` needs **no** line here — it is found
automatically. `parameters_file` exists for swapping in a different set (e.g. per
project) and then beats `[lab]`.

The `scqo user` command writes this file for you (hand-editing stays fine):
`scqo user --device chipA --setup qblox_main` validates both names against the
registries before writing (`.toml.bak` + re-parse guard — the file is never left
broken); `--clear-device` / `--clear-setup` remove a selection. Bare `scqo user` is
a pure diagnosis view (always exits 0): your selection, where it came from, and
exactly what a run would resolve to — or the precise `runs would refuse:` message.
With `$SCQO_USER_CONFIG=none` the overlay is disabled and `scqo user` refuses to
write.

Only these four keys are allowed — anything else (data_root, backend,
state_sync...) is machine wiring and fails loudly: a user cannot repoint where data
lands or which instrument a run drives. `setup` is user-overlay ONLY — a shared
`[lab]` default setup would silently steer every account's instrument, so there is
no such key. The overlay applies only on top of a FOUND base config, never to the
built-in defaults. `$SCQO_USER_CONFIG` selects a different overlay file, or disables
the overlay with `none` (scripts/tests use this). See which devices and setups exist
with `scqo device list` (touches no instrument).

### Registries: samples and cooldown cycles

Two hand-edited TOML files complete the lab picture. The principle:
**every fact lives at the level that owns it** — and every run is stamped with its
full environment (cycle id + setup name + operator + backend).

`<data_root>\devices.toml` — optional; one table per sample (instrument-independent
facts only: description, design values). The viewer's Device page shows the matching
card; display-only, a typo warns and is ignored.

`<data_root>\<device>\cooldowns.toml` — the device's cycle registry: one table per
cooldown (packaging is FIXED per cycle — you cannot repackage cold), with NAMED
`[<id>.setup.<name>]` sub-tables underneath. A **setup** is one whole measurement
arrangement of the cycle — which backend drives the sample and where that backend's
config files live — and its **name IS its identity**: stamped on every run, selected
with `scqo user --setup <name>`. One sub-table per arrangement that exists during
the cycle (the common case is exactly one; here the sample is wired to both
instruments at once):

```toml
[cd8]
start = 2026-07-06
fridge = "BlueforsA"
packaging = "PCB v3, Al box"
# end = 2026-08-01                # absent = this cycle is ACTIVE

[cd8.setup.qblox_main]
backend = "qblox"                  # qblox | qm | simulated

[cd8.setup.qm_highpower]           # the same sample, also wired to the OPX1000
backend = "qm"
note = "high-power readout line"
```

No path keys: the vendor-config folder is **derived from the keys** —
`<device>\cd8\<setup>\backend_config\`. For each real setup create that folder and
copy the vendor files in under canonical names; SCQO keeps its own
`scqo_state.json` + `physical.json` in the sibling `cd8\<setup>\scqo\`
(auto-created). Nothing can dangle: the TOML keys ARE the folder locations.

The rules (validated LOUDLY at run start — this file stamps runs AND selects the
instrument, so a broken one fails before any instrument time is spent):

- A setup table carries EXACTLY `backend` (required: `qblox` | `qm` | `simulated`)
  and an optional `note` — any other key is refused loudly. There are no `since`
  dates (the NAME is the identity), no port-map pairs (wiring lives in the vendor
  config files), and no `instrument_config` path (retired in v0.9 — see below).
- **The vendor-config folder is DERIVED, never typed**:
  `<device>\<cooldown>\<setup>\backend_config\`, holding ALL the vendor's config
  files under their **canonical names** — qblox: `dut_config.json` +
  `hw_config.json`; qm: `state.json` + `wiring.json`. Simulated setups have no
  folder. Writing `instrument_config` in the registry is refused loudly, naming
  the derived folder. (Consequences by construction: a path can never dangle when
  folders move, two setups can never share a vendor folder, and SCQO's sibling
  `scqo\` folder can never be swept up by the QM backend's QUAM load.)
- Starting a NEW cooldown = new folders: copy the vendor files into
  `<newcid>\<setup>\backend_config\` — each cycle keeps its own wiring snapshot,
  which is the registry's whole point.
- Setup names are letters/digits/`_`/`-` only (they travel as CLI arguments and
  index values) and are **immutable for the life of their cycle**: renaming one is
  declaring a NEW setup — the accept era guard and run provenance compare names, so
  a rename strands the old runs' era. Pick names you can keep (`qblox_main`,
  `qm_highpower`).
- **A cycle may have ZERO setups** — legal in the file, but runs refuse until the
  manager hand-adds a block (the refusal prints the exact block to paste).
- Selection: a **single-setup cycle auto-selects** — users do nothing. With several
  setups, runs refuse (listing the names) until each user picks theirs once:
  `scqo user --setup <name>` (personal, validated against the ACTIVE cycle).

Manage cycles with `scqo device cooldown` — works from any directory:

```
scqo device cooldown                   # validate + list cycles + the ACTIVE cycle's setups
scqo device cooldown start cd9 --fridge BlueforsA --packaging "PCB v3"
scqo device cooldown end               # close the open cycle (today's date)
```

`start` records the insertion as an EMPTY cycle (safe append — hand-written content
and comments stay untouched); the setup blocks are ALWAYS hand-added afterwards, one
per arrangement. `end` inserts today's date (`.toml.bak` + re-parse guard). Every
run is then auto-stamped with (cycle id, setup name) — query with
`scqo find --cooldown cd8 --setup qblox_main` (setup names are unique per cycle
only, so combine the two), filter in the viewer, and stop hand-editing a cooldown
tag into `default_tags`. After `scqo device cooldown end`, runs on that device
**refuse** until the next `scqo device cooldown start` — an ended cycle has no setup
to resolve.

**Upgrading to v0.6.0 (fresh-start policy, no migration) — READ THIS ONE, it
changes what a run DOES:**

- **Runs no longer write fitted values back.** They become *pending suggestions*:
  at a terminal `scqo run` shows the table and asks (press `a` to apply all —
  Enter applies NOTHING and the device stays unchanged); in scripts everything
  stays pending until `scqo accept <run_id>`. The pre-v0.6 behavior is one flag
  away: `scqo run ... --accept` (or `update="apply"` in Python).
- **T1/T2*/T2echo moved out of the device state.** `scqo state` no longer shows
  them — they are SAMPLE physics now, living in `<data_root>\<device>\physical.json`
  with their own change history: `scqo state --physical [--history]`. Legacy
  `t1_s`/`t2_star_s`/`t2_echo_s` keys in an old `scqo_state.json` are simply not
  read (`readout_fidelity` stays: it is a fact about qubit+setup).
- **Nothing to migrate:** the run index rebuilds itself (schema v6); pre-v0.6
  runs reindex with no suggestions. Find undecided runs any time with
  `scqo find --pending` or bare `scqo accept`.
- Changed your mind later? At a terminal `scqo accept <run_id>` simply ASKS
  ("re-apply (rollback)?", "apply anyway?" on an era mismatch, per-row stale
  overwrite questions — Enter always = No); `--reapply`/`--force` are the script
  form of those answers. See TUTORIAL §2.
- **Provenance is first-class:** `scqo state --sources` (and the viewer's
  `live:` markers / value-to-run links) show which run set each CURRENT value —
  strictly: values reseeded by the vendor or written by another tool show
  `(externally changed)` and credit no run. Additive, no data action.

**Upgrading to v0.7.0 (fresh-start policy, no migration): setups became NAMED,
and the commands regrouped.**

- **The registry format above replaces v0.6's.** The old `[[<id>.setup]]` array
  form is refused loudly (the message names the file and the new form); `since`
  dates and port-map pairs are refused as unknown keys — rewrite each block as a
  `[<id>.setup.<name>]` sub-table. (v0.7 blocks carried `backend`/
  `instrument_config`/`note`; since v0.9 `instrument_config` is DERIVED — write
  only `backend` [+ `note`], see the v0.9 note.) Wiring now lives ONLY in the
  vendor config folder.
- **Commands regrouped, no aliases:** the calibration view `scqo device` is now
  **`scqo state`** (flags unchanged: `--history`/`--physical`/`--sources`);
  `scqo devices` → **`scqo device list`** (or bare `scqo device`), `scqo sample
  new` → **`scqo device add`**, `scqo cooldown ...` → **`scqo device cooldown
  ...`**. NEW: **`scqo user`** shows/writes your device + setup selection;
  `user.toml` gains the `setup` key (§2 overlay subsection).
- `scqo device cooldown start` no longer takes `--backend`/`--instrument-config` —
  it opens an EMPTY cycle; setup blocks are always hand-added.
- **Nothing to migrate:** the run index rebuilds itself (schema v7); pre-v0.7 runs
  reindex with an empty setup era (the accept era guard treats them as a different
  era).
- **No reinstall needed:** the entry points are unchanged, so `git pull` suffices
  on a dev machine (tagged servers follow §5) — but old muscle memory
  (`scqo devices`, `scqo cooldown`, `scqo sample`) now prints `unknown command`.
  In the driver repos, the `device`/`devices`/`cooldown`/`sample` wrapper scripts
  are gone with NO replacements (use the `scqo` command: `scqo state` /
  `scqo device ...` / `scqo user ...`).

**Upgrading to v0.8.0 (fresh-start policy, no migration): absolute readout power.**

- **A fifth tracked field, `readout_power_dbm`** (absolute dBm at the instrument
  port), on every backend. Setting it re-solves the output chain (QM
  `full_scale_power_dbm` / Qblox `output_att`) keeping the digital amplitude
  ≤ 0.5 full scale — so `readout_amp` changes as a COUPLED side effect, which the
  history now records explicitly (`coupled_to` on the change record). The first
  write normalizes a legacy chain to that canonical form; pending `readout_amp`
  suggestions from before may go stale — truthfully.
- **New experiment `resonator_spectroscopy_power_chain`**: the CAREFUL punchout —
  it steps the output chain (QM `full_scale_power_dbm` / Qblox `output_att`) one
  power point at a time (amp held ~0.5 for SNR), so its dBm axis is wide and
  cross-backend comparable (slow: one compile+run per point). Proposes
  `readout_power_dbm` + `readout_freq`. See TUTORIAL §2 "Readout power — two modes".
- **Run records gain `power_context`** (raw chain values per qubit at run end) in
  `record.json` — provenance only; the index schema is UNCHANGED (v7, no reindex).
- **The FAST punchout is RENAMED**: `resonator_spectroscopy_power` →
  `resonator_spectroscopy_power_amp` (it sweeps power via the FPGA amplitude in one
  program; the chain-stepped sibling above is `_chain`). No alias: rename your
  `~\.scqo\parameters.toml` section header (`[resonator_spectroscopy_power]` →
  `[resonator_spectroscopy_power_amp]`); pre-rename runs stay findable under the
  old name (`scqo find --experiment resonator_spectroscopy_power`).
- **Both punchouts now take the SAME absolute-dBm inputs** —
  `min_power_dbm`/`max_power_dbm` (default −50…−20), same fields, same proposals
  (`readout_power_dbm` + `readout_freq`); only the mechanism differs and the figure
  labels it. `_amp` realizes the window by temporarily solving the chain for
  `max_power_dbm` (a recorded write, auto-reverted — the run leaves the device as
  found) and sweeping the amplitude down from it, so any window up to the chain max
  is reachable. Consequences: (a) the old relative keys `min_power_db`/`max_power_db`
  are GONE — a stale `[resonator_spectroscopy_power_amp]` parameters.toml entry
  fails loudly naming the file (`extra="forbid"`); rename the keys and rescale the
  values to absolute dBm. (b) A qubit whose `readout_power_dbm` is unknown now
  REFUSES to run (both punchouts; the old `readout_amp` fallback is gone) — set
  `readout_power_dbm` once, or fix `readout_amp`, first. It also gains the
  optional `resonator_relaxation_time_ns` parameter (ring-down wait between
  readouts) and both backends now acquire amplitude → averages → frequency (the
  frequency sweep is innermost/fastest, so the readout power only changes between
  slow outer steps).
- **No reinstall needed** (no entry-point changes): `git pull` suffices on dev
  machines; tagged servers follow §5. Old state files load as-is (the new field
  backfills from the vendor config on first load).

**Upgrading to v0.9.0 (fresh-start policy, no migration): state + physics are per
(cooldown, setup), in their own `scqo\` folders.** Two users on two setups of one
sample no longer share (or clobber) a file, and the on-disk tree mirrors
`device → cooldown → setup`.

- **The per-device `scqo_state.json` and device-level `physical.json` are RETIRED —
  simply not read.** Each (cooldown, setup) context has a
  `<data_root>\<device>\<cooldown>\<setup>\scqo\` folder holding its own
  `scqo_state.json` (calibration + history) and `physical.json` (measured physics +
  history). Delete old files at will; in pull mode calibration reseeds from the
  vendor config anyway. History starts fresh per context (rows carry `setup=`).
- **The `instrument_config` key is RETIRED — the vendor folder is DERIVED from the
  keys**: `<device>\<cooldown>\<setup>\backend_config\` (a setup table is just
  `backend` + optional `note`). Delete any `instrument_config` lines and keep the
  vendor files in the derived folder; a typed path is refused loudly naming it.
  SCQO never writes there — its own files live in the sibling
  `<cooldown>\<setup>\scqo\` — so the QM backend's QUAM state-directory load
  (which merges every loose `*.json` and rejects unknown keys) can never sweep up
  SCQO files, and no dangling path is possible. Setup names differing only by
  letter case are refused too (they are folder names now). Cooldown ids must be
  filename-safe for the same reason.
- **`physical.json` is per (cooldown, setup)** — a measured T1/T2 is contextual, so
  a noisy chain's number never overwrites a clean one's; they are simply different
  files. `scqo state --physical` shows THIS context's values (flat); compare across
  setups/cooldowns via `scqo find` or the viewer trends page (both stamp cooldown +
  setup). The setup-independent "true" physics stays the Phase-3 `sample.json`
  roll-up. Saves MERGE under a lock file, so two same-context sessions can't erase
  each other's rows.
- **`scqo state` prints a context header** (device / setup / cooldown / state
  file) so you always know whose numbers you're reading.
- **Nothing else to migrate:** the run index is unchanged (schema v7, no reindex);
  no entry-point changes — `git pull` suffices on dev machines, tagged servers
  follow §5. `scqo doctor` lists every setup's `scqo_state.json` path.

**Upgrading a dev machine (tracks `main`, editable installs):** `git pull` in every
repo is normally ALL it takes — code and new subcommands are picked up immediately.
Re-run the §1 `uv pip install -e` lines only when the release notes in
[RELEASES.toml](RELEASES.toml) say so (entry points and dependencies register at
install time — e.g. anything crossing v0.4.0); finish with `scqo doctor` either way.
Tagged servers follow the §5 procedure instead.

### Adding a new sample

Nothing shared to edit — a sample IS its data folder plus its own cooldown registry.
`scqo device add <name> [--description "..."]` (any directory) creates the data
folder and prints every remaining step paste-ready (it never edits shared files):

1. **Manager, at insertion**: record the first cycle —
   `scqo device cooldown start cd1 --fridge <name> --packaging <text>` (an EMPTY
   cycle) — then hand-add one `[cd1.setup.<name>]` block per measurement setup
   (just `backend` + optional `note`) to the new `cooldowns.toml`; for real
   backends create the DERIVED folder `cd1\<name>\backend_config\` and copy the
   vendor files in under the canonical names.
2. **Optional**: a `devices.toml` entry (description/design facts for the viewer).
3. **Users**: `scqo user --device <name>` — that is the whole selection (a
   single-setup cycle auto-selects; several → `scqo user --setup <name>`).
   Everything else auto-creates on first use: `<data_root>\<name>\` run folders,
   each context's `<cooldown>\<setup>\scqo\` folder, the index row, viewer pages.
   Verify with `scqo device list`.

### Cleaning state — from "just the index" to factory reset

Most "clean-up" needs are lighter than a factory reset. The ladder, mildest first:

1. **Rebuild the index only** (always safe — the folders are the truth): delete
   ALL `index.sqlite*` files (`-wal`/`-shm` siblings too) in the data_root, then
   `python -m scqo <data_root>`. Fixes a stale/corrupt index; loses nothing.
2. **Reset a context's state**: delete that (cooldown, setup)'s
   `<data_root>\<device>\<cooldown>\<setup>\scqo\scqo_state.json` — calibration
   reseeds from the vendor config at the next session (fresh-start rule; the change
   history in it is lost). **Do NOT reflexively delete `physical.json` next to it**:
   that file IS that context's measured-physics ledger (T1/T2, arch/dispersive fits +
   their history) and does NOT reseed from anywhere — delete it only if you truly
   mean to discard those measurements.
3. **Factory reset** — everything below.

### Factory reset (make a machine "new" again)

To reinstall as if on a clean computer, delete the machine's scqo state — repos and
venvs may stay (they are code, not state):

**Dev PC:** close any running viewer/datasette, then delete your per-user files and
your scratch data root; unset the env vars if you ever set them:

```powershell
Remove-Item -Recurse -Force "$env:USERPROFILE\.scqo"        # config/parameters/user toml
Remove-Item -Recurse -Force 'D:\local_test_data'            # your scratch data_root
Remove-Item Env:SCQO_CONFIG, Env:SCQO_USER_CONFIG -ErrorAction SilentlyContinue
```

Afterwards every script prints `# lab config: built-in defaults (simulated, nothing
saved)` — the new-machine state; continue with the Developer quickstart below.

**Lab server:** stop the viewer; delete the live data_root — **WARNING: the NAS
mirror propagates deletions on its next scheduled sync; copy anything you want to
keep OFF the NAS first** — then delete each account's `~\.scqo\` personal files.
Keep (or recreate) the machine-wide `SCQO_CONFIG` shared config file, `git fetch
--tags && git checkout <new tag>` in each repo, and per the fresh-start rule delete
the per-context `scqo\` folders (calibration reseeds from the vendor configs). Re-seed the registries
(`devices.toml`, per-device `cooldowns.toml` via `scqo device cooldown start` +
hand-added setup blocks) before the first measurement so runs are stamped from day
one.

### Developer quickstart (local, no hardware)

The whole system on your own machine in six steps — a Tier-2/3 dev deployment with a
SCRATCH data root (dev machines track `main`, keep their own data, and never point
writes at the server's data — §5):

1. **Clone side-by-side** into one folder (§1 block: SCQO, scqat, and the driver
   repo(s) you develop against).
2. **Create the venvs** (§1 commands): `view` always; `qblox`/`qm` only if you drive
   that stack — **then activate the one the rest of this quickstart runs in**
   (`.venv-qblox` also contains the full view stack, so it covers the pytest, the
   scripts and the viewer below):

   ```powershell
   D:\github\.venv-qblox\Scripts\Activate.ps1   # macOS/Linux: source ~/github/.venv-qblox/bin/activate
   ```

   The venvs live NEXT TO the repos (in their parent folder), not inside them — a
   bare `.venv-qblox\Scripts\Activate.ps1` only works from that parent folder, and
   from inside a repo PowerShell shows a misleading "cannot load module" error.
   Every later `python ...` in this quickstart assumes the prompt shows `(qblox)`.
3. **Write `~\.scqo\config.toml`** — smallest working dev config (UTF-8 no BOM):

   ```toml
   [lab]
   data_root = 'D:\qpu_data_dev'    # scratch, yours alone
   ```

   (With no config at all everything still runs — simulated, nothing saved.)
4. **Declare and select the practice sample, then start its first cycle** —
   required: with a device selected, every run must resolve a setup (which backend
   drives the sample) and be stamped with it:

   ```powershell
   scqo device add simdev                    # creates D:\qpu_data_dev\simdev + prints the steps
   scqo user --device simdev                 # your selection, written to ~\.scqo\user.toml
   scqo device cooldown start cd1 --fridge dev --packaging "sim"
   ```

   then hand-add the cycle's one setup to `D:\qpu_data_dev\simdev\cooldowns.toml`
   (any editor, UTF-8 no BOM; `simulated` needs no vendor folder):

   ```toml
   [cd1.setup.practice]
   backend = "simulated"
   ```

   A single-setup cycle auto-selects, so no `scqo user --setup` is needed. Every
   run is now stamped with (cycle, setup name) + operator, and `simulated` setups
   always persist their state (push is forced — §2).
5. **Verify offline**: `cd D:\github\SCQO; python -m pytest -q` (§3 — all green, no
   instrument).
6. **First run + look at it** (any directory — the `scqo` command needs no repo):

   ```powershell
   scqo doctor                             # health check — everyone's first move
   scqo device list                        # the menu — what can I select?
   scqo run resonator_spectroscopy         # first saved, stamped run
   scqo find --limit 5
   python -m scqo.viewer                   # -> http://127.0.0.1:8080
   ```

   From here, [TUTORIAL.md](TUTORIAL.md) is the daily manual.

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

Expected output: 5 numbered steps, each OK, ending in a `PASS - scqo works against
this real ...` line. The QM script skips qubits whose state is uncalibrated (fields
`None`) automatically; on the Qblox device non-`q*` elements like the coupler
(`c12`) are excluded by the `q*` default — pass `--qubits` to choose explicitly.
Both lab configs passed on 2026-07-04 (and this test caught three real integration
bugs before any hardware time was spent — that's its job).

## 5. Lab deployment — server + NAS + zero-install laptops

Target topology once the stack leaves the test bench:

```
instruments ── lab server (a normal PC; runs a TAGGED, stable version)
                 ├─ data_root on its LOCAL disk        <- ALL writes happen here
                 ├─ python -m scqo.viewer --host 0.0.0.0     (LAN browsing/tagging)
                 ├─ OpenSSH server                     (measure from any laptop)
                 └─ scheduled robocopy ──> \\NAS\qpu_data    (backup + analysis copy)

tier-1 laptop     browser -> http://<server>:8080 ; ssh <user>@<server> to measure
analysis laptop   view env (section 1) + own config.toml, data_root = the NAS copy
tier-2/3 dev PC   full local setup as in section 1 (simulated setups, contrib sandbox)
```

The rules that make this safe:

- **The live `index.sqlite` and run folders stay on the server's LOCAL disk** —
  SQLite's WAL mode does not work on network shares. The NAS holds a *mirror*
  refreshed by a scheduled task; the folders are the truth (that's what the backup
  protects) and the index rebuilds anywhere, so it doesn't even need mirroring.
- **One authoritative config per server** (the canonical `data_root`). With one
  login account per student, per-user `~\.scqo\config.toml` silently leaves every
  OTHER account on built-in defaults (simulated, unsaved!) — put the file at a shared
  path and select it machine-wide instead (admin, once):
  `[Environment]::SetEnvironmentVariable('SCQO_CONFIG','D:\github\scqo-config.toml','Machine')`.
  Personal configs exist only on analysis laptops and point `data_root` at the NAS
  copy — those machines read, never write. In the shared config, leave
  `parameters_file` **unset** so each account keeps its own standing defaults in
  `~\.scqo\parameters.toml` (section 2); setting it would pin ONE parameter set for
  every account — and a missing path fails loudly for all of them.
  **The sanctioned per-user layer on top of the shared config is `~\.scqo\user.toml`**
  (section 2): each account picks its own `device` (the sample), `setup` (when the
  ACTIVE cycle has several), `default_tags` and `parameters_file` there — and
  nothing else. `scqo user --device <name> [--setup <name>]` writes it, validated.
  **Recommended server policy: OMIT `device` from the shared config**, so an account
  that has not yet chosen a sample with `scqo user` gets the simulated demo
  (nothing saved) and cannot touch hardware by accident — `scqo device list` shows
  the menu and the exact command. On single-user dev machines, setting `device`
  directly in your own `config.toml` remains the normal, sufficient form.
- Simultaneous users are supported and tested (`tests/test_index_scale.py`), but
  **one measurement at a time per instrument** remains a social convention — the
  instruments themselves cannot run two programs at once.
- The server runs a **git tag** of all repos (first cut: `v0.1.0`, `git checkout
  v0.1.0` in each); dev machines track `main`. Update the server deliberately, after
  CI is green — never mid-cooldown on a whim. The update procedure:
  `git fetch --tags; git checkout <new tag>` in each repo, re-run section 3, restart
  the viewer (editable installs pick the new code up on restart). **Which tags belong
  together — and any REQUIRED upgrade action — is recorded per release in
  [RELEASES.toml](RELEASES.toml)** (process: [RELEASING.md](RELEASING.md)).
  **Upgrading across v0.4.0**: the `scqo` console command and the backend entry
  points register at INSTALL time, not import time — also re-run the section-1
  `uv pip install -e` lines for `.venv-view`, `.venv-qblox` and `.venv-qm`; finish
  with `scqo doctor` on a student account.
- **Dev machines (tier 2/3) keep their OWN scratch `data_root`** (e.g.
  `D:\qpu_data_dev`) — never point writes at the server's data over the network
  (the SQLite rule). Tier-2 prove-out runs on real hardware execute from the dev
  machine (the instruments are network devices) into the dev data_root, and the
  manager reviews them there (`find_runs` / a local viewer) before promotion. The
  one-program-per-instrument convention spans machines: coordinate with whoever is
  measuring via the server.
- Every run records **who** ran it (`operator` = the SSH/Windows login) — filter with
  `scqo find --operator <name>` or the viewer's operator box.

One-time server setup (Windows 11, **admin** PowerShell — the only part of this
guide that needs elevation; everything else runs as a standard user):

```powershell
# SSH access for tier-1 measuring (macOS/Linux/Windows laptops all have ssh built in)
Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0
Set-Service sshd -StartupType Automatic; Start-Service sshd

# let LAN laptops reach the viewer — without this, localhost works but laptops get
# connection-refused (the first-bind firewall popup needs elevation anyway)
New-NetFirewallRule -DisplayName "SCQO viewer 8080" -Direction Inbound -Action Allow `
  -Protocol TCP -LocalPort 8080
# add 8081 too if datasette (scqo.browse) should be reachable from laptops:
# New-NetFirewallRule -DisplayName "SCQO browse 8081" -Direction Inbound -Action Allow -Protocol TCP -LocalPort 8081

# make PowerShell the shell students land in (default is cmd.exe, where the
# venv Activate.ps1 scripts in TUTORIAL section 5 would not run)
New-ItemProperty -Path "HKLM:\SOFTWARE\OpenSSH" -Name DefaultShell `
  -Value "C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe" -PropertyType String -Force

# one STANDARD (non-admin) account per student — the login becomes the run's
# recorded operator, so no shared accounts
net user <student> <initial-password> /add /fullname:"Student Name"

# nightly data mirror to the NAS (= the lab's backup policy; /MIR mirrors deletions too)
schtasks /Create /TN scqo-mirror /SC DAILY /ST 03:00 `
  /TR "robocopy D:\qpu_data \\NAS\qpu_data /MIR /R:2 /W:5 /LOG:D:\qpu_data\mirror.log"
```

Mirror notes: robocopy exit codes 1–7 all mean success, so the task's "Last Result"
of 0x1 is normal, not a failure. A Synology **Drive Client** sync task is an accepted
alternative to robocopy — but **only as one-way upload** ("Upload data to Synology
Drive Server only") with files kept fully local (on-demand placeholders off): a
two-way task would let NAS-side edits and deletions flow back INTO the live
`data_root`.

First start of the viewer (standard user — no elevation needed):

```powershell
# a FRESH data_root has no index yet; on v0.1.0 create it first (newer versions
# initialize an existing-but-empty folder automatically):
D:\github\.venv-view\Scripts\python.exe -m scqo D:\qpu_data      # prints: indexed 0 runs

# run it now…
D:\github\.venv-view\Scripts\python.exe -m scqo.viewer --host 0.0.0.0

# …and keep it running across logons with a Startup-folder script (no admin needed;
# runs while this account has a session — fine for an always-logged-on lab server)
@'
@echo off
start "" /min D:\github\.venv-view\Scripts\python.exe -m scqo.viewer --host 0.0.0.0
'@ | Set-Content -Encoding Ascii "$env:APPDATA\Microsoft\Windows\Start Menu\Programs\Startup\scqo-viewer.cmd"
```

(Client side — what a student actually types from their laptop — is
[TUTORIAL.md §5](TUTORIAL.md); point new members there, not here.)

A student measuring from their own laptop, with nothing installed on it:

```
ssh <user>@<server>
D:\github\.venv-qblox\Scripts\Activate.ps1
scqo run resonator_spectroscopy --qubits q1
```

…then views the figures at `http://<server>:8080`.

## 6. Install troubleshooting

**First move, always: `scqo doctor`** — venv, drivers, config chain, registries, one
read-only command.

| Symptom | Cause / fix |
|---|---|
| runs succeed but `scqo state` never changes | v0.6.0 behavior: fitted values are SUGGESTED, not applied — decide at the run prompt, later via `scqo accept <run_id>`, or run with `--accept` (see the §2 v0.6.0 upgrade note) |
| `scqo accept` refused with "stale" / "already decided" | that's the SCRIPT (non-terminal) behavior — at a terminal it asks a [y/N] question instead (Enter = No); in scripts answer via `--force` / `--reapply` |
| the T1/T2 columns disappeared from `scqo state` | moved in v0.6.0: coherence times are sample physics now — `scqo state --physical` (`<device>\physical.json`) |
| `scqo devices` / `scqo cooldown` / `scqo sample` print `unknown command` | renamed in v0.7.0, no aliases: `scqo device list`, `scqo device cooldown`, `scqo device add`; the old `scqo device` view is `scqo state` (see the §2 v0.7.0 upgrade note) |
| `ModuleNotFoundError: scqo` | no venv activated — Windows: `.venv-view\Scripts\Activate.ps1`; macOS/Linux: `source .venv-view/bin/activate` |
| `scqo: command not found` / not recognized | no venv activated, or scqo upgraded across v0.4.0 without re-running the section-1 install line (the command registers at install time). `Get-Command scqo` shows which venv's command you're getting |
| viewer: `missing package: uvicorn` (or fastapi/jinja2), or a `python-multipart` RuntimeError | **wrong venv activated** — run the viewer from `(view)` or `(qblox)`. The `(.venv-qm)` lock env deliberately omits `python-multipart`, so the viewer's tag-edit route cannot start there |
| `device ... is on backend 'qblox' ... driver is not registered in this environment` | right command, wrong venv (the view env has no instrument drivers by design), or a stale editable install — the message distinguishes both and names the venv / the install line to re-run |
| `ModuleNotFoundError: lchqb` / `qblox_scheduler` from a run script | you're in the view env (by design it has no instrument libs) — activate `.venv-qblox` to measure |
| `lab config not found` | your `--config`/`$SCQO_CONFIG` path is wrong (intentional loud failure — better than silently unsaved) |
| `# lab config: built-in defaults ...` in the catalog header | no `~\.scqo\config.toml` yet: runs work but are **not saved** — do section 2. A personal `user.toml` does NOT rescue this: the overlay needs a base config |
| `... not allowed in a user overlay` | your `~\.scqo\user.toml` sets a machine-wiring key — only `device` / `setup` / `default_tags` / `parameters_file` are personal (section 2) |
| `invalid cooldown registry ...` (corrupt TOML) or `<path>\cooldowns.toml: ...` at run start | `cooldowns.toml` is broken (unparseable file, two open cycles, a non-filename-safe cooldown id, the retired `[[<id>.setup]]` array form — setups are NAMED `[<id>.setup.<name>]` sub-tables since v0.7.0, an unknown setup key — `since`/port maps and `instrument_config` are retired, allowed keys are exactly `backend`/`note`, casefold-twin setup names...) — it stamps runs AND selects the instrument, so it fails BEFORE instrument time is spent; `scqo device cooldown` (no args) is the validator |
| `device ... has no cooldown registry yet` / `no ACTIVE cooldown cycle` at run start | intentional refusal: with a device selected, every run must resolve a setup — the message names the exact `scqo device cooldown start` line. The same refusal appears after `scqo device cooldown end` until the next cycle starts |
| `cycle ... has no setups yet — runs need one` | the cycle was started empty (that is normal): the manager hand-adds a `[<cycle>.setup.<name>]` block (backend [+ note]) to the device's `cooldowns.toml` and, for real backends, creates the derived `<cycle>\<name>\backend_config\` folder — the refusal prints the exact block to paste |
| `cycle ... has N setups and none is selected` | the ACTIVE cycle offers several measurement setups and runs will not guess — pick yours once: `scqo user --setup <name>` (personal; a single-setup cycle needs no selection) |
| `setup 'x' ... does not exist in the ACTIVE cycle` | stale selection — typically an old name after a new cycle started. `scqo user --setup <name>` picks a current one (bare `scqo user` lists what a run resolves to); `scqo user --clear-setup` returns to auto-selection |
| self-test: `missing package: qblox_scheduler` | install the driver into this env (section 1, second install line) |
| `Repository not found` when cloning | the repo is (still) private and the active GitHub credential cannot see it — GitHub reports 404, not 403, to unauthorized users; sign in with an account that has access |
| self-test: `Unexpected attribute 'lo_mode'` / `'__package_versions__'` (or similar) | the vendor state file was written by a NEWER quam/vendor lib than this env's pin — delete the unknown null attributes from the **working copy** (originals untouched), or bump the lock deliberately after re-validation |
| viewer: `no index.sqlite under <data_root>` (v0.1.0) or `data_root does not exist` | fresh data_root: on v0.1.0 create the index first with `python -m scqo <data_root>`; newer versions initialize an existing folder automatically and only refuse a path that does not exist (typo guard) |
| viewer works at `http://localhost:8080` but LAN laptops get connection-refused | missing inbound firewall rule — run the `New-NetFirewallRule` line in section 5 (one-time, admin) |
| `uv trampoline failed to spawn Python child process` / `entity not found` — for every account except the installer | uv's Pythons live in the installing user's profile — reinstall to a shared dir (`UV_PYTHON_INSTALL_DIR`) and recreate the venvs (section 1 SSH-server note) |
| same trampoline error, but ONLY in SSH sessions (console works) | the venv bakes uv's minor-version junction path in, and SSH logon sessions may not traverse junctions — recreate the venv with the explicit patch-dir interpreter (section 1 SSH-server note) |
| over SSH a student's catalog header says `built-in defaults` (runs not saved) | that account has no per-user config — set the machine-wide `SCQO_CONFIG` variable to the shared config (section 5) |

Setup done → hand the machine to the student and point them at
[TUTORIAL.md](TUTORIAL.md).
