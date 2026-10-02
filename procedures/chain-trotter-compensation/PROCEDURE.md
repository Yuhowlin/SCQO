---
name: chain-trotter-compensation
description: Take two calibrated pair operations into the unidirectional Trotter chain. Calibrate the chain's per-round source-sink phase compensation with qc_trotter_compensation, write it into the chain parameter file, and run qc_unidirectional_trotter. Use after pair-partial-swap has produced both pairs' operations for the target (theta1, theta2), and again after any change to either operation or to the round's timing.
goal: A chain parameter file whose compensation_amps zero the source-sink phase per round, and a qc_unidirectional_trotter run on it.
inputs:
  chain: source, relay and sink qubits (5Q4C q1, q2, q3)
  operations: the two pair operations from pair-partial-swap
  operation_gap_ns: the idle between a round's operations (20 on 5Q4C)
  max_rounds: the round-count axis (20)
outputs:
  compensation_amps: "{<sink>: a}, the amplitude factor of the sink's stark tone"
  trotter_run: sink population against round count
experiments: [qc_trotter_compensation, qc_unidirectional_trotter]
backends: [qm]
depends_on: [pair-partial-swap]
validated: hardware 5Q4C q1-q2-q3, 2026-09-22 (theta = 0.30/0.30, 0.60/0.60 and both mixed combinations)
---

# chain-trotter-compensation

## Physics in brief

- **One round:** swap 1 (source → relay), swap 2 (relay → sink), reset of the relay, then
  the stark tones.
  - Only the **differential** source − sink phase per round is observable, and 0 maximizes
    the sink.
  - The relay's tone is inert: it plays after the reset, onto an emptied qubit.
- **That phase belongs to the combination of the two operations.** The first pair's swap
  adds its share to the source's phase, the second pair's swap to the sink's.
  - The shares add independently: on 5Q4C the four 030/060 combinations close to within
    0.008 turn.
  - They are large: replacing either operation moved the phase by about 0.06 turn.
  - The pair calibrations cannot see these shares (see *Predicting the compensation from
    the pair calibrations*). The compensation is therefore measured in the chain, once per
    combination.
- **Ideal sink after M rounds** with that phase at 0, for pair angles (θ1, θ2):
  P(M) = [sin θ1 sin θ2 Σ_{j<M} cos^(M−1−j)θ2 cos^j θ1]².
  - For equal small angles it peaks at about 4/e² ≈ 0.54, at M ≈ 2/θ²: round 22 for
    θ = 0.30, round 5 for 0.60.
  - Larger angles reach the same peak in fewer rounds, so decoherence costs them less.

## Prerequisites

1. `pair-partial-swap` done for both pairs, with the readout freshly checked on all three
   qubits.
2. One parameter file per operation combination, shared by both experiments, so they cannot
   disagree about the round. 5Q4C uses `~/.scqo/chain.json` (030/030) and
   `~/.scqo/chain_060.json` (060/060); `--params` also reads TOML.
   - It holds only keys **both** Parameters classes accept. Parameters is `extra="forbid"`:
     a compensation-only key in the file makes `qc_unidirectional_trotter` refuse it.
   - Compensation-only keys go on the command line: `compensation_target`,
     `min_compensation_amp`, `max_compensation_amp`, `num_amp_points`.
   - Set every key explicitly so `~/.scqo/parameters.toml` cannot leak in. The stderr line
     `# parameter defaults from ...` lists anything that did.

   ```json
   {
     "targets": ["q1", "q2", "q3"],
     "first_pair": {"pair": "q1_q2", "operation": "partial_swap_030"},
     "second_pair": {"pair": "q2_q3", "operation": "partial_swap_030"},
     "reset_qubit": "q2",
     "reset_operation": "reset",
     "idle_reference_operation": "partial_swap",
     "swap_coupler_flux": {},
     "stark_operation": "stark",
     "stark_detuning_hz": 50000000.0,
     "compensation_amps": {"q3": 0.33},
     "prep_qubit": null,
     "prep_operation": "x180",
     "operation_gap_ns": 20,
     "max_rounds": 20,
     "reset_method": "thermal",
     "thermalization_time_ns": null,
     "active_reset_rounds": 1,
     "readout_mode": "average",
     "num_averages": 400
   }
   ```

