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

The scqo stack uses this vocabulary throughout — **scqat** (`estimators/`, `tools/`, `BaseEstimator`), **SCQO** (`Experiment`, `scqo.experiments`, `probe()`, `estimate()`), and the drivers **LCHQBDriver** + **LCHQMDriver** (`probe()`-only experiments). scqat's estimator keeps its own orchestrator method `analyze()` (a different layer). LCHQMDriver's qualibrate calibration nodes keep qualibrate's own `node` framework and never import scqo (its scqo surface lives in `customized/scqo/`). (QBLOX_training documents Qblox's *own* `Experiment` ABC — a different class from this `Experiment`.)

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

## Package layout

The device model is the greenfield schema — `docs/greenfield-schema.md` is the spec
(marked implemented). A device = MODES (quantum degrees of freedom), COMPOSITES (named
mode groups with joint physics), LINES (physical control paths) and CHANNELS (one signal
of one kind riding a line); a line's rider lists mint the channels. Field routing is
per-field by ROLE: fact -> physical.json, knob -> scqo_state.json + pushed to the vendor,
monitor -> scqo_state.json never pushed. Knobs live on CHANNELS (`q1_ro.readout_freq_hz`,
`q1_xy.pi_amp`, `q1_xy.thermalization_time_s`, `q1_z.idle_flux`); facts live on modes and composites (`q1.f_01_hz`,
`q1_res.f_r_hz`, `q1_q2.zz_hz`); composite per-operation knobs are full names
(`iswap_coupler_flux`). As-designed targets live in the sibling `design.toml`.

