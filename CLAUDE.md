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
  backend.py      # Backend ABC: .device + .acquire(experiment) -> xarray.Dataset
  experiment.py   # Experiment ABC: physics half (define_sweep/simulate/estimate/update) + backend half (probe)
  registry.py     # @register / get / catalog  (AI's menu of measurements)
  session.py      # Session: catalog() / run() / find_runs() / load_run() / device_state() / history()
  datastore.py    # DataStore + RunRecord: every run saved to a folder, indexed in SQLite (rebuildable)
  labconfig.py    # ~/.scqo/config.toml -> LabConfig + make_session (students never edit repos)
  testing.py      # InMemoryDevice + SimulatedBackend (run with no instrument)
  experiments/
    resonator_spectroscopy.py   # frequency sweep, Lorentzian dip -> updates readout_freq
    qubit_spectroscopy.py       # two-tone peak search -> coarse drive_freq (bring-up step 2)
    qubit_ramsey.py             # time sweep, decaying-cosine fit -> updates drive_freq + T2*
    qubit_power_rabi.py         # amplitude sweep, cosine fit -> updates pi_amp
    qubit_relaxation.py         # pi + swept wait, exp-decay fit -> reports t1_s (no writeback)
    qubit_echo.py               # Hahn echo, exp-envelope fit -> reports t2_echo_s (no writeback)
    qubit_spectroscopy_flux.py  # 2D flux x detuning arch -> sweet spot / Ej_sum (Phase-3 feeder)
    single_shot_readout.py      # per-shot IQ blobs (prepared_state x shot_idx) -> readout fidelity
    resonator_spectroscopy_flux.py   # 2D resonator flux map -> sweet spot / dv_phi0 / f_r0 / g (report)
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

**Multi-device rule (decided 2026-07-05):** `device_name` = the physical SAMPLE (chip),
never the instrument; the instrument is provenance (every run/fit stamps `backend`).
ONE data_root + ONE index for all samples (`find_runs(device=...)` / `--device` filter;
per-sample DBs are rejected). With two instruments carrying two samples, the lab config's
`[qblox]`/`[qm]` tables override `device_name`/`state_path` per backend (`scqo.labconfig`).
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
1. **Students** run the driver scripts (`run_experiment.py` / `find_runs.py`) with
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
         under the core `scqo.experiments` group; contrib copy deleted.
   - [ ] Launcher stubs regenerated in each driver (`python scripts/experiments/_sync.py`)
         so the new experiment is directly runnable.

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