## Steps

### Step 1: operations into the file

Set `first_pair` and `second_pair` to the operations from `pair-partial-swap`. A
compensation measured for another combination does not carry over: run Steps 2–3 again
after changing either operation.

### Step 2: coarse compensation scan

- **Run**
  `scqo run qc_trotter_compensation --params chain.json --set compensation_target=<sink> --set max_compensation_amp=1.0 --set num_amp_points=31`.
  - If `compensation_amps` already holds the sink, add `--set "compensation_amps={}"`. The
    experiment refuses a qubit that is both swept and held fixed.
  - Takes about 1.5 minutes.
- **Read** `best_compensation_amp_refined` ± `best_compensation_amp_err` and the flag
  `compensation_unresolved`.
  - The refined value is the parabola vertex of the sink averaged over rounds ≥ 2, within
    ±0.08 of that curve's grid best. Rounds 0 and 1 carry no phase information: the sink
    holds at most one path. Starting at round 2 or at round 4 changed the result by ≤ 0.002
    on four scans.
  - The map draws it as the solid line, with its spread as a band.
  - Do not take `best_compensation_amp`, the dotted line. It is the single brightest
    (amplitude, round) pixel, and it landed 0.02–0.04 away from the ridge centre on 5Q4C
    (Trap 4). `best_sink_p_max` and `best_n_at_max` describe the transport at that pixel.
  - If `compensation_unresolved` = 1, the scan shows no interior peak; `refine_reason` in
    the estimator metadata says why. Move or widen the window, staying at or below 1.0.
- **Decide**:
  - Sweep only the **sink** and leave the source untoned. Only the difference matters.
  - On 5Q4C, q1's tone at 5192.9 MHz sits 2.6 MHz from q3 and would drive it.
  - The window stops at 1.0, one full turn (`procedures/README.md`), so it holds exactly
    one optimum. The same phase one turn higher (1.05 for 030/030 at 20:14) is never used:
    a tone that strong drives the qubit.

### Step 3: fine scan

- **Run** the same command with `min_compensation_amp` and `max_compensation_amp` at the
  best value ± 0.12, clipped to 0–1.0, and 25 points (step 0.01).
- **Read** `best_compensation_amp_refined` ± `best_compensation_amp_err` again: that is
  the compensation. The spread was ±0.001–0.003 on 5Q4C.
  - On 5Q4C a coarse-scan vertex landed within 0.004 of the fine one run minutes later
    (060/060), so the fine scan is a confirmation.

### Step 4: write it and run the chain