```
scqo/
  parameters.py   # Parameters base + TargetSelection / AveragingParameters mixins (decision surface)
  result.py       # Outcome enum + Result base (extraction surface)
  catalog.py      # the KIND catalogs: mode kinds (transmon/flux_transmon/fluxonium/
                  #   cavity/resonator), composite kinds (qubit_pair, cat_system),
                  #   channel kinds (drive/readout/flux/pump); FieldSpec {unit, doc,
                  #   role fact|knob|monitor, portable, design_ok, shape, paired_with,
                  #   design_source} + the frozen DERIVATION (channel kind x target
                  #   kind) legality table - the schema source
  entities.py     # the four frozen entity dataclasses over one base (mode/composite/
                  #   line/channel) + signature() = the components.lock identity
  roster.py       # components.toml (schema 3) loader: [modes]/[composites]/[lines]/
                  #   [channels]; EXPANDS rider lists into minted channels
                  #   (readout -> q1_ro + q1_res, drive -> q1_xy, flux -> q1_z) and
                  #   compiles each entity's exact legal-field set
  design.py       # design.toml loader: entity-named as-designed targets (the chip
                  #   datasheet; bring-up sweep anchors), validated AFTER roster
                  #   expansion; Design.compare = doctor's design-vs-measured join
  stores.py       # the two per-context value stores, one shape
                  #   {"schema": 3, "values": {entity: {field: ...}}}: physical.json
                  #   (facts) + scqo_state.json (knobs + monitors); ROLE routes the write
  _state_io.py    # shared state-file plumbing: the .lock file + the .history.jsonl
                  #   sidecar (lock-guarded merge-on-save, torn-line tolerance)
  device.py       # vendor views per CHANNEL KIND (make_view_base) + CompositeView
                  #   (per-operation knobs via read_knob/write_knob) + RecordingDevice
                  #   (every write -> ChangeRecord) + DeviceModel ABC
  fieldmap.py     # VendorBinding/VendorOnly shapes: the DRIVER-declared field catalog
                  #   (neutral field -> vendor path/unit/convert DESCRIPTION + the
                  #   backend-unique inventory) rendered by `scqo state --fields`
  suggestions.py  # Suggestion + SuggestionCapture: update() writes become PENDING
                  #   proposals on the run record, routed by ROLE at accept/reject;
                  #   origin="operator" = human-attached via Session.suggest
  provenance.py   # live-source provenance: which run each CURRENT value traces to
                  #   (strict-match; a drifted value reports "external")
  lock.py         # the production cut: freeze() writes components.lock, verify()
                  #   enforces superset-by-signature (retire, never delete)
  checks.py       # doctor witnesses over the model, renderer-free (unreachable modes,
                  #   design coverage, lock drift, roster-vs-vendor inventory, wiring)
  report.py       # report rows behind `scqo state` / `scqo device` - renderer-free,
                  #   JSON-able (CLI prints, viewer + AI loop consume the same shapes)
  contract.py     # DatasetContract per probing method: the explicit probe <-> estimator API
  backend.py      # Backend ABC: .device + .acquire(experiment) -> xarray.Dataset
  experiment.py   # Experiment ABC: physics half (define_sweep/simulate/estimate/update)
                  #   + backend half (probe); kind-based gating (target_kinds) +
                  #   validate_targets pre-probe hook; knobs via device.channel(t, kind)
  _scqat.py       # the one scqat import point (lazy): per-target split + analyze() loop
  session.py      # Session: catalog() / run() / accept() / reject() / suggest() / set_values() /
                  #   find_runs() / load_run() / tag_run() / device_state() / physical_state() /
                  #   qubit_state() / history(); qubit-closure addressing (q1.pi_amp -> q1_xy)
  datastore.py    # DataStore + RunRecord: every run saved to a folder, indexed in SQLite (rebuildable)
  labconfig.py    # ~/.scqo/config.toml -> LabConfig + make_session (students never edit repos)
  testing.py      # InMemoryDevice + SimulatedBackend + the demo device (REAL
                  #   components.toml/design.toml text parsed by the real loaders)
  browse.py       # `python -m scqo.browse` - datasette raw-SQL power tool over the index (8081)
  viewer/         # `python -m scqo.viewer` - the daily read-only GUI (8080)
  __main__.py     # `python -m scqo <data_root>` - rebuild the index from the run folders
  cli/            # the `scqo` command (run/find/accept/suggest/set/tag/state/user/
                  #   device/doctor): ONE engine, any-directory;
                  #   the device's SELECTED named setup picks the backend, resolved via
                  #   the scqo.backends entry-point group; a factory is
                  #   build_backend(cfg, setup, roster) - a driver serves a view PER
                  #   CHANNEL ENTITY and resolves names through the roster, never by
                  #   parsing them; simulated is built in
  experiments/    # the registry lives in __init__.py: @register / get / catalog (the
                  #   AI's menu; maturity core|contrib + DERIVED capability tags)
    _capabilities/  # one module per capability: the canonical Parameters mixin + contract
                    #   fragment + sim/estimate helpers (state_readout.py, flux.py,
                    #   qubit_reset.py = reset_method + the thermal wait, resolved for
                    #   both drivers by the ONE helper reset_wait_ns); catalog
                    #   `tags` are DERIVED from mixin subclassing — never declared strings,
                    #   zero tags legitimate (new experiments may be unclassifiable)
    _drive_power.py             # shared recorded set->revert drive_power_dbm boundary
    _flux_component.py          # kind-agnostic foreign flux source mixin (record-only guard)
    _sim.py                     # shared helpers for the offline simulators
    resonator_spectroscopy.py   # frequency sweep, Lorentzian/circle fit -> readout_freq_hz
                                #   (readout channel) + f_r_hz/kappa_tot_hz (resonator facts)
    qubit_spectroscopy.py       # two-tone peak search -> coarse drive_freq_hz (drive channel)
                                #   + f_01_hz (mode fact) (bring-up step 2)
    qubit_ramsey.py             # time sweep, decaying-cosine fit -> drive_freq_hz (drive
                                #   channel) + f_01_hz/t2_star_s (mode facts)
    qubit_power_rabi.py         # amplitude sweep, cosine fit -> pi_amp (drive channel)
    qubit_relaxation.py         # pi + swept wait, exp-decay fit -> t1_s (mode fact)
    qubit_echo.py               # Hahn echo, exp-envelope fit -> t2_echo_s (mode fact)
    qubit_spectroscopy_flux_pulse.py  # 2D flux x detuning arch -> ej_sum_hz/f_q_max_hz (mode
                                #   facts) + flux_offset/flux_per_phi0 (flux-channel facts)
    single_shot_readout.py      # per-shot IQ blobs (prepared_state x shot_idx) ->
                                #   fidelity_g/fidelity_e monitors + pos_* blob centers on the
                                #   readout channel; a discriminating driver also proposes
                                #   readout_rotation_rad/readout_threshold/readout_rus_threshold
    resonator_spectroscopy_flux.py   # 2D resonator flux map -> idle_flux + readout_freq_hz
                                #   at the sweet spot; flux_offset/flux_per_phi0 (flux-channel
                                #   facts) + f_r0_hz/g_hz (resonator facts)
    readout_power.py            # per-shot fidelity vs amp prefactor -> readout_amp
    readout_frequency.py        # per-shot fidelity vs readout detuning -> readout_freq_hz
    resonator_spectroscopy_power_amp.py  # FAST punchout: set-top -> one-program FPGA amplitude
                                #   sweep down -> revert; absolute-dBm window -> readout_power_dbm + readout_freq_hz
    resonator_spectroscopy_power_chain.py  # CAREFUL punchout: steps the output chain per point
                                #   (amp ~0.5 for SNR; wide, cross-backend) -> readout_power_dbm + readout_freq_hz
    qubit_pi_pulse_error.py     # pi-amplitude error amplification -> pi_amp (drive channel)
    qubit_drag_equator.py       # 3-line symmetric DRAG calibration -> drag_beta (drive channel)
    qubit_drag_alternating.py   # alternating-pulse DRAG calibration -> drag_beta (drive channel)
    qubit_relaxation_flux.py    # T1 vs swept z bias - record-only diagnostic (per-flux fits in result.fit)
    qubit_echo_flux.py          # T2_echo vs swept z bias - record-only diagnostic
    qubit_sqrb.py               # single-qubit randomized benchmarking - record-only gate fidelities
    qubit_tomography.py         # state tomography (custom contract) - record-only
    pair_zz_coupler.py          # residual ZZ vs coupler bias (echo fringe per bias) -> idle_flux
                                #   on the COUPLER's flux channel (ZZ-off point) + zz_hz (pair fact)
tests/test_model_run.py         # catalog -> run -> suggest -> accept, no hardware
tests/test_datastore.py         # run folders + index + tags + reindex, no hardware
```

