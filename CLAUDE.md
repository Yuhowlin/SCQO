# SCQO — Superconducting Qubit Orchestration (instrument-agnostic)

## Why this repo exists
Run superconducting-qubit calibration experiments at the level of **experiment + parameters**, independent of
the instrument backend. Two existing lab repos do the same physics on different hardware; SCQO is the
neutral layer above them, and the substrate for **AI-driven experiment loops** (decide approach + params →
run → estimate → extract → decide next).

## Terminology (canonical vocabulary — single source of truth)
The word **"protocol" is retired**; use these names across all repos.

- **Experiment** — the registered, instrument-agnostic unit SCQO catalogs and dispatches to a backend (QM or Qblox). Owns its **Parameters**; binds a probe + an estimator.
- **probe** — the acquisition half: build the instrument sequence (QM program / Qblox schedule) and run it → **Dataset** (xarray). On the simulated backend the probe runs the **model** forward to synthesize data ("simulation = virtual experiment").
- **estimator** — the analysis half: fit the Dataset to a **model** → **Result** (extracted model parameters). Implemented in scqat (`scqat.estimators`); its orchestrator method is `analyze()`.
- **tool** / **fitter** — reusable helpers an estimator imports (`scqat.tools`); a fitter is the common case. Many-to-many; **tools never import estimators**.
- **model** — the physics that predicts the signal; used *forward* by a simulated probe and *inverse* by an estimator. SCQ.jl builds/simulates models; scqat fits them.
- **Parameters / Result / Backend / Session** — input schema / extracted output / instrument adapter (QM, Qblox, Simulated) / the orchestrator entry point (`catalog()` / `run()` / `device_state()`).

