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
uv pip install --python .venv-view/bin/python -e "./SCQO[viewer]" -e ./scqat datasette pytest httpx
source .venv-view/bin/activate
```

(Add the qblox env — same lines as Windows with `/bin/python` paths — only if the
machine actually drives a cluster; finding/loading/viewing saved data never needs it.)

## 2. The lab config: `~\.scqo\config.toml`

This one small file tells every script where data goes, which device you are on,
and which backend runs. Create it at `~\.scqo\config.toml` (Windows:
`C:\Users\<you>\.scqo\config.toml`; macOS: `/Users/<you>/.scqo/config.toml`).
Save it as **UTF-8 without BOM** — Python's `tomllib` rejects UTF-16 and BOM'd
files. Normal editors do this by default; PowerShell 5.1's `Out-File`/`Set-Content
-Encoding UTF8` writes a BOM, so when scripting the file use
`[IO.File]::WriteAllText($path, $text)` instead.

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

**QUAM state authority (QM):** `[qm] state_dir` names THE state a session uses in
**both** `qm_sim` and real `qm` modes (since v0.1.2 — before that, real mode silently
resolved through `~/.qualibrate`, the hidden second authority that broke the server's
first real run). A machine measuring the real instrument points it at the LIVE state
folder; a tier-2 dev machine doing a real prove-out points it at a **fresh copy of
the server's live state** (scp it over first) — `state_sync = "pull"` keeps
writebacks inside the copy, so the canonical state is never touched.

Optionally describe each sample in `<data_root>\devices.toml` (one table per chip:
description, design values, where it is mounted — instrument-independent facts only);
the viewer's Device page shows the matching card. All samples share ONE `data_root`
and ONE index — filter with `--device` / the viewer's device dropdown.

**Moving a sample to the other instrument** (e.g. chipA from Qblox to the OPX1000)
needs **no data action at all** — the folder, index, history and trends follow the
sample name; runs before/after the move stay distinguishable by their `backend`.
The operator's checklist:

1. Config: carry the sample in the NEW instrument's table (`[qm] device_name =
   "chipA"` + its `state_path`), set `backend = "qm"`; update `mounted_on` in
   `devices.toml`; new fridge insert = new cooldown → edit `default_tags`.
2. Build the new vendor config as usual (wiring/attenuation are new-fridge facts).
   **Seed the frequencies from the sample's last known values**: open the viewer's
   Device page (it reads saved snapshots, so it works after the old instrument is
   disconnected) and copy `readout_freq` / `drive_freq` per qubit — these are sample
   properties and transfer well.
3. Do **NOT** transfer `pi_amp` / `readout_amp` — they encode the setup (line
   attenuation, output gain). Re-derive them with the standard bring-up:
   `python scripts\calibrate.py` on the new instrument.
4. Keep **qubit names identical across instruments** — `q1` must mean the same
   physical qubit in `dut_config.json` AND in the QUAM `state.json`. Names belong to
   the sample; different names would split its trends and history.

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

### Standing parameter defaults: `~\.scqo\parameters.toml`

A second, optional TOML file holds your **semi-permanent experiment settings** — the
knobs you would otherwise retype on every run. One top-level table per experiment
(names as shown by `run_experiment.py`'s catalog listing); precedence is always
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
  `python scripts\run_experiment.py resonator_spectroscopy` — and
  `calibrate.py` runs every sequence step with these effective defaults too.
- `--help` on any launcher marks file-sourced values, e.g. `default=15e6 [parameters.toml]`.
- Working on several projects? Point `parameters_file` in `[lab]` at a different
  file to swap the whole set. Two samples on two instruments? Each vendor table
  (`[qblox]`/`[qm]`) may override `parameters_file` with that sample's own set —
  exactly like `device_name`/`state_path` above.
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
tier-2/3 dev PC   full local setup as in section 1 (sim/twin backends, contrib sandbox)
```

The rules that make this safe:

- **The live `index.sqlite` and run folders stay on the server's LOCAL disk** —
  SQLite's WAL mode does not work on network shares. The NAS holds a *mirror*
  refreshed by a scheduled task; the folders are the truth (that's what the backup
  protects) and the index rebuilds anywhere, so it doesn't even need mirroring.
- **One authoritative config per server** (instrument → sample mapping). With one
  login account per student, per-user `~\.scqo\config.toml` silently leaves every
  OTHER account on built-in defaults (simulated, unsaved!) — put the file at a shared
  path and select it machine-wide instead (admin, once):
  `[Environment]::SetEnvironmentVariable('SCQO_CONFIG','D:\github\scqo-config.toml','Machine')`.
  Personal configs exist only on analysis laptops and point `data_root` at the NAS
  copy — those machines read, never write. In the shared config, leave
  `parameters_file` **unset** so each account keeps its own standing defaults in
  `~\.scqo\parameters.toml` (section 2); setting it would pin ONE parameter set for
  every account — and a missing path fails loudly for all of them.
- Simultaneous users are supported and tested (`tests/test_index_scale.py`), but
  **one measurement at a time per instrument** remains a social convention — the
  instruments themselves cannot run two programs at once.
- The server runs a **git tag** of all repos (first cut: `v0.1.0`, `git checkout
  v0.1.0` in each); dev machines track `main`. Update the server deliberately, after
  CI is green — never mid-cooldown on a whim. The update procedure:
  `git fetch --tags; git checkout <new tag>` in each repo, re-run section 3, restart
  the viewer (editable installs pick the new code up on restart).
- **Dev machines (tier 2/3) keep their OWN scratch `data_root`** (e.g.
  `D:\qpu_data_dev`) — never point writes at the server's data over the network
  (the SQLite rule). Tier-2 prove-out runs on real hardware execute from the dev
  machine (the instruments are network devices) into the dev data_root, and the
  manager reviews them there (`find_runs` / a local viewer) before promotion. The
  one-program-per-instrument convention spans machines: coordinate with whoever is
  measuring via the server.
- Every run records **who** ran it (`operator` = the SSH/Windows login) — filter with
  `find_runs.py --operator <name>` or the viewer's operator box.

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
python D:\github\LCHQBDriver\scripts\run_experiment.py resonator_spectroscopy --qubits q1
```

…then views the figures at `http://<server>:8080`.

## 6. Install troubleshooting

| Symptom | Cause / fix |
|---|---|
| `ModuleNotFoundError: scqo` | no venv activated — Windows: `.venv-view\Scripts\Activate.ps1`; macOS/Linux: `source .venv-view/bin/activate` |
| viewer: `missing package: uvicorn` (or fastapi/jinja2) | **wrong venv activated** — the viewer lives in every section-1 env; check the prompt says `(view)`, `(qblox)` or `(.venv-qm)`, not something stale |
| `ModuleNotFoundError: lchqb` / `qblox_scheduler` from a run script | you're in the view env (by design it has no instrument libs) — activate `.venv-qblox` to measure |
| `lab config not found` | your `--config`/`$SCQO_CONFIG` path is wrong (intentional loud failure — better than silently unsaved) |
| `# lab config: built-in defaults ...` in the catalog header | no `~\.scqo\config.toml` yet: runs work but are **not saved** — do section 2 |
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