### Datastore (the "find my measurement data" layer)
`Session(backend, data_root=...)` persists **every** run — raw dataset (`dataset.nc`),
parameters/result/record JSONs, device before/after snapshots, and the scqat artifacts
(metadata / plotdata / figure PNGs, per qubit) — under
`<data_root>/<device>/<YYYY-MM-DD>/<run_id>/`. The **run folder is the truth**;
`<data_root>/index.sqlite` is a disposable cache (`python -m scqo <data_root>`
rebuilds it). Query with `Session.find_runs(experiment=, target=, tag=, since=, outcome=,...)`,
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

### Testing discipline — run only what the edit can break
Default for a localized change (from `D:\github\SCQO`): `uv run pytest tests/test_model_experiments.py -k ramsey -q`.
Selection map for experiment work (`scqo/experiments/<name>.py`) — always the first row, plus any that apply:

| Also changed | Add to the run |
|---|---|
| *always* | `tests/test_model_experiments.py -k <stem>` |
| a capability mixin (`_capabilities/`) | `tests/test_capabilities.py` **+ `tests/test_model_experiments.py` UNFILTERED** — drop the `-k`: a mixin edit is shared-core for every experiment that subclasses it, and only the full every-experiment sweep catches the ones you didn't think of (~50 s, 30 tests) |
| a time axis (`idle_time_ns`-style grid) | `tests/test_time_grid.py -k <stem>` |
| `Contract` / `define_sweep` | `tests/test_contract.py` (small — run whole) |
| a `*_method` Literal | `tests/test_estimator_method_sync.py` |
| a `catalog.py` FieldSpec | `tests/test_model_catalog.py` |
| Parameters defaults/overlay plumbing | `tests/test_parameter_defaults.py` |

`-k` takes the **distinctive stem, not the registered name**: `-k ramsey` matches both
`test_every_experiment_runs_clean[qubit_ramsey]` and `test_ramsey_writes_drive_freq_fact_twin_and_t2`,
while `-k qubit_ramsey` misses the second. **0 collected means the filter was wrong** — widen it, never skip.
Leave `test_cli_*.py` (20 subprocess spawns), `test_index_scale.py` (100k rows) and `test_viewer.py` alone
unless the edit is in `scqo/cli/`, `scqo/datastore.py` or `scqo/viewer/` respectively.

The **full suite** (`uv run pytest -q`) is for exactly two cases: (1) cutting a release, and (2) an edit to
shared core, where the blast radius is everything — `catalog.py`, `entities.py`, `roster.py`, `stores.py`,
`device.py`, `experiment.py`, `session.py`. Otherwise **report the exact command run** and offer the
full-suite command instead of spending the minutes unasked.

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
   - [ ] `update()` writes only catalogued fields (extend the kind catalog in `catalog.py` first if needed).
   - [ ] Ran repeatedly via contrib with findable data; results reviewed via `find_runs`.
   - [ ] `description` is catalog-quality (an AI reads it to decide).
   - [ ] Physics half moved to `scqo/experiments/`; driver `probe()` subclasses registered
         under the core `scqo.experiments` group; contrib copy deleted (then directly
         runnable via `scqo run <name>`).

**`scqo run <name>` is the single CLI entry point** — never add wrappers, launcher stubs,
or per-command shims.

### The placement rule (digest — full text: TUTORIAL §10; bench: `scqo state --rule`)
Classify each USE of a quantity, in order, first match wins:
(1) gone when the run ends → per-run Parameters; (2) true of the chip in the dark
(no instrument SETTING realizes it; setup coordinates OK if declared) → role `fact`
→ physical.json; (3) measured but a vendor knob realizes it (TOF) → write the vendor
knob, catalog unit; (4) a knob the loop reads/writes vendor-neutrally → role `knob`
on its channel/composite → scqo_state.json + pushed (absolute at a declared plane =
portable; chain-fraction = non-portable, twin or catalogued scale);
(5) measured, no knob → performance of the current knobs = role `monitor`
(scqo_state.json, never pushed), else run-record-only;
(6) rest = vendor config, catalogued with kind realizer/candidate/vendor/unique —
unique locks experiments to that instrument.

### Reference backends
- `D:\github\LCHQMDriver` — Quantum Machines (qm-qua / quam / qualibrate); QM reference impl (`calibrations/LCH_*.py`, `customized/node/*/parameters.py`, `quam_config/my_quam.py`).
- `D:\github\LCHQBDriver` — Qblox (qblox-scheduler); the Qblox backend, independent of the QM stack.
- `D:\github\QBLOX_training` — read-only Qblox reference docs (`docs/applications/superconducting/single_qubit_experiment_helpers/experiment.py`, `cal*.py`, `custom_elements.py`).

## Status
Current published release: **v0.14.0** — see `RELEASES.toml` for the combo manifest and required upgrade actions. Release history lives in git tags + `RELEASES.toml`, not here.