**Naming status (2026-06-08):** the scqo stack is fully migrated to this vocabulary — **scqat** (`estimators/`, `tools/`, `BaseEstimator`), **SCQO** (`Experiment`, `scqo.experiments`, `probe()`, `estimate()`), and **LCHQBDriver** (`probe()`-only experiments). scqat's estimator keeps its own orchestrator method `analyze()` (a different layer). LCHQMDriver still uses qualibrate's own `node` framework. (QBLOX_training documents Qblox's *own* `Experiment` ABC — a different class from this `Experiment`.)

## The two source repos (reference implementations)

| | LCHQMDriver | QBLOX_training |
|---|---|---|
| Instrument | Quantum Machines OPX1000 (MW-FEM + LF-FEM) | Qblox Cluster (QCM / QCM-RF / QRM-RF) |
| Low-level API | `qm-qua` (QUA DSL) | `qblox_scheduler` (`Schedule` + `Operations`) |
| Device model | QUAM — `Quam(FluxTunableQuam)`; qubit = `.xy/.z/.resonator`; param e.g. `q.f_01` | `QuantumDevice` + `BasicTransmonElement`/`FluxTunableTransmonElement`; param e.g. `q.clock_freqs.f01` |
| Experiment framework | `qualibrate` `QualibrationNode` + `@node.run_action` + web GUI | hand-rolled `Experiment` ABC, notebook-driven, no GUI |
| Parameters | `NodeParameters` (pydantic, mixin inheritance, validated) | positional kwargs to `execute(...)`, no schema |
| Pulse DSL | `qubit.xy.play("x180")` (QUAM macros) | `X(qubit)`, `Measure(...)` (scheduler operations) |
| Sweep | QUA `for_` loops, xarray `sweep_axes` | `Schedule.loop(linspace/arange)` |
| Data out | `XarrayDataFetcher` → `xarray.Dataset` | `hw_agent.run()` → `xarray.Dataset` |
| State writeback | `node.record_state_updates(): q.f_01 -= …` | `post_run(): q.clock_freqs.readout = fr` |
| Persistence | `quam_state/*.json` | `dut_config_*.json` |

### What already converges (build on these)
- Both emit an **`xarray.Dataset`** as the canonical data format.
- Both split **experiment parameters** (the sweep) from **device state** (qubit config persisted to JSON).
- Both follow the same lifecycle: **build sweep → run on HW → analyze/fit → write results back to device → persist.**

### Where they diverge (what the neutral layer must absorb)
1. Parameter declaration: rich pydantic schema vs bare kwargs.
2. Experiment framework: real framework + GUI vs thin ABC.
3. Pulse/sweep DSL: QUAM macros vs scheduler operations.
4. Device-model attribute names: `q.f_01` / `q.xy.RF_frequency` vs `q.clock_freqs.f01` / `q.clock_freqs.readout`.

## Target architecture (AI-drivable, backend-neutral)
Adopt qualibrate's *patterns*, generalized so QM and Qblox are adapters:

- **Parameters**: pydantic schema per experiment (introspectable: names, types, ranges, defaults, docstrings).
- **Experiment registry**: named, described catalog of measurement approaches (the AI's decision menu).
- **Experiment lifecycle**: `probe → run → estimate → update` (neutral; a driver implements `probe`, the backend runs it).
- **Structured Result + Outcome**: machine-readable extracted quantities + success flags (not just figures).
- **Device model adapter**: neutral parameter names mapped onto QUAM vs QuantumDevice attributes.
- **State + history**: persistent device state and run history so an AI loop has memory.

AI loop surface:
`registry + Parameters schema (decide)` → backend adapter (run) → `structured Result (extract)` →
device-state update + history → next decision.

## Source repos on disk (read-only references)
- `D:\github\LCHQMDriver` — QM/qualibrate reference; see `calibrations/LCH_*.py`,
  `customized/node/*/parameters.py`, `quam_config/my_quam.py`.
- `D:\github\QBLOX_training` — Qblox reference; see
  `docs/applications/superconducting/single_qubit_experiment_helpers/experiment.py`, `cal*.py`,
  `custom_elements.py`.

## Package layout (scaffolded)

```
scqo/
  parameters.py   # Parameters base + QubitSelection / AveragingParameters mixins (decision surface)
  result.py       # Outcome enum + Result base (extraction surface)
  device.py       # QubitView / DeviceModel ABCs (neutral field names)
  physical.py     # PHYSICAL_FIELDS + PhysicalStore: the SAMPLE's instrument-independent
                  #   measured physics (T1/T2, arch/dispersive fits) -> <device>/physical.json
  suggestions.py  # Suggestion + SuggestionCapture: update() writes become PENDING
                  #   proposals on the run record; accept/reject decide them (v0.6)
  backend.py      # Backend ABC: .device + .acquire(experiment) -> xarray.Dataset
  experiment.py   # Experiment ABC: physics half (define_sweep/simulate/estimate/update) + backend half (probe)
  registry.py     # @register / get / catalog  (AI's menu of measurements)
  session.py      # Session: catalog() / run() / accept() / reject() / find_runs() /
                  #   load_run() / device_state() / physical_state() / history()
  datastore.py    # DataStore + RunRecord: every run saved to a folder, indexed in SQLite (rebuildable)
  labconfig.py    # ~/.scqo/config.toml -> LabConfig + make_session (students never edit repos)
  testing.py      # InMemoryDevice + SimulatedBackend (run with no instrument)
  cli/            # the `scqo` command (run/find/accept/tag/state/user/
                  #   device/doctor): ONE engine, any-directory;
                  #   the device's SELECTED named setup picks the backend, resolved via
                  #   the scqo.backends entry-point group; simulated is built in
                  #   (_backends.ensure_demo_experiments fills the catalog driver-less)
  experiments/
    resonator_spectroscopy.py   # frequency sweep, Lorentzian dip -> updates readout_freq
    qubit_spectroscopy.py       # two-tone peak search -> coarse drive_freq (bring-up step 2)
    qubit_ramsey.py             # time sweep, decaying-cosine fit -> updates drive_freq + T2*
    qubit_power_rabi.py         # amplitude sweep, cosine fit -> updates pi_amp
    qubit_relaxation.py         # pi + swept wait, exp-decay fit -> t1_s (physical store)
    qubit_echo.py               # Hahn echo, exp-envelope fit -> t2_echo_s (physical store)
    qubit_spectroscopy_flux.py  # 2D flux x detuning arch -> sweet spot / Ej_sum (physical store; Phase-3 feeder)
    single_shot_readout.py      # per-shot IQ blobs (prepared_state x shot_idx) -> readout fidelity
    resonator_spectroscopy_flux.py   # 2D resonator flux map -> sweet spot / dv_phi0 / f_r0 / g (physical store)
    readout_power.py            # per-shot fidelity vs amp prefactor -> updates readout_amp
    readout_frequency.py        # per-shot fidelity vs readout detuning -> updates readout_freq
    resonator_spectroscopy_power.py  # 2D punchout (detuning x power_db) -> readout_amp + readout_freq
tests/test_end_to_end.py        # catalog -> run -> writeback, no hardware
tests/test_datastore.py         # run folders + index + tags + reindex, no hardware
```

### Datastore (the "find my measurement data" layer)
`Session(backend, data_root=...)` persists **every** run — raw dataset (`dataset.nc`),
parameters/result/record JSONs, device before/after snapshots, and the scqat artifacts
(metadata / plotdata / figure PNGs, per qubit) — under
`<data_root>/<device>/<YYYY-MM-DD>/<run_id>/`. The **run folder is the truth**;
`<data_root>/index.sqlite` is a disposable cache (`python -m scqo <data_root>`
rebuilds it). Query with `Session.find_runs(experiment=, qubit=, tag=, since=, outcome=,...)`,
reload with `load_run(run_id)` / `datastore.open_dataset(run_id)`. Runs carry searchable
**tags** (`run(..., tags=[...])`, config `default_tags`, retroactive `tag_run`). Change
history records the `run_id` that caused each device update. State authority:
`state_sync="pull"` (default) seeds from the vendor at startup (safe when another tool also
calibrates, e.g. qualibrate on QM); `"push"` restores the saved SCQO config into the vendor
(only for devices SCQO fully owns).

**Multi-device rule (decided 2026-07-05):** the device = the physical SAMPLE (chip),
never the instrument; the instrument is provenance (every run/fit stamps `backend`).
ONE data_root + ONE index for all samples (`find_runs(device=...)` / `--device` filter;
per-sample DBs are rejected). Each user selects the sample and setup (`device`/`setup`
in user.toml; `scqo user`); which instrument carries it — and where its vendor config
folder lives — is a fact of the SELECTED named setup of the device's ACTIVE cooldown
cycle (`[<cycle>.setup.<name>]` in its cooldowns.toml, v0.7.0), never a config key.
Instrument-independent sample facts live in the optional human-edited registry
`<data_root>/devices.toml` (`datastore.load_device_registry`; rendered by the viewer).
Instrument-DEPENDENT measured values (thermal population etc.) stay in run records with
backend provenance — compare across instruments by query, never average them away.
Sample-level inferred physics (`sample.json` per device folder) is Phase-3 output.
Moving a sample between instruments needs NO data action (folder/history/trends follow
the sample; eras distinguish by backend) — procedure in INSTALL.md §2. Rule: qubit
names belong to the SAMPLE and must be identical in every vendor config ("q1" = the
same physical qubit on both instruments), or its trends and history split.
Scale/concurrency (tests/test_index_scale.py): device-scoped pages are O(limit) via
the composite index — fast at 100k+ runs/sample, unaffected by neighbors; only
UNSCOPED JSON tag/qubit filters scan lab-wide totals. Simultaneous same-PC sessions
(two students, two samples) are safe (WAL + busy retry; folder written before index,
so reindex heals any skipped write); multi-PC writers need per-PC data_roots.
Deployment split (2026-07-05, INSTALL.md §5): the lab SERVER runs tagged releases
(v0.1.0+) and owns the canonical data_root; dev machines track main with their OWN
scratch data_root — never write to the server's data over the network.

### How a driver adds an experiment
1. Subclass the backend-free experiment from `scqo.experiments`.
2. Implement only `probe()` for the instrument (lazy-import the vendor lib inside it).
3. `@register` the subclass so it appears in `catalog()`.
Parameters, Result, `estimate`, `simulate` and `update` are inherited unchanged.

### Experiment governance (3 tiers) + promotion checklist
1. **Students** use the `scqo` command (`scqo run` / `scqo find` / `scqo user`) with
   `~/.scqo/config.toml`; they change nothing in the governed repos.
2. **Advanced users** prototype new experiments + estimators in the sandbox repo
   `D:\github\scqo-contrib` (github.com/shiau109/scqo-contrib; entry-point group
   `scqo.experiments.contrib`, tagged `maturity: contrib` in the catalog; template:
   `qubit_relaxation`). Contrib runs persist to the same datastore, so prove-out is evaluable.
3. **The manager promotes** a proven experiment into the system. Checklist:
   - [ ] `DatasetContract` declared; probe output validated against it on the real instrument.
   - [ ] `simulate()` implemented -> offline end-to-end test in `tests/`.
   - [ ] Estimator lives in scqat with metadata (+ figures) outputs.
   - [ ] `update()` writes only neutral tracked fields (extend the field list first if needed).
   - [ ] Ran repeatedly via contrib with findable data; results reviewed via `find_runs`.
   - [ ] `description` is catalog-quality (an AI reads it to decide).
   - [ ] Physics half moved to `scqo/experiments/`; driver `probe()` subclasses registered
         under the core `scqo.experiments` group; contrib copy deleted. (It is then
         directly runnable via `scqo run <name>` — launcher stubs were retired in v0.7.0.)

### Reference backends
- `D:\github\LCHQMDriver` — Quantum Machines (qm-qua / quam / qualibrate).
- `D:\github\LCHQBDriver` — Qblox (qblox-scheduler). Independent of the QM stack.

## Status
Core scaffolded and tested offline via `SimulatedBackend`. Three worked experiments prove
the pattern across all three sweep types and device fields:
frequency->`readout_freq` (resonator spec), time->`drive_freq`+T2* (Ramsey),
amplitude->`pi_amp` (power Rabi). **Both real backends now exist**: the Qblox backend
(`LCHQBDriver`) and the QM backend (`LCHQMDriver/customized/scqo/`) implement `probe()`
against the same experiments. Drivers are discovered automatically via the
`scqo.experiments` entry-point group (no manual import needed); `Session.run` returns
structured failures (never raises across the JSON boundary) and writes back per
successful qubit. More experiments follow the same pattern.

**2026-07-04 — data layer landed:** every `Session.run` with a `data_root` persists the
full run (dataset + params + result + device snapshots + scqat figures) to
`<data_root>/<device>/<date>/<run_id>/`, indexed in a rebuildable `index.sqlite` with
searchable tags; `find_runs`/`load_run`/`tag_run` complete the Session surface, and
change history links each writeback to its `run_id`. Lab config (`~/.scqo/config.toml`,
`scqo.labconfig`) drives the student scripts in both driver repos. `state_sync="pull"`
is the default (QM stays pull until qualibrate migration completes).

**2026-07-05 — first Tier-3 promotions + 2D sweeps:** `t1_relaxation` promoted from
scqo-contrib (first full sandbox->promotion exercise) and `resonator_spectroscopy_power`
promoted from the QM qualibrate path. The stack now supports **multi-axis sweeps**
(`DatasetContract.sweeps` tuple; N-D `_to_canonical` in both drivers) and a fourth
tracked field **`readout_amp`** (readout pulse amplitude; QM: within the current
output-power config — FEM-gain reconfiguration stays with the qualibrate power node).

**2026-07-06 — rename:** `t1_relaxation` -> `qubit_relaxation` and `t2_echo` ->
`qubit_echo` (files, classes, registered names, scqat estimators + artifact filenames),
aligning with the `qubit_*` convention. No alias: runs recorded before the rename stay
findable only under the old names (`find_runs(experiment="t1_relaxation")`).

**2026-07-06 — standing parameter defaults (v0.2.0):** optional `~/.scqo/parameters.toml`
(one `[experiment]` table each; `parameters_file` in `[lab]` or a vendor table swaps
sets) merged in `Session.run` — precedence code defaults < file < caller; wired via
`LabConfig.parameter_defaults`/`make_session` like `default_tags`. `Session.catalog()`
overlays effective defaults (`x-default-source`, file-supplied keys dropped from
`required`); params `ValidationError` is now a structured failure (not raised, not
persisted) that names the defaults file for file-sourced typos. Driver `_cli.py`
(mirrored) marks file defaults in `--help`, prints applied-defaults provenance to
stderr, and only falls back to all-device qubits when neither CLI nor file names them.
Docs: INSTALL §2 subsection + TUTORIAL §2 three-tier parameters.

**2026-07-06 — punchout sim/estimator coupling:** the |IQ|-with-drive scaling in the
punchout `simulate()` (`586af0e`) requires scqat **tag v0.1.4+**, whose punchout
estimator baseline-normalizes the cross-power dip-amplitude outlier test (scqat
`83a8cd9`). With older scqat the seeded e2e test deterministically fails
(`optimal_power_db` -5.5 instead of -14.5) — that was the 2026-07-06 CI/local red
window, **not** a numpy-version effect (verified identical on numpy 2.3.1 and 2.5.1).
The floor is a comment in pyproject, not a `>=` pin: scqat's package metadata is stuck
at 0.1.0 across its tags, so a version pin would break every install until scqat
bumps `pyproject.version` at release time.

**2026-07-06 — multi-user server model (v0.3.0, fresh-start: no data migration).**
Principles: every fact lives at the level that owns it; every run carries its full
provenance (operator + backend + cycle + wiring era). Landed in six phases:
- **Per-user overlay `~/.scqo/user.toml`** over the machine-wide shared config —
  allowed keys ONLY `backend` (sample follows instrument via vendor-table
  re-resolution) / `default_tags` (merged, deduped) / `parameters_file` (user >
  vendor > lab); `$SCQO_USER_CONFIG` selects/disables (`none` = hermetic tests);
  applies only on top of a found base config. `LabConfig.user_source` provenance.
- **Operator on ChangeRecord** — stamped inside `RecordingDevice` writes (manual
  notebook writes attributed too); `_current_operator` moved datastore→config.
- **Field-descriptor table** `config.FIELDS: dict[str, FieldSpec(unit, doc, push)]`
  (TRACKED_FIELDS dropped; nothing imported it). Record-only measured physics —
  `t1_s`, `t2_star_s`, `t2_echo_s`, `readout_fidelity` (`p_e_given_g` stays run-only)
  — recorded to state+history, NEVER pushed to the vendor; needs a FIELDS entry and
  NOTHING else (ABC + drivers untouched). Pull-seed now MERGES saved record-only
  values (else every restart erased them — QM forces pull); push-load pushes only
  `PUSHED_FIELDS`. qubit_relaxation/echo/single_shot record; ramsey pushes
  drive_freq + records t2_star_s. `updated_device` covers record-only runs.
- **Registries** (hand-edited TOML in data_root): `instruments.toml` (connection
  facts; display-only loader) and per-device `cooldowns.toml` — device → cycle
  (packaging fixed) → dated FULL wiring snapshots ([[id.mapping]], `since` required;
  any port change = new snapshot). LOUD validation (it stamps runs) at run START.
- **Run stamping (index schema v4, auto-reindex):** `RunRecord.cooldown` +
  `wiring_since`; `find_runs(cooldown=...)`; viewer: runs cooldown filter/column,
  device page cycle+wiring panel + instrument cards, stable state columns
  (descriptor order — fields are heterogeneous per qubit now), history operator
  column; TREND_QUANTITIES derived from FIELDS.
- **Mirrored scripts** (grew to TEN shared files): NEW `cooldown.py` (validate/list;
  `start` append-only; `end` targeted insert + .bak + re-parse), `devices.py`
  (the Tier-1 menu: backend → sample → instrument(IP) → cycle → wiring + the exact
  user.toml selection line; touches no instrument) and `sample.py` (add-a-sample
  scaffold: prints paste-ready config/registry snippets + creates the data folder;
  never edits shared files — INSTALL §2 checklist); `find_runs.py --cooldown`;
  `device.py --history` shows `by=<operator>`. (Mirror retired same day — see below.)

**2026-07-07 — CLI consolidation (v0.4.0): the mirror is gone.** The 10-file script
engine moved into `scqo/cli/` — ONE implementation, tested in SCQO CI, exposed as the
**`scqo` console command** (`[project.scripts]`; subcommand flags byte-identical to
the old scripts) that works from any directory in the right venv. Backends resolve
via the NEW **`scqo.backends` entry-point group** (mirrors `scqo.experiments`):
LCHQBDriver registers `qblox = lchqb.scqo_backend:build_backend`, LCHQMDriver
`qm = customized.scqo.backend_factory:build_backend` (each serves its real + `_sim`
modes; QM's state_sync="pull" guard now fires BEFORE QUAM loads); a missing driver
fails loudly naming the repo/venv. `simulated` is built in with a UNIFIED q0/q1 demo
device (QM's old q1/q2 demo retired) + `ensure_demo_experiments()` (register-if-
absent, never shadows a driver) so driver-less envs get a full catalog. Driver
`scripts/` are now ≤10-line wrappers (all documented `python scripts\...` forms keep
working; `_lab.py`/`_cli.py` are import shims); launcher stubs regenerate via
`scqo sync-launchers`. NOTE: entry points + the console command register at INSTALL
time — upgrading across v0.4.0 requires re-running the `uv pip install -e` lines
(INSTALL §1/§5); uninstalled-checkout script use (old sys.path trick) is retired.

**2026-07-07 — v0.4.1 + release discipline.** `scqo doctor` (read-only health check:
venv/drivers/config chain/registries/catalog — the first debugging move, born from a
real server incident where a stale editable install hid the qm entry point). Releases
are now COMBOS recorded in RELEASES.toml (all four scqo-versioned repos share the tag
name; scqat pins independently, >= v0.1.4) with the manager checklist in RELEASING.md.
Also: missing-driver error distinguishes wrong-venv from stale-install; the test suite
is isolated from the runner's real ~/.scqo files (suite-wide conftest fixture); viewer
tests skip where python-multipart is absent (the QM lock env).

**2026-07-08 — device-centric configuration (v0.5.0; published same day: combo tag
across the four repos + RELEASES.toml entry, scqat pinned v0.1.5).** Users select the
SAMPLE; the registry knows the instrument. Resolution chain: `device` (user.toml >
`[lab]`; none = built-in simulated demo, unsaved) → the device's cooldown registry →
ACTIVE cycle → current `[[<cycle>.setup]]` era (latest `since` ≤ today; same date =
later block in the file wins) → `setup["backend"]` → `scqo.backends` factory, NEW
signature **`build_backend(cfg, setup)`**. Every missing link is a SystemExit naming
the exact `scqo cooldown start` fix. Fresh-start: retired keys are simply not read
(no migration shims — nothing published carried them).
- **`[[.mapping]]` renamed `[[.setup]]`** and now owns the WHOLE setup: `since` +
  `backend` (∈ `SETUP_BACKENDS` = qblox|qm|simulated) + `instrument_config` (folder
  holding ALL vendor config files under canonical names — qblox: `dut_config.json` +
  `hw_config.json`; qm: `state.json` + `wiring.json`; required for real backends,
  FORBIDDEN for simulated) + note + port map. LOUD validation in `load_cooldowns`:
  ≥1 setup per cycle, field rules, within-cycle folder uniqueness (normcase+resolve,
  path written back resolved); folder EXISTENCE is checked only by factories +
  doctor, so analysis machines still read registries.
- **Retired**: `[qblox]`/`[qm]` vendor tables, `[lab]` backend/device_name/state_path,
  `instruments.toml` (+ its loader — the config folder IS the connection truth), twin
  modes (`qblox_sim`/`qm_sim`). `state_path` is pure convention
  `<data_root>/<device>/scqo_state.json`. user.toml keys: `device`/default_tags/
  parameters_file. `make_session(backend, cfg, *, backend_label)` forces
  state_sync="push" for simulated (persistence footgun killed); persistence requires
  data_root AND device; `backend_label` = the resolved setup's backend (provenance).
- **Index schema v5**: `wiring_since` → `setup_since` (auto-reindex on version
  mismatch). Post-`cooldown end` runs REFUSE until the next `scqo cooldown start`
  (was: stamped ""). CLI: `cooldown start` gains `--backend`/`--instrument-config`
  and writes a real first `[[setup]]` block (+ stderr WARN if canonical files
  absent); `sample new` prints the no-shared-edit checklist; `devices` is a DEVICE
  menu; `doctor` checks setup fields, canonical files, and warns on cross-device
  shared ACTIVE config folders. Real-config test fixtures:
  `tests/demo_instr_config/` (OPX_OPX1000, QBlox_Scheduler); drivers run
  parse-grade tests against them (skip-guarded, side-by-side checkout).

**2026-07-10 — suggest → review → accept + physical-parameter store (v0.6.0,
fresh start: no data migration).** `Session.run` no longer applies fitted values:
`update()` runs against a `SuggestionCapture` shim (zero changes to experiment/
driver/contrib update() bodies; a typo'd field now raises instead of vanishing) and
every write becomes a PENDING `Suggestion` (qubit/field/store/before/after/status/
comment) stored on the run record (`record.json` = truth; index v6 adds
`suggestions` + `suggestions_pending`, auto-reindex). `update` param: `"suggest"`
(default) / `"apply"` (old behavior — capture + immediate accept-all through the
SAME path, so applied runs carry the audit trail too; `True`/`False` alias
apply/none) / `"none"`. Decisions: `scqo run` prompts at a terminal (Enter = apply
nothing; partial by rows/qubit/field), non-TTY leaves pending; later
`scqo accept <run_id>` (interactive picker / `--qubit`/`--field` partial /
`--reject` + `--comment`; bare `scqo accept` lists pending, `find --pending` too).
`Session.accept` replays through RecordingDevice (vendor-push-first, per-qubit
atomic, ChangeRecords stamped with the ORIGINATING run_id) guarded by cooldown-era
match + per-item staleness (before == current) unless `--force`; reject/list are
datastore-only (no backend built). `calibrate` prompts per step (accepted values
feed the next step; `--accept` unattended). NEW `scqo/physical.py`:
`PHYSICAL_FIELDS` + `PhysicalStore` -> `<data_root>/<device>/physical.json`
(values + ChangeRecord history; next to a bare state_path when no data_root) — the
SAMPLE's instrument-independent measured physics. t1_s/t2_star_s/t2_echo_s MOVED
out of config.FIELDS into it (readout_fidelity stays: instrument-dependent);
legacy state-file keys are simply not read. Flux experiments' update() now
proposes their physics (unified names: `flux_period_v`->`dv_phi0_v`,
`f01_at_sweet_spot_hz`->`f_q_max_hz`; fit keys unchanged). Physics-integrity
rule (from the v0.6.0 review): resonator_spectroscopy_flux proposes f_r0/g ONLY
when the caller supplied `f_q_max_hz` (an unconstrained dispersive fit holds
f_q_max at a scqat placeholder assumption), and never proposes f_q_max_hz itself
(an input; qubit_spectroscopy_flux measures it) — assumed values must not enter
the measured-physics ledger. Surfaces:
`Session.physical_state()`/`history(store=)`/`find_runs(pending=)`,
`scqo device --physical`, viewer (display-only: pending filter + updates column,
run-page suggestions table, device-page physical panel; tag/note stays its only
write). `updated_device` redefined: "≥1 suggestion applied" (a later accept flips
it via the tag_run-pattern `update_suggestions`). `sample.json` stays reserved for
Phase-3 *inferred* physics. **2026-07-11 (from the new-user role-play):**
`scqo accept --reapply` re-decides ALREADY accepted/rejected items — roll back to
an older run's value or accept after a reject; staleness guard OFF in that mode
(the summary's `current` shows what was overwritten), era guard stays; every
re-application is a fresh run-linked ChangeRecord. Plus the v0.5→v0.6 upgrade
notes in INSTALL §2 and the "Coming from v0.5.0?" call-out in TUTORIAL §2.
**2026-07-12 — live-source provenance + guards-as-dialogue.** NEW `scqo/provenance.py`
(pure; shared by viewer + Session.live_sources()): the LAST ChangeRecord per
(qubit, field) is credited as a value's source ONLY while `record.new == current`
(strict match — vendor reseeds/qualibrate writes = "external", NEVER a false
credit; run_id=None = "manual"; no record = "unrecorded"). Surfaces: viewer runs
list `live:` line in the updates column (pending line preserved), device page
values link to their source runs (state authority switched to scqo_state.json
`config` when present — deferred accepts now visible), run page "on device"
column (LIVE / superseded→link), CLI `scqo device --sources` (both stores, one
table). And `Session.accept(dry_run=True)` returns a JSON plan (era reported not
raised, decided items included, per-item stale/current facts); the interactive
picker (`_review.review_interactively`) turns every guard into a warning + [y/N]
(Enter = No): era asks once, decided rows ask "re-apply (rollback)?", stale rows
show the diff — no flag knowledge needed at a TTY; `--force`/`--reapply` remain
the script form (batch semantics byte-identical). `_apply` returns applied ITEMS
(status alone can't tell after a failed reapply).

**2026-07-12 — named user-selectable setups + CLI regrouping (v0.7.0, fresh
start: no data migration, no aliases).** Within one cooldown cycle a device may
now declare SEVERAL setups (different backend, or same instrument with another
config folder) and each user picks theirs. `cooldowns.toml` setups became NAMED
sub-tables `[<cycle>.setup.<name>]` carrying EXACTLY backend/instrument_config/
note — `since` dates (resolution was date-based) and port-map pairs (display-only
everywhere; wiring lives in the vendor config folder) are RETIRED, unknown keys
and the old `[[.setup]]` array form refuse loudly. Names ^[A-Za-z0-9_-]+$, one
folder per setup per cycle, and a name is IMMUTABLE for its cycle (rename =
declare a new setup). Zero-setup cycles are legal: `scqo device cooldown start`
opens an EMPTY cycle (no --backend/--instrument-config; setup blocks are
hand-added), runs refuse naming the fix. Selection: user.toml gains `setup`
(user overlay ONLY, never [lab]); single-setup cycle auto-selects; several →
refuse listing names. Resolution lives in `datastore.resolve_setup` +
`_backends.resolve_device_setup` (canonical refusal texts, shared by
build_session and `scqo user`'s "runs would refuse:" preview). The resolved
name threads build_session → make_session(setup_name=) → Session.setup_name →
DataStore(setup=) → run_stamps() → RunRecord.setup (index v7, auto-reindex;
`setup_since` gone) → the accept era guard (compares (cooldown, setup name);
bound name deliberately NOT re-validated at persist time — truthful provenance,
never raise after the measurement). `find_runs(setup=)` / `scqo find --setup` /
viewer runs-page setup filter+column; device page shows ALL setups of the
ACTIVE cycle (wiring panel gone). CLI: `scqo state` = the old `scqo device`
view (flags unchanged); NEW `scqo user` (no-args diagnosis view exits 0;
--device/--setup validate against the registry then WRITE user.toml — the
first-ever user.toml writer, targeted line edit + .toml.bak + re-parse,
$SCQO_USER_CONFIG honored, "none" refuses; stale setup auto-cleared on device
switch); `scqo device` = admin group (list = per-setup menu rows / add =
old `sample new` / cooldown start|end), old `devices`/`sample`/`cooldown`
commands removed. Drivers: factory contract unchanged (reads only
backend+instrument_config). The driver scripts/ WRAPPER LAYER is fully retired
(user decision, no users yet = the chance to clean): the command wrappers
(run_experiment/calibrate/find_runs/tag_run/device/devices/cooldown/sample), the
`_lab`/`_cli` import shims, the auto-generated `scripts/experiments/` launcher
stubs AND the `scqo sync-launchers` subcommand are all DELETED — `scqo run
<name>` is the one way to run an experiment; driver scripts/ keeps only real
per-repo content (check_real_config.py + LCHQB's ai_loop_demo.py — the one
worked Session-API example, importing `from scqo.cli import build_session`
directly; its run_resonator_spectroscopy.py example was deleted too, redundant
with `scqo run`). Never add wrappers or per-command stubs again.
Combo tag v0.7.0 all four repos, scqat pin v0.1.5.

**2026-07-12 (post-v0.7.0, on main) — `scqo calibrate` REMOVED** (user decision:
unused at this phase, keep the project light). The fixed bring-up sequence was
the command's only content; the docs now show the three explicit `scqo run`
steps (res spec → qubit spec → power Rabi, accept each). A sequence runner
returns with the Phase-3 AI loop, where deciding the next step belongs.
`cli/calibrate.py` deleted, `_COMMANDS` entry gone (no alias), `_review.py` is
shared by run + accept only.
