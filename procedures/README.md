# Procedures

A **procedure** is a goal-driven, adaptive sequence of cataloged experiments that produces or
tunes something the device carries, such as a partial-swap operation of a chosen angle. Each
step reads named `result.fit` keys and decides what runs next. Where a step needs a
backend-side tool, for example registering a new operation in the vendor config, the
procedure names that tool.

| | experiment | campaign | procedure |
|---|---|---|---|
| unit | one run, one estimator | a fixed step list walked N times | a sequence that branches on results |
| decides | nothing (it may *propose* knob updates) | nothing (it aggregates statistics) | the next step, its parameters, when to stop |
| output | a Result | statistics over repeats | a validated device configuration + the evidence for it |
| home | `scqo/experiments/` | a plan TOML (`scqo campaign`) | `procedures/<name>/PROCEDURE.md` |

Procedures are written for operators and AI agents alike. They cover the decide half of the
loop (decide, run, estimate, extract, decide next) that the catalog cannot express. They live
in this repository so that the experiment names, parameters and fit keys they cite change
together with the code. The TUTORIAL teaches the tools. A procedure records how to reach a
goal with them, including the traps met on real hardware.

## File format

One directory per procedure, holding `PROCEDURE.md` (plus any data file the procedure itself
needs). The file opens with YAML front matter:

```yaml
---
name: pair-partial-swap              # = the directory name
description: One or two sentences: what it produces and WHEN to use it.
goal: The measurable end state.
inputs: {name: meaning, ...}
outputs: {name: meaning, ...}
experiments: [registered experiment names, in the order used]
backends: [qm]                       # where every step can be executed today
depends_on: [other procedure names]  # optional
validated: hardware 5Q4C 2026-09-22  # offline | hardware <chip> <date> | unverified
---
```

The body keeps these sections, in this order, so an agent can find them without reading
everything:

1. **Physics in brief**: only what the decisions need.
2. **Prerequisites**: state that must hold before step 1.
3. **Steps**: each one with **Run** (the experiment and the parameters that matter), **Read**
   (the exact `result.fit` keys or plotdata variables) and **Decide** (the rule that picks the
   next step).
4. **Stop criteria**: success, and when to abort.
5. **Traps**: failure modes seen on hardware, each with its symptom.
6. **Typical values**: chip-scoped and dated, never presented as constants.
7. **Evidence**: run ids behind the `validated` line.
8. **Open issues**: pointers to `BACKLOG.md` entries.

Rules:

- Cite experiments by registered name and fit keys exactly as `result.fit` spells them.
- A hardware step needs the operator's go-ahead. Abort on any hardware or gateway error
  (non-zero exit, `error` in the result, qm/grpc exceptions, `DEADLINE_EXCEEDED`, a run
  taking more than three times its expected time): report it and do not debug the
  instrument.
- When a step's rule changes, update the procedure in the same commit as the code change that
  caused it.
- A stark compensation tone stays below one full turn of stark phase. Take the solution with
  the lowest amplitude, and stop every scan window at one turn (5Q4C: amplitude factor 1.0,
  since the `stark` operations are scaled so that 1.0 is 2π on q1 and q3). A stronger tone
  starts to drive the qubit instead of only shifting it. A window wider than one turn also
  holds two solutions, which can split an estimator's criteria.

## Index

| procedure | produces | backends | validated |
|---|---|---|---|
| [pair-partial-swap](pair-partial-swap/PROCEDURE.md) | a square partial-swap operation of angle θ on one pair, at resonance | qm | hardware 5Q4C q1_q2 + q2_q3, 2026-09-22 (θ 0.30, 0.60) |
| [chain-trotter-compensation](chain-trotter-compensation/PROCEDURE.md) | the chain's per-round phase compensation, then a `qc_unidirectional_trotter` run | qm | hardware 5Q4C q1-q2-q3, 2026-09-22 (four 030/060 combinations) |
| [qubit-frequency-park](qubit-frequency-park/PROCEDURE.md) | a flux-tunable qubit parked at its apex (or at a chosen f01) to 0.2 mV, with its drive | qm | hardware 5Q4C q1, 2026-09-26 (apex, from a +8 mV offset) |