- Write `"compensation_amps": {"<sink>": a}` into the chain file.
- **Run** `scqo run qc_unidirectional_trotter --params chain.json` (about 15 s).
- **Read** `sink_p_max` and `n_at_max`, and each qubit's population against round count.
- **Check** against the ideal curves the figure draws dashed beside the source and the sink
  (Physics in brief, with each operation's `theta_rad`; `ideal_sink_p_max` in the fit).
  - They need both operations declared in the roster with an accepted `theta_rad`
    (`pair-partial-swap` Step 4); otherwise stderr names what is missing.
  - Put `round_duration_ns` in the chain file (360 on 5Q4C at gap 20) and keep the source's
    and sink's `t1_s` / `t2_star_s` measured: both theories then carry that decay — the
    discrete Trotter model (hollow points) and the master equation (line), the ideal one
    left as a faint ceiling; `model_sink_p_max` in the fit. On 5Q4C 2026-09-29 (040/040,
    run `20260929-110252-157`) the decayed model's sink peak read 0.36 against 0.338
    measured and 0.542 ideal.
  - The SOURCE curve is the clean probe: it depends on the first angle alone, so a source
    that falls faster than cos^(2N)θ₁ is loss outside the swaps (5Q4C 2026-09-28: q1's T1
    had halved), not a swap error.
  - For a decay, fit c + A·P(M)·e^(−M/τ) by hand. With the ideal peak beyond `max_rounds`
    (030/030), A and τ trade off, so fix A at the readout contrast (0.9 on 5Q4C) to compare
    runs.

## Predicting the compensation from the pair calibrations

This was tried on 5Q4C and is **not reliable**. It is kept for the round-length
measurements and for the reason it fails. In turns, modulo 1:

    s_sink(a*) = s_sink(a_sink,pair) − s_source(a_source,pair) + (f_source − f_sink)·(T_chain − T_pair)

- **s_q(a):** qubit q's measured stark phase curve.
- **a_sink,pair and a_source,pair:** the compensations the two pairs measured in
  `pair-partial-swap`.
- **f:** the frame (drive) frequencies.
- **T:** the **actual** round lengths, which differ from the nominal ones.

On 5Q4C the gateway simulator measured:

| round | nominal | actual | where the difference comes from |
|---|---|---|---|
| chain | 340 ns | 360 ns | the reset macro's `update_frequency` / `reset_if_phase` and the aligns add 20 ns |
| pair, gap 260 | 360 ns | 368 ns | 8 ns of overhead |
| pair, gap 252 | 352 ns | 360 ns | 8 ns of overhead |

f_q1 − f_q3 = −47.4 MHz, so one 4 ns clock cycle shifts the phase by 0.19 turn.

Outcome on 2026-09-22:

- **Equal operations:** predicted 0.25 (030/030) and 0.37 (060/060), measured 0.23 and
  0.327. That looked close, but the 030/030 optimum later drifted to 0.33 with the vendor
  config unchanged (Trap 3), so the agreement says little.
- **Mixed operations:** the q1_q2 pair compensation hardly moves between 030 and 060, while
  q2_q3's does. The relation therefore predicts that the optimum follows the second pair:
  (060,030) ≈ (030,030) and (030,060) ≈ (060,060). Measured within one half hour:

  | first / second | predicted | measured |
  |---|---|---|
  | 030 / 030 | 0.25 | 0.330 |
  | 060 / 030 | 0.25 | 0.402 |
  | 030 / 060 | 0.37 | 0.243 |
  | 060 / 060 | 0.37 | 0.332 |

  In q3's stark phase, replacing the first pair's operation moved the optimum by
  −0.064 turn and replacing the second pair's by +0.063 turn. That is as much for the first
  pair as for the second, and for the second pair the prediction had the opposite sign.
- **Likely reason (not verified):**
  - A pair experiment measures only the phase difference between the pair's two qubits.
  - During a swap both of them sit at a shared frequency, which the coupler pulse shifts.
    That shared part cancels inside the pair.
  - In the chain it adds to the source's phase (first swap) or to the sink's (second
    swap).
  - Both 060 coupler pulses are about 9 mV stronger than the 030 ones. Both shares came
    out near 0.064 turn, i.e. about 1.6 MHz over the 40 ns pulse.
- **What does work:** the shares add in phase. Measure three combinations and the fourth
  follows from s(o1,o2) = s(o1,x) + s(y,o2) − s(y,x), where s is the sink's stark phase at
  the optimum. On 5Q4C that predicted each combination from the other three within
  0.006–0.014 in amplitude.

## Stop criteria

- Done when the fine scan's `best_compensation_amp_refined` is written into the chain file
  and the trotter run completes.
- Abort on any hardware or gateway error, as `procedures/README.md` defines it.

## Traps

1. **Any timing change moves the optimum:** the gap, the reset length, the stark length, or
   align overhead. Rerun Steps 2–3 after changing the round.
2. **Any operation change moves it too.**
   - *Case:* 5Q4C, 2026-09-22 22:47–23:01. Replacing the first pair's 030 by 060 moved
     q3's optimum from 0.330 to 0.402; replacing the second pair's moved it to 0.243.
   - *Cost:* keeping the old value (0.07–0.09 off) loses roughly 40–60% of the sink's gain
     over background.
   - *Cure:* rescan after any operation change, or predict from three measured
     combinations.
3. **Drift.**
   - Within 30 minutes: 0.23–0.24, 0.21–0.22 and 0.228–0.230.
   - Over hours: 030/030 moved from 0.23 (20:14) to 0.33 (22:47). The setup snapshots of
     the two scans differ only by operations added in between.
   - Repeats 10 minutes apart then agreed within 0.002.
   - Run Steps 2–3 right before a trotter run.
4. **`best_compensation_amp` is the brightest pixel, not the ridge centre.** On 5Q4C:
   - 060/060: 0.367 against the round-averaged vertex 0.331 (coarse), and 0.350 against
     0.327 (fine).
   - 030/030: 0.25 against 0.232 (coarse), and 0.23 against 0.224 (fine).
   - Read `best_compensation_amp_refined` (Step 2).
5. **One file, two consumers.** Keep compensation-only keys out of the chain file, and add
   `--set "compensation_amps={}"` when rescanning a sink that is already compensated.
6. **Stale readout,** as in `pair-partial-swap`.
7. **The repetition period moves the operating point.**
   - `reset_method="active"` shortens each shot to ~15 µs, so the chain's large z and
     coupler pulses fill most of the time instead of ~4 % of it.
   - *Case:* 5Q4C 2026-09-29, 040/040 at a compensation scanned with thermal (301 µs)
     shots, tag `active-reset-ab`. Sink mean over N = 4–14: 0.30 thermal, 0.25 active; the
     source kept 0.795 per round instead of 0.81. A thermal reset with a 10 µs wait gave
     0.25 and 0.79, and 60 µs gave 0.28 and 0.81. So the loss follows the repetition
     period, not the reset. Active reset itself did its job: the N=0 residual fell from
     0.06 to 0.02 on q2 and from 0.07–0.09 to 0.05 on q3.
   - *Cure:* run Steps 2–3 with the SAME `reset_method` (and wait) as the trotter run. Put
     `reset_method` in the chain file, so both experiments read it.

## Typical values

5Q4C cooldown cd2, 2026-09-22. Gap 20 gives a 360 ns round in every combination, since all
four operations are 40 ns. 20 rounds, 400 averages, compensation on q3 only:

| first / second pair | q3 compensation | sink maximum (round) | ideal peak (round) | decay, A fixed at 0.9 |
|---|---|---|---|---|
| 030 / 030 | 0.23 (20:14), 0.33 (22:47) | 0.2675 (12) at 0.23; 0.273 (12) at 0.33 | 0.538 (≥ 20) | 14.9 ± 1.2 rounds |
| 060 / 030 | 0.402 | 0.260 (7) | 0.395 (10) | 15.3 ± 1.3 rounds |
| 030 / 060 | 0.243 | 0.247 (7) | 0.387 (10) | 11.8 ± 0.9 rounds |
| 060 / 060 | 0.327–0.332 | 0.37–0.44 (4–5), three runs | 0.547 (5) | 12.5 ± 1.3 rounds |

- **The q3 tone itself:** q3's `stark` operation is a 60 ns pulse stored at 0.4364 of full
  scale, played 50 MHz above q3. A factor of 0.33 plays 0.144 of full scale and moves q3 by
  −0.143 turn per round (about 2.4 MHz for those 60 ns). The phase grows roughly with the
  square of the factor, with 1.0 set to one full turn.
- **Decay:** it looks set by the second pair's operation (about 15 rounds with 030, 12 with
  060), not by the tone's strength. The cause is open.
- **Fit dependence:** the 030/030 run at 20:20 gave 19 rounds with A free (0.93), so decays
  compare only within one fit model.

## Evidence

5Q4C runs (all `20260922-`):

- **030 / 030:** coarse scans `194717-192`, `200826-906`; fine scan `201332-432`; trotter
  `202034-457`.
- **060 / 060:** coarse scan `222749-171`; fine scan `222941-753`; trotters `223659-049`
  (14 rounds), `224700-690`.
- **Combination scans, in run order:** `224737-805` (030/030), `224921-396` (060/030),
  `225414-374` (030/060), `225557-608` (060/060), `225740-356` (030/030), `225922-725`
  (060/030).
- **Combination trotters:** `230954-808` (030/030), `231009-683` (060/030), `231024-703`
  (030/060), `231040-123` (060/060).

## Open issues

`BACKLOG.md`:

- **F16:** recording the actual round length
- **F17:** a shorter round, with the stark tones played during the relay reset
- **F18:** a stark amplitude-to-phase conversion, so the one-turn bound can live in code
