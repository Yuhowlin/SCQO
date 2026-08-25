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
- **campaign** — an ordered list of experiment STEPS walked N times (`CampaignPlan` / `Session.run_campaign()` / `campaign_id`). OUTER repetition: every (repeat, step) is a full `run()` with its own folder, dataset, fit and TIMESTAMP, stamped `campaign`/`repeat_idx`/`step_idx`; the campaign owns only the plan, the cadence, the stop conditions and the per-(experiment, target, quantity) statistics. Repeating ONE experiment is the degenerate 1-step case. Do NOT call it a *repetition* (scqat's `repetition_data` splitter, the drivers' HW-averaging loop and `pulse_repetitions` already own that word) nor a *series* (the /trends time series).

The scqo stack uses this vocabulary throughout — **scqat** (`estimators/`, `tools/`, `BaseEstimator`), **SCQO** (`Experiment`, `scqo.experiments`, `probe()`, `estimate()`), and the drivers **scqo-qblox** + **scqo-qm** (`probe()`-only experiments). scqat's estimator keeps its own orchestrator method `analyze()` (a different layer). scqo-qm's vendored official qualibrate nodes keep qualibrate's own `node` framework and never import scqo (its scqo surface is the `scqo_qm` package). (QBLOX_training documents Qblox's *own* `Experiment` ABC — a different class from this `Experiment`.)

## Where the two backends started (historical)

