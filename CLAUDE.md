# SCQO — Superconducting Qubit Orchestration (instrument-agnostic)

## Why this repo exists
Run superconducting-qubit calibration experiments at the level of **protocol + parameters**, independent of
the instrument backend. Two existing lab repos do the same physics on different hardware; SCQO is the
neutral layer above them, and the substrate for **AI-driven experiment loops** (decide approach + params →
run → analyze → extract → decide next).

## Terminology (canonical vocabulary — single source of truth)
The word **"protocol" is retired**; use these names across all repos.

- **Experiment** — the registered, instrument-agnostic unit SCQO catalogs and dispatches to a backend (QM or Qblox). Owns its **Parameters**; binds a probe + an estimator.
- **probe** — the acquisition half: build the instrument sequence (QM program / Qblox schedule) and run it → **Dataset** (xarray). On the simulated backend the probe runs the **model** forward to synthesize data ("simulation = virtual experiment").
- **estimator** — the analysis half: fit the Dataset to a **model** → **Result** (extracted model parameters). Implemented in scqat (`scqat.estimators`); its orchestrator method is `analyze()`.
- **tool** / **fitter** — reusable helpers an estimator imports (`scqat.tools`); a fitter is the common case. Many-to-many; **tools never import estimators**.
- **model** — the physics that predicts the signal; used *forward* by a simulated probe and *inverse* by an estimator. SCQ.jl builds/simulates models; scqat fits them.
- **Parameters / Result / Backend / Session** — input schema / extracted output / instrument adapter (QM, Qblox, Simulated) / the orchestrator entry point (`catalog()` / `run()` / `device_state()`).

**Naming status (2026-06-08):** scqat is migrated (`estimators/`, `tools/`, `BaseEstimator`, `*Estimator`). **SCQO's own code and the sections below still use the legacy names** `Protocol` / `build → run → analyze → update` / `scqo.protocols`; renaming them to **Experiment / probe / estimate** is a pending pass, and LCHQBDriver mirrors the legacy names until then. (QBLOX_training documents Qblox's *own* `Experiment` ABC — a different class from this `Experiment`.)

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
2. Protocol framework: real framework + GUI vs thin ABC.
3. Pulse/sweep DSL: QUAM macros vs scheduler operations.
4. Device-model attribute names: `q.f_01` / `q.xy.RF_frequency` vs `q.clock_freqs.f01` / `q.clock_freqs.readout`.

## Target architecture (AI-drivable, backend-neutral)
Adopt qualibrate's *patterns*, generalized so QM and Qblox are adapters:

- **Parameters**: pydantic schema per protocol (introspectable: names, types, ranges, defaults, docstrings).
- **Protocol registry**: named, described catalog of measurement approaches (the AI's decision menu).
- **Protocol lifecycle**: `build → run → analyze → update` (neutral; each backend implements `build`/`run`).
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
  backend.py      # Backend ABC: .device + .acquire(protocol) -> xarray.Dataset
  protocol.py     # Protocol ABC: physics half (define_sweep/simulate/analyze/update) + backend half (build)
  registry.py     # @register / get / catalog  (AI's menu of measurements)
  session.py      # Session: catalog() / run() / device_state()  — the one human+AI entry point
  testing.py      # InMemoryDevice + SimulatedBackend (run with no instrument)
  protocols/
    resonator_spectroscopy.py   # frequency sweep, Lorentzian dip -> updates readout_freq
    ramsey.py                   # time sweep, decaying-cosine fit -> updates drive_freq + T2*
    power_rabi.py               # amplitude sweep, cosine fit -> updates pi_amp
tests/test_end_to_end.py        # catalog -> run -> writeback, no hardware
```

### How a driver adds an experiment
1. Subclass the backend-free protocol from `scqo.protocols`.
2. Implement only `build()` for the instrument (lazy-import the vendor lib inside it).
3. `@register` the subclass so it appears in `catalog()`.
Parameters, Result, `analyze`, `simulate` and `update` are inherited unchanged.

### Reference backends
- `D:\github\LCHQMDriver` — Quantum Machines (qm-qua / quam / qualibrate).
- `D:\github\LCHQBDriver` — Qblox (qblox-scheduler). Independent of the QM stack.

## Status
Core scaffolded and tested offline via `SimulatedBackend`. Three worked protocols prove
the pattern across all three sweep types and device fields:
frequency->`readout_freq` (resonator spec), time->`drive_freq`+T2* (Ramsey),
amplitude->`pi_amp` (power Rabi). More protocols + the real backends follow the same pattern.
