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

The scqo stack uses this vocabulary throughout — **scqat** (`estimators/`, `tools/`, `BaseEstimator`), **SCQO** (`Experiment`, `scqo.experiments`, `probe()`, `estimate()`), and **LCHQBDriver** (`probe()`-only experiments). scqat's estimator keeps its own orchestrator method `analyze()` (a different layer). LCHQMDriver still uses qualibrate's own `node` framework. (QBLOX_training documents Qblox's *own* `Experiment` ABC — a different class from this `Experiment`.)

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

## Package layout (scaffolded)

```
scqo/
  parameters.py   # Parameters base + QubitSelection / AveragingParameters mixins (decision surface)
  result.py       # Outcome enum + Result base (extraction surface)
  device.py       # QubitView / DeviceModel ABCs (neutral field names)
  physical.py     # PHYSICAL_FIELDS + PhysicalStore: the SAMPLE's instrument-independent
                  #   measured physics (T1/T2, arch/dispersive fits) -> the context's
                  #   scqo/physical.json (+ physical.history.jsonl sidecar)
  _state_io.py    # shared state-file plumbing: the .lock file + the .history.jsonl
                  #   sidecar (read/write + pre-split embedded-history fallback)
  suggestions.py  # Suggestion + SuggestionCapture: update() writes become PENDING
                  #   proposals on the run record; accept/reject decide them (v0.6);
                  #   origin="operator" = human-attached via Session.suggest (v0.9)
  backend.py      # Backend ABC: .device + .acquire(experiment) -> xarray.Dataset
  experiment.py   # Experiment ABC: physics half (define_sweep/simulate/estimate/update) + backend half (probe)
  registry.py     # @register / get / catalog  (AI's menu of measurements)
  session.py      # Session: catalog() / run() / accept() / reject() / suggest() /
                  #   find_runs() / load_run() / device_state() / physical_state() / history()
  datastore.py    # DataStore + RunRecord: every run saved to a folder, indexed in SQLite (rebuildable)
  labconfig.py    # ~/.scqo/config.toml -> LabConfig + make_session (students never edit repos)
  testing.py      # InMemoryDevice + SimulatedBackend (run with no instrument)
  cli/            # the `scqo` command (run/find/accept/suggest/tag/state/user/
                  #   device/doctor): ONE engine, any-directory;
                  #   the device's SELECTED named setup picks the backend, resolved via
                  #   the scqo.backends entry-point group; simulated is built in
                  #   (_backends.ensure_demo_experiments fills the catalog driver-less)
  experiments/
    resonator_spectroscopy.py   # frequency sweep, Lorentzian dip -> updates readout_freq + f_r/kappa (physical store)
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
    resonator_spectroscopy_power_amp.py  # FAST punchout: set-top -> one-program FPGA amplitude
                                #   sweep down -> revert; absolute-dBm window -> readout_power_dbm + readout_freq
    resonator_spectroscopy_power_chain.py  # CAREFUL punchout: steps the output chain per point
                                #   (amp ~0.5 for SNR; wide, cross-backend) -> readout_power_dbm + readout_freq
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

**Multi-device rule:** the device = the physical SAMPLE (chip),
never the instrument; the instrument is provenance (every run/fit stamps `backend`).
ONE data_root + ONE index for all samples (`find_runs(device=...)` / `--device` filter;
per-sample DBs are rejected). Each user selects the sample and setup (`device`/`setup`
in user.toml; `scqo user`); which instrument carries it — and where its vendor config
folder lives — is a fact of the SELECTED named setup of the device's ACTIVE cooldown
cycle (`[<cycle>.setup.<name>]` in its cooldowns.toml), never a config key. ALL folder
locations are DERIVED from the registry keys: a setup table is exactly `backend` +
optional `note`; its vendor folder is the sibling `<cid>/<name>/backend_config/`,
injected by `load_cooldowns` as `setup["instrument_config"]` (typed paths are refused —
they can dangle). That sibling split is load-bearing: it keeps SCQO's own files out of
QUAM's state-directory rglob by construction.
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
         under the core `scqo.experiments` group; contrib copy deleted (then directly
         runnable via `scqo run <name>`).

**`scqo run <name>` is the single CLI entry point** — never add wrappers, launcher stubs,
or per-command shims.

### Reference backends
- `D:\github\LCHQMDriver` — Quantum Machines (qm-qua / quam / qualibrate); QM reference impl (`calibrations/LCH_*.py`, `customized/node/*/parameters.py`, `quam_config/my_quam.py`).
- `D:\github\LCHQBDriver` — Qblox (qblox-scheduler); the Qblox backend, independent of the QM stack.
- `D:\github\QBLOX_training` — read-only Qblox reference docs (`docs/applications/superconducting/single_qubit_experiment_helpers/experiment.py`, `cal*.py`, `custom_elements.py`).

## Status
Current published release: **v0.9.0** — see `RELEASES.toml` for the combo manifest and required upgrade actions. Release history lives in git tags + `RELEASES.toml`, not here.