**This section is history, not the current picture** — for the repos as they are today see
**Reference backends** near the end. SCQO was extracted from two lab drivers that did the same
physics on different hardware: `LCHQMDriver`, now **[scqo-qm](https://github.com/shiau109/scqo-qm)**,
and `LCHQBDriver`, now **[scqo-qblox](https://github.com/shiau109/scqo-qblox)**. Neither old name
exists any more. The Qblox column below is drawn from `QBLOX_training`, the read-only vendor
example repo the Qblox driver was written against — it is a *reference*, not an ancestor, and it
documents Qblox's own `Experiment` ABC, a different class from this one.

| | QM side (was LCHQMDriver) | Qblox side (QBLOX_training reference) |
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
`q1_res.f_dress0_hz`, `q1_q2.zz_hz`); composite per-operation knobs are full names
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
                  #   (facts) + scqo_state.json (knobs + monitors); ROLE routes the write;
                  #   history appends to the context's changes DB (O(new) saves, no
                  #   history load at init)
  changes.py      # the per-context change-history TRUTH: history.sqlite in each
                  #   scqo/ folder (both stores via the `store` column; ChangeRecord
                  #   lives here) — per-CONTEXT so lab aggregation stays a folder
                  #   copy (one writer per context), NEVER dropped/rebuilt (unlike
                  #   index.sqlite); indexed queries (param_series/latest_two/
                  #   context_facts/fact_series) + cross-context collect_* helpers
  _state_io.py    # the values-file .lock (acquired strictly OUTSIDE the changes-DB
                  #   transaction) + the retired sidecar's name for the v2 gate
  device.py       # vendor views per CHANNEL KIND (make_view_base) + CompositeView
                  #   (per-operation knobs via read_knob/write_knob) + RecordingDevice
                  #   (every write -> ChangeRecord) + DeviceModel ABC
  fieldmap.py     # VendorBinding/VendorOnly shapes: the DRIVER-declared field catalog
                  #   (neutral field -> vendor path/unit/convert DESCRIPTION + the
                  #   backend-unique inventory) rendered by `scqo state --fields`
  suggestions.py  # Suggestion + SuggestionCapture: update() writes become PENDING
                  #   proposals on the run record, routed by ROLE at accept/reject;
                  #   origin="operator" = human-attached via Session.suggest.
                  #   CAMPAIGN-level rows (on campaign.json) additionally carry
                  #   `experiment` — the proposing step; the accept groups by it
  provenance.py   # live-source provenance: which run — or campaign-level accept —
                  #   each CURRENT value traces to (statuses run|campaign|manual|
                  #   external|unrecorded; strict-match, run outranks campaign)
  lock.py         # the production cut: freeze() writes components.lock, verify()
                  #   enforces superset-by-signature (retire, never delete)
  checks.py       # doctor witnesses, renderer-free: model (unreachable modes, design
                  #   coverage, lock drift, roster-vs-vendor inventory, wiring) +
                  #   environment (profile-resident venv base / foreign-profile
                  #   config+data_root — the multi-account-server trap, INSTALL §1)
  report.py       # report rows behind `scqo state` / `scqo device` - renderer-free,
                  #   JSON-able (CLI prints, viewer + AI loop consume the same shapes).
                  #   Also the catalog-DERIVED field orders + units (never hand-kept
                  #   lists): PHYSICAL/INSTRUMENT_FIELD_ORDER + FIELD_UNITS for the
                  #   viewer's tables, and MEASURED_QUANTITIES (no knobs - a knob is
                  #   a setting and cannot drift) for the campaign progress line
  campaign.py     # CampaignPlan/CampaignStep/CampaignWriteback + the PURE aggregator
                  #   (summarize / aggregate / stderr_twin / robust_summary) over fit
                  #   dicts; no I/O, no orchestration. scatter_ratio = std /
                  #   mean_stderr is the drift-vs-fit-noise question. The simulated
                  #   backend is deterministic, so an OFFLINE campaign reports
                  #   std == 0.0. [writeback] (stat mean|median, min_n) is the
                  #   aggregate-writeback policy consumed at finalize.
  contract.py     # DatasetContract per probing method: the explicit probe <-> estimator API
  backend.py      # Backend ABC: .device + .acquire(experiment) -> xarray.Dataset
  experiment.py   # Experiment ABC: physics half (define_sweep/simulate/estimate/update)
                  #   + backend half (probe); kind-based gating (target_kinds) +
                  #   validate_targets pre-probe hook; knobs via device.channel(t, kind)
  _scqat.py       # the one scqat import point (lazy): per-target split + analyze() loop
  session.py      # Session: catalog() / run() / run_campaign() / accept() / reject() /
                  #   suggest() / set_values() / find_runs() / load_run() / tag_run() /
                  #   find_campaigns() / load_campaign() / campaign_runs() / check_campaign() /
                  #   accept_campaign() / reject_campaign() / suggest_campaign() /
                  #   device_state() / physical_state() /
                  #   qubit_state() / history(); qubit-closure addressing (q1.pi_amp -> q1_xy).
                  #   run_campaign finalize replays the statistics through each step
                  #   experiment's update() (SuggestionCapture) -> campaign-level pending
                  #   suggestions; _preflight refuses a plan label shadowing an experiment
                  #   name (exempting the self-named 1-step `scqo run --repeat` shape)
  datastore.py    # DataStore + RunRecord: every run saved to a folder, indexed in SQLite (rebuildable)
  labconfig.py    # ~/.scqo/config.toml -> LabConfig + make_session (students never edit repos)
  testing.py      # InMemoryDevice + SimulatedBackend + the demo device (REAL
                  #   components.toml/design.toml text parsed by the real loaders)
  browse.py       # `python -m scqo.browse` - datasette raw-SQL power tool over the index (8081)
  viewer/         # `python -m scqo.viewer` - the daily read-only GUI (8080):
                  #   runs / run / campaigns / campaign / setup / trends / samples
                  #   pages; per-setup pages under each cooldown cycle (current +
                  #   previous run link per parameter), /trends is CHANGE-HISTORY
                  #   driven (port 1 = one parameter in one setup, latest 50;
                  #   port 2 = one physical fact across all contexts) and the
                  #   device page holds the facts x (cooldown, setup) matrix —
                  #   all read from history.sqlite strictly read-only (never
                  #   creates one); per-setup export.{html,xlsx,pdf} (_export.py:
                  #   self-contained offline HTML with embedded run snapshots /
                  #   two-sheet xlsx / 16:9 PDF document mirroring the offline HTML)
  __main__.py     # `python -m scqo <data_root>` - rebuild the index from the run folders
  cli/            # the `scqo` command (run/campaign/find/accept/suggest/set/tag/state/
                  #   user/device/doctor): ONE engine, any-directory;
                  #   the device's SELECTED named setup picks the backend, resolved via
                  #   the scqo.backends entry-point group; a factory is
                  #   build_backend(cfg, setup, roster) - a driver serves a view PER
                  #   CHANNEL ENTITY and resolves names through the roster, never by
                  #   parsing them; simulated is built in
  experiments/    # the registry lives in __init__.py: @register / get / catalog (the
                  #   AI's menu; maturity core|contrib + DERIVED capabilities —
                  #   never "tags", that word is the datastore's run tags)
    _capabilities/  # one module per capability: the canonical Parameters mixin + contract
                    #   fragment + sim/estimate helpers (state_readout.py,
                    #   flux.py = the swept flux window in TWO FRAMES sharing one axis
                    #   key (flux_bias_v): FluxSweepParameters is ABSOLUTE DAC volts
                    #   (probe sets the DC offset) and FluxPulseSweepParameters is
                    #   RELATIVE to the channel's idle_flux (probe plays on top of the
                    #   standing bias). Frame follows MECHANISM and must show in the
                    #   NAME - a relative carrier ends in `_pulse` (checked, not a
                    #   convention) - and every carrier records `old_idle_flux` so
                    #   `flux_offset = old_idle_flux + <fitted>` is one invariant; the
                    #   flux_offset FACT is absolute in both frames,
                    #   qubit_reset.py = reset_method 'thermal'|'active' + the thermal
                    #   wait, resolved for both drivers by the ONE helper
                    #   reset_wait_ns; a backend that cannot realize a method must
                    #   REFUSE it by name, never downgrade. The opt-in set is NOT
                    #   symmetric across backends and must not be assumed: Qblox opts
                    #   in 4 coherent-drive carriers, QM 7 (those four plus
                    #   qubit_ramsey_phasor and both T1 trackers). Each driver's
                    #   census test is the authority; every other experiment refuses
                    #   by name,
                    #   amplitude.py = the swept amplitude window + the ABSOLUTE amplitude
                    #   behind it. AmplitudeSweepParameters owns min/max_amp_factor +
                    #   num_amp_points on ONE axis, AMP_AXIS = `amp_prefactor` (scqat and
                    #   the QM probes already used that name, so nothing renames at the
                    #   boundary). The window is a FACTOR of the target's stored
                    #   pi_amp/pi_amp_x90/readout_amp ON PURPOSE — one array serves every
                    #   target in a multiplexed run, which a shared absolute window
                    #   cannot. attach_absolute_amp() adds the derived `digital_amp`
                    #   coord (target x AMP_AXIS) in run()'s attach_acquisition_coords
                    #   hook, so dataset.nc answers "what amplitude actually played?" on
                    #   its own; scqat draws it as a SECONDARY axis. It is DIMENSIONLESS
                    #   (0-1 of full scale, catalog unit "") — never volts, never dBm.
                    #   A carrier declares only `amp_reference_field()` — a BARE field
                    #   name, since catalog.py guarantees uniqueness across channel kinds,
                    #   so the kind would be a second unchecked source of truth — and
                    #   estimate() reads its `old_<knob>` through the same amp_anchor.
                    #   The neutral bound is lt=2.0 (the widest ANY backend expresses);
                    #   the real limit is factor x stored <= 1 and each driver refuses it
                    #   BY NAME (scqo-qm + scqo-qblox, each experiments/_amp_limits),
                    #   detuning.py = the swept frequency window in TWO FRAMES
                    #   sharing ONE axis (DETUNING_AXIS = `detuning_hz`; a frame is
                    #   an origin, not a different quantity). drive_detuning =
                    #   start/end_drive_detuning_hz + num_drive_freq_points, RELATIVE
                    #   to the current drive_freq_hz (the four qubit_spectroscopy*);
                    #   readout_detuning = start/end_readout_detuning_hz +
                    #   num_readout_freq_points, RELATIVE to readout_freq_hz (the four
                    #   resonator_spectroscopy* + readout_frequency). Explicit
                    #   [start, end] so an ASYMMETRIC window is expressible — on the
                    #   readout side that is the physics, since power and flux sweeps
                    #   both walk the dip DOWN from f_dress0 toward f_bare. The edges
                    #   take EITHER order (only zero width is refused) and the axis is
                    #   normalised ASCENDING in _window_sweep — both drivers could sweep
                    #   descending, but scqat's peak_fit inverts its width bound on a
                    #   descending axis and mis-fits SILENTLY, so the one ordering point
                    #   is window_bounds() (never a chained start <= x <= end).
                    #   THE FRAME IS IN THE
                    #   FIELD NAME and the two mixins are independent SIBLINGS (unlike
                    #   the flux frames, where _pulse subclasses absolute): one
                    #   experiment could carry both, and shared names would merge by
                    #   MRO into one number driving two sweeps);
                    #   catalog
                    #   `capabilities` are DERIVED from mixin subclassing — never declared
                    #   strings, zero capabilities legitimate (new experiments may be
                    #   unclassifiable); CAPABILITY_SUMMARIES = the curated one-liner
                    #   per capability behind `scqo run --capability` (keys pinned to
                    #   the derived set by test_capabilities)
    _gate_target.py             # which drag knob a target_gate selects (drag_knob)
    _overlap.py                 # the concurrent-tone timing shared by the overlap probe
    _punchout.py                # the two punchouts' shared branch/plateau extraction
    _time_grid.py               # the shared swept-TIME axes. log_time_axis_ns SHRINKS:
                                #   points snap to the 4 ns grid and de-duplicate, so the
                                #   realized length is < num_points - read axis.size
    _transmon_estimate.py       # shared transmon arithmetic behind the flux map + punchouts
    _depletion.py               # the post-readout photon-depletion wait: THE precedence
                                #   helper depletion_wait_ns + the kappa->wait formula.
                                #   The readout twin of qubit_reset one level over -
                                #   kappa_tot_hz (fact) x depletion_factor -> the
                                #   readout channel's readout_depletion_s knob, exactly
                                #   as t1_s -> thermalization_time_s
    _distortion_hint.py         # the cryoscopes' operator hint: the taps are FACTS, so
                                #   the command that writes them into the VENDOR config
                                #   comes from the backend (duck-typed hook
                                #   distortion_apply_command) and is printed on stderr
                                #   at writeback; no hook = the manual step, named
    _drive_power.py             # shared recorded set->revert drive_power_dbm boundary
    _flux_component.py          # kind-agnostic foreign flux source mixin (record-only guard)
    _sim.py                     # shared helpers for the offline simulators
    <name>.py                   # ONE module per registered experiment - the physics half
                                #   (define_sweep/simulate/estimate/update); a driver adds
                                #   only probe(). The full list is the census below; each
                                #   one's description lives in the registry, read it with
                                #   `scqo run <name> --help`
tests/test_model_run.py         # catalog -> run -> suggest -> accept, no hardware
tests/test_datastore.py         # run folders + index + tags + reindex, no hardware
tests/test_campaign.py          # the pure aggregator + run_campaign orchestration
```

### The registered experiments

<!-- BEGIN generated: experiments -->
**41 registered experiments.** This list is GENERATED from the registry
(`scqo.catalog()`) - refresh it with `python scripts/update_docs.py`. Descriptions are
catalog-quality and live in the registry, never here: read one with
`scqo run <name> --help`, or browse by capability with `scqo run --capability <name>`.

```
broadband_qubit_spectroscopy        qubit_parity_switch_discrete        qubit_t1_ade
broadband_resonator_spectroscopy    qubit_pi_pulse_error                qubit_t1_bayesian
pair_swap_chevron                   qubit_power_rabi                    qubit_thermal_population
pair_swap_flux_map                  qubit_ramsey                        qubit_tomography
pair_zz_coupler                     qubit_ramsey_cryoscope              qubit_xyz_delay
qc_n_stark_amp                      qubit_ramsey_phasor                 readout_frequency
qc_n_swap_amp                       qubit_relaxation                    readout_power
qubit_deterministic_benchmarking    qubit_relaxation_flux_pulse         resonator_spectroscopy
qubit_drag_alternating              qubit_spectroscopy                  resonator_spectroscopy_flux
qubit_drag_equator                  qubit_spectroscopy_cryoscope        resonator_spectroscopy_power_amp
qubit_echo                          qubit_spectroscopy_flux_pulse       resonator_spectroscopy_power_chain
qubit_echo_flux_pulse               qubit_spectroscopy_overlap          single_shot_readout
qubit_parametric_drive              qubit_sqrb                          single_shot_readout_gef
qubit_parity_switch_continuous      qubit_stark_phase_echo
```
<!-- END generated: experiments -->

### Datastore (the "find my measurement data" layer)
`Session(backend, data_root=...)` persists **every** run — raw dataset (`dataset.nc`),
parameters/result/record JSONs, device before/after snapshots, and the scqat artifacts
(metadata / plotdata / figure PNGs, per qubit) — under
`<data_root>/<device>/<YYYY-MM-DD>/<run_id>/`. The **run folder is the truth**;
`<data_root>/index.sqlite` is a disposable cache (`python -m scqo <data_root>`
rebuilds it). Query with `Session.find_runs(experiment=, target=, tag=, since=, outcome=,...)`,
reload with `load_run(run_id)` / `datastore.open_dataset(run_id)`. A **campaign**
persists one extra folder, `<data_root>/<device>/campaigns/<campaign_id>/`
(`campaign.json` = plan + status + statistics + the aggregate SUGGESTIONS and their
decisions, rewritten per repeat; `repeats.jsonl` = append-only skeleton, run_ids and
timing, never fit VALUES) — a sibling of the day folders because an overnight campaign
crosses midnight, and invisible to every glob over the data root. Post-finalize
decisions go ONLY through the locked `edit_campaign_suggestions` (persist_campaign is
the running process's lockless whole-file write); the campaigns table carries a
derived `suggestions_pending` column (schema v10) behind `find_campaigns(pending=)`,
and applied values stamp `ChangeRecord.campaign_id` (provenance status "campaign";
decide via `scqo accept --campaign <id>`, regenerate pre-feature campaigns via
`scqo campaign --suggest <id>`). Its children are ordinary runs stamped `campaign`/`repeat_idx`/`step_idx`
in an INDEXED column (never a `campaign:<id>` tag — that would be an unindexed
`json_each` scan and a second grouping authority); walk them with
`campaign_runs(campaign_id)`, which is unlimited and in execution order, NOT
`find_runs(campaign=...)`, which is newest-first and capped at 50.
The manifest is finalized in a `finally`, so it is written on EVERY exit path -
normal stop, Ctrl-C anywhere (including the cadence sleep, which is where a
`period_s` campaign spends most of its wall clock), or an unexpected error
(`status="failed"`). An interrupted repeat KEEPS the steps that already ran, marked
`partial` and counted in `repeats_partial`, never in `repeat_done` - the data is
already on disk and campaign-stamped, so discarding it would leave `campaign_runs()`
returning more children than the manifest admits. `run_campaign` never prints - it
emits `cadence_wait`/`repeat_start`/`step_done`/`repeat_done` to an `on_progress`
callback and the CLI renders (`cli/_campaign.py::progress_lines`), and
`cli/_campaign_plot.py` writes `statistics.png` into the campaign folder on finalize
(lazy Agg matplotlib; a figure failure warns and NEVER fails the campaign). Those lines go to **stderr**, `#`-prefixed,
ASCII, plain newlines and **never `\r`**: stdout must stay `| jq`-parseable, and on
QM the vendor's `progress_counter` already rewrites a bar there. `"stop"` from the
callback is honoured only at `repeat_done` — stopping mid-repeat would leave a
half-walked bundle whose quantities no longer share a drift epoch. Runs carry searchable
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
Default for a localized change (from the repo root): `uv run pytest tests/test_model_experiments.py -k ramsey -q`.
Selection map for experiment work (`scqo/experiments/<name>.py`) — always the first row, plus any that apply:

| Also changed | Add to the run |
|---|---|
| *always* | `tests/test_model_experiments.py -k <stem>` |
| a capability mixin (`_capabilities/`) | `tests/test_capabilities.py` **+ `tests/test_model_experiments.py` UNFILTERED** — drop the `-k`: a mixin edit is shared-core for every experiment that subclasses it, and only the full every-experiment sweep catches the ones you didn't think of |
| a time axis (`idle_time_ns`-style grid) | `tests/test_time_grid.py -k <stem>` |
| `Contract` / `define_sweep` | `tests/test_contract.py` (small — run whole) |
| a `*_method` Literal | `tests/test_estimator_method_sync.py` |
| a `catalog.py` FieldSpec | `tests/test_model_catalog.py` |
| Parameters defaults/overlay plumbing | `tests/test_parameter_defaults.py` |
| `campaign.py` / `run_campaign` / the campaign CLI | `tests/test_campaign.py` + `tests/test_cli_campaign.py` |
| `report.py`'s catalog-derived field orders | **+ `tests/test_viewer.py`** — the viewer imports them |

`-k` takes the **distinctive stem, not the registered name**: `-k ramsey` matches both
`test_every_experiment_runs_clean[qubit_ramsey]` and `test_ramsey_writes_drive_freq_fact_twin_and_t2`,
while `-k qubit_ramsey` misses the second. **0 collected means the filter was wrong** — widen it, never skip.
Leave `test_cli_*.py` (many subprocess spawns), `test_index_scale.py` (100k rows) and `test_viewer.py` alone
unless the edit is in `scqo/cli/`, `scqo/datastore.py` or `scqo/viewer/` respectively.

The **full suite** (`uv run pytest -q`) takes minutes, dominated by the `test_cli_*.py` subprocess
tests. It is for exactly two cases: (1) cutting a release, and (2) an edit to **shared core**, which
means one of `catalog.py`, `entities.py`, `roster.py`, `stores.py`, `device.py`, `experiment.py`,
`session.py` — **or a `_capabilities/` mixin**, whose blast radius is every experiment that
subclasses it (the row above says so; this list is the same claim, not a narrower one).
Otherwise **report the exact command run** and offer the full-suite command instead of spending the
minutes unasked.

No test or timing counts are quoted here on purpose: they rotted two releases deep last time
(771 was the v2.1.0 number, still printed at v3.1.0 when the real figure was 868). Each release
records the per-repo counts it actually validated — see the `OFFLINE-VALIDATED` line in the
matching [RELEASES.toml](RELEASES.toml) block.

### Experiment governance + promotion checklist
Three roles, distinguished by ACCESS rather than seniority:

1. **Operators** use the `scqo` command (`scqo run` / `scqo find` / `scqo user`) with
   `~/.scqo/config.toml`; they change nothing in these repos.
2. **Contributors** prototype a new experiment + estimator **in a fork** and open pull
   requests back — [CONTRIBUTING.md](CONTRIBUTING.md) has the layout, the branch/merge
   order across repos, and what a PR must prove. (The `scqo-contrib` sandbox that used to
   be this tier is **retired**: it is private, pinned at v0.12.0, excluded from every combo
   since greenfield, and its template imports a `QubitSelection` that greenfield renamed to
   `TargetSelection` — so it fails on import and cannot even be copied from. Do not route
   anyone there.)
3. **A maintainer merges** the promotion — someone with push rights on these repos, which on
   a fork PR means the reviewer. Checklist, which is also what a PR is reviewed against:
   - [ ] `DatasetContract` declared; probe output validated against it on the real instrument.
   - [ ] `simulate()` implemented -> offline end-to-end test in `tests/`.
   - [ ] Estimator lives in scqat with metadata (+ figures) outputs.
   - [ ] `update()` writes only catalogued fields (extend the kind catalog in `catalog.py` first if needed).
   - [ ] Ran repeatedly with findable data; results reviewed via `find_runs`. State plainly
         whether that was on hardware or offline — a PR records this as
         `offline` / `hardware <chip> <date>` / `unverified`.
   - [ ] `description` is catalog-quality (an AI reads it to decide).
   - [ ] Physics half in `scqo/experiments/`; driver `probe()` subclasses registered under
         the core `scqo.experiments` group (then directly runnable via `scqo run <name>`).
   - [ ] `python scripts/update_docs.py` re-run, so the experiment census in this file
         includes it — `tests/test_docs_current.py` fails otherwise.

**`scqo run <name>` is the single CLI entry point** — never add wrappers, launcher stubs,
or per-command shims. `scqo campaign <plan.toml>` is not an exception: it is a different
verb over a different INPUT (a plan, not an experiment name), and it REFUSES a bare
experiment name in code, pointing at `scqo run <name> --repeat N` — so "exactly one way
to run one experiment" is a checked property, not a convention.

### The readout schema (digest — full text: TUTORIAL §11)
Readout output is DECLARED, never inferred: `use_state_discrimination` (analog I/Q vs
digital) × `readout_mode` (shot vs average; `ReadoutModeParameters`, only on experiments
realizing both) + the multi-qubit joint form. Variable names carry the semantics —
`state` is ALWAYS a per-shot integer LEVEL (qutrit-capable, never a probability),
`population` the averaged marginal, `joint_population` the averaged joint distribution
of a multi-qubit target (a pair stores JOINT; marginals = partial trace, derived not
stored). `sweeps` are physics axes; readout adds its own dims (`shot_idx`, `member`,
`joint_state`) declared per accepted form via `DatasetContract.readout_dims` /
`alt_readout_dims` and validated with sweep rigor. `member`/label-digit order = roster
roles (high, low; `member` coord carries the ROLE labels). `joint_state` labels are
generated per-member level digits ("00".."11", "02" when f-resolved), never hand-listed.
A backend that cannot realize a combo REFUSES by name. Helpers live once in
`_capabilities/state_readout.py` (`states_to_joint_population` / `joint_to_marginals` /
`joint_state_labels` / `member_order`); drivers import them, scqat only reads datasets.

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

### The sibling repos
All four are public. They must be cloned as **siblings under one parent directory, under
their own names** — `pyproject.toml` resolves `scqat` as `{ path = "../scqat" }` and each
driver resolves `scqo` as `{ path = "../SCQO" }`, so a renamed or nested folder breaks the
install. [CONTRIBUTING.md](CONTRIBUTING.md) has the layout.

- **[scqat](https://github.com/shiau109/scqat)** — the analysis half: every estimator and
  fitter SCQO lazy-imports from `estimate()`. A hard dependency, and the one repo whose
  version SCQO pins a floor against (see `RELEASES.toml`).
- **[scqo-qm](https://github.com/shiau109/scqo-qm)** (was LCHQMDriver) - Quantum Machines (qm-qua / quam / qualibrate); the scqo surface is the `scqo_qm` package (backend/ + experiments/, one fused file per experiment); the qualibrate GUI serves the vendored OFFICIAL nodes only (the LCH shells are retired; `customized/` is a frozen archive); `quam_config/my_quam.py` stays the QUAM entrypoint.
- **[scqo-qblox](https://github.com/shiau109/scqo-qblox)** (was LCHQBDriver) - Qblox (qblox-scheduler); the `scqo_qblox` package, independent of the QM stack.
- **QBLOX_training** - the vendor's read-only Qblox example repo (`docs/applications/superconducting/...`). It is a LOCAL reference checkout on the lab machine, not part of this project and not needed to build or test anything here.

## Status
Current published release: **v3.1.0** — see `RELEASES.toml` for the combo manifest and required upgrade actions. Release history lives in git tags + `RELEASES.toml`, not here.
