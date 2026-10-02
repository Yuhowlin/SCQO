---
name: pair-partial-swap
description: Develop or re-tune a square partial-swap operation of a target angle theta on one qubit pair, played at the swap resonance, and measure its per-round stark compensation. Use when a Trotter chain or any repeated-swap experiment needs a new swap angle, or when an existing angle has drifted.
goal: A stored operation partial_swap_<t> on the pair whose per-swap angle, read from the N-oscillation period, is theta +- 0.01 rad, with its control z pulse at the fitted resonance.
inputs:
  pair: roster pair name, e.g. q1_q2
  theta_rad: target per-swap angle, e.g. 0.30
  swap_time_ns: square pulse length, 40 on 5Q4C
outputs:
  operation: macro partial_swap_<t> playing partial_swap_square_<t> (t = theta x 100, three digits; 0.30 -> 030)
  z_amp_v: control-qubit z pulse amplitude (the resonance)
  coupler_amp_v: coupler pulse amplitude (sets the angle)
  theta_rad: period-based angle actually reached, stored as the <pair>.partial_swap_<t>.theta_rad monitor
  compensating_stark_amp: per-round compensation at the round length used, with that round length
experiments: [single_shot_readout, pair_swap_flux_map, qc_swap_flux_stark, qc_n_stark_amp]
backends: [qm]
validated: hardware 5Q4C q1_q2 + q2_q3, 2026-09-22 (theta = 0.30, 0.60)
---

# pair-partial-swap

## Physics in brief

- The swap angle is θ = J(Φc)·t. J is set by the coupler pulse amplitude, which rides on
  the coupler's `decouple_offset` (a pulse, never the line voltage). t is the pulse length.
  The control qubit's z pulse brings it into resonance with its partner.
- Repeated swaps add up (transfer sin²(Nθ)) only when the relative phase the pair picks up
  per round is compensated. The compensating element is a stark tone on the moving qubit.
  That phase depends on the **round length** modulo 1/Δ, where Δ is the pair's frequency
  difference. q1_q2 on 5Q4C: Δ = 301 MHz, so one 4 ns clock cycle is 0.2 turn. A
  compensation amplitude means nothing without the round length it was measured at.

## Prerequisites

1. **Fresh readout on both members.** Run `single_shot_readout` on both qubits at the start
   of the session, and again after hours. The pair experiments discriminate with the
   *stored* threshold and rotation, and a stale one zeroes a member silently (see Traps).
   Accept its suggestions when the stored rotation has moved or the stored threshold no
   longer sits between the two blobs.
2. The moving qubit carries the `stark` xy operation (`scqo-qm/quam_config/register_stark.py`)
   and a chosen `stark_detuning_hz` (5Q4C: 50 MHz). Its amplitude is scaled so that factor
   1.0 is one full turn of stark phase (5Q4C q1 and q3). Every stark window below stops
   there, as `procedures/README.md` requires.
3. Converting a compensation amplitude into a phase also needs that qubit's measured phase(a)
   curve from `qubit_stark_phase_echo` plotdata. Never use the fitted k for this: the Stark
   shift saturates.

## Steps

### Step 1: starting point from the flux map

- **Run** `pair_swap_flux_map --targets <pair>` with `swap_time_ns` equal to the operation's
  length. Use a coupler window from the off point up to θ ≈ 1 rad (5Q4C: q1_q2 0.07–0.11 V,
  q2_q3 0.06–0.09 V) and a qubit window of ±5 mV around the known resonance. An existing
  map of the pair can be reused.
- **Read** the per-column `theta_rad` and `resonance_qubit_flux_v` from
  `analysis/<pair>/pair_swap_flux_map_plotdata.nc`. `result.fit` holds only the summary.
- **Decide**:
  - **Coupler start:** where the per-column θ crosses the target, interpolated between
    successfully fitted columns.
  - **Flux-map bias:** the flux map's θ differs from the period-based θ of Step 4 by 0–10%.
    The ratio belongs to the pair, not to the angle, so the ratio an earlier operation on the
    pair measured carries over. On 5Q4C (period θ / flux-map θ): q1_q2 0.98 at θ 0.30 and
    1.00 at 0.60, q2_q3 1.10 and 1.09. Corrected this way, the 0.60 starts landed 0.013 and
    0.0015 rad from the target.
  - **z start:** the pair's last `qc_swap_flux_stark` resonance, shifted by the change of the
    map's `resonance_poly_curve` between the two coupler amplitudes. From θ 0.30 to 0.60 the
    resonance moved by +0.17 mV on q1_q2 and −0.35 mV on q2_q3.

### Step 2: register the operation

- **Run**, in `.venv-qm`:
  `scqo-qm register-partial-swap --pair <pair> --name partial_swap_<t> --z-amp <z start> --coupler-amp <coupler start>`.
  `--length` defaults to 40 ns.
- It writes three entries into the active setup's QUAM tree
  (`<data_root>/<device>/<cycle>/<setup>/backend_config/state.json`):
  - `qubit_control.z.operations["partial_swap_square_<t>"]`: `SquarePulse(amplitude=z start, length)`
  - `coupler.operations["partial_swap_square_<t>"]`: `SquarePulse(amplitude=coupler start, length)`
  - `macros["partial_swap_<t>"]`: `ISwapImplementation(flux_pulse="partial_swap_square_<t>")`
- Later steps retune with `--update --z-amp <v>` or `--update --coupler-amp <v>`, which
  change the two amplitudes only. `--list` shows what every pair carries.
- The tool replaces the live `state.json` only after a staged save shows that nothing else
  changes. It refuses an amplitude the port would clip. Run it between measurements: an edit
  made during a run shows up as setup-snapshot drift in that run's record.
- Declare a NEW operation in the device roster too (`components.toml`,
  `[composites.<pair>] operations = [..., "partial_swap_<t>"]`). Only a declared operation
  holds the angle Step 4 proposes; without it the run says so on stderr and keeps the angle
  in its fit alone.

### Step 3: the resonance, and the compensation at this round length

- **Run** `qc_swap_flux_stark --targets <pair>` with:
  - `swap_operation=partial_swap_<t>`
  - `swap_count` N = round(1.2/θ): 4 at θ = 0.30, 2 at 0.60. N·θ must stay below π/2, or
    the arch folds.
  - `swap_angle_rad=θ`
  - a flux window of ±3.5 mV around the z start (31 points)
  - stark 0–1.0 (31 points), never wider. A compensation near 1.0 is the same phase as one
    near 0 (Trap 4).
  - the chosen `operation_gap_ns` (see Traps: round length)
  - `num_averages=200`
- **Read**:
  - `resonance_flux_amp_v` ± `resonance_flux_err_v`
  - the flags `resonance_at_edge`, `resonance_unresolved` and `compensation_in_gap`
  - `compensating_stark_amp` ± `compensating_stark_err`
- **Decide**: if the resonance is at the edge or unresolved, move the window onto the fitted
  centre and rerun once. Otherwise set the operation's z amplitude to
  `resonance_flux_amp_v` (`scqo-qm register-partial-swap --update --z-amp`). Do not use
  `swap_angle_rad_refined`, the arch-fit angle, as the stop criterion: decoherence biases
  it low, by 2–9% on 5Q4C.

### Step 4: the angle from the N-oscillation

- **Run** `qc_n_stark_amp --targets <pair>` with `swap_operation=partial_swap_<t>`,
  `swap_counts` 0..20, stark 0–1.0 (21 points), the gap of Step 3 and `num_averages=200`.
  The counts should span at least two periods, i.e. 2π/θ counts: 21 at θ = 0.30. 0..20
  spans about four periods at 0.60. Lengthen them for smaller angles.
- **Read**:
  - `compensating_theta_rad`, which is π / `compensating_osc_period`
  - `compensating_stark_amp_refined`
  - `osc_criteria_agree`
  - `min_osc_period`, which must stay ≥ 2: no stark row may swap by more than π/2 per count
- **Decide**:
  - **Done** if |θ − target| ≤ 0.01. Accept the run's `theta_rad` suggestion
    (`scqo accept <run>`): the chain analysis draws its ideal curves from it. The run proposes
    it only when `osc_criteria_agree` = 1 and `min_osc_period` ≥ 2.
  - **Otherwise** change the coupler amplitude by (target − θ)/slope
    (`scqo-qm register-partial-swap --update --coupler-amp`) and repeat Steps 3–4. The resonance
    moves by about 0.1–0.2 mV when the coupler changes.
  - **Slope** on 5Q4C: q1_q2 0.043 rad/mV at θ 0.30 and 0.039 at 0.60; q2_q3 0.031 at 0.30
    and about 0.066 at 0.60 (from the flux map; that pair needed no second point). Once two
    points are measured, interpolate between them instead.
  - **Expect** one or two iterations. A single run's θ scatters by about ±0.007 rad, so
    iterating below that chases noise.

## Stop criteria

- Success: the period-based θ is within 0.01 rad of the target, the resonance is resolved
  (not at the edge, not unresolved), and `osc_criteria_agree` holds.
- Abort on any hardware or gateway error, as `procedures/README.md` defines it.

## Traps

1. **Stale readout thresholds.**
   - *Symptom:* one member's joint states are exactly 0.000 over the whole map. Only
     `resonance_unresolved` flags it.
   - *Case:* 5Q4C, 2026-09-22 18:13, `20260922-181347-904`. q1's "10"/"11" were 0.000 across
     961 pixels after the IQ phase drifted during the day. q2's too-close threshold also
     inflated "01" to about 0.4 far from resonance.
   - *Cure:* Prerequisite 1.
2. **Round length.**
   - Program overhead makes rounds longer than their nominal length. For `qc_n_stark_amp` and
     `qc_swap_flux_stark` a round is swap + gap + stark + 8 ns: 368 ns at gap 260 and 360 ns
     at gap 252, measured on the QM gateway simulator on 2026-09-22.
   - Always store the gap with a compensation value.
   - Comparing compensations across different round lengths needs the frame term; see
     `chain-trotter-compensation`.
3. **Two estimates of θ.** The flux-map θ is a starting point only (0–10% bias). The arch θ
   from Step 3 is biased low. The period θ from Step 4 is the reference.
4. **A compensation at the phase-wrap point.** When the compensating phase is about one full
   turn, the pick can land near stark 0 in one experiment and near the top of the window in
   the other, as q2_q3 did at gap 252 (1.000 and 0.018). Both are the same phase.
5. **Drift.** The resonance moved about 0.7 mV overnight. Rerun Step 3 before relying on an
   old z amplitude.
6. **A stark window wider than one turn.**
   - *Symptom:* two compensations one turn apart inside the window, and
     `osc_criteria_agree` = 0 because the contrast and period criteria each pick a different
     one. The two can also read different angles.
   - *Case:* 5Q4C q1_q2, θ 0.60, window 0–1.3, `20260922-213409-297`: branches at 0.47 and
     1.19. The upper one read θ 0.598 and would have ended the iteration. The rerun with
     0–1.0, `20260922-213557-000`, read 0.6135 at 0.45, which needed a second iteration.
   - *Cure:* windows stop at one turn (`procedures/README.md`). The upper branch also means a
     tone strong enough to drive the qubit.

## Typical values

5Q4C cooldown cd2, 2026-09-22, 40 ns square pulses, `stark_detuning_hz` 50 MHz:

| θ target | pair | control | coupler (V) | z (V) | θ period | θ arch | compensating stark, gap 260 | gap 252 |
|---|---|---|---|---|---|---|---|---|
| 0.30 | q1_q2 | q1 | 0.08692 | −0.15004 | 0.292 / 0.302 | 0.285 | 0.475–0.490 | 0.93 |
| 0.30 | q2_q3 | q3 | 0.0714 | −0.15425 | 0.306 / 0.305 | 0.285 | 0.849–0.856 | 1.00 ≡ 0 (one turn) |
| 0.60 | q1_q2 | q1 | 0.0958 | −0.14987 | 0.602 | 0.577 | 0.484–0.487 | — |
| 0.60 | q2_q3 | q3 | 0.0797 | −0.15460 | 0.5985 | 0.589 | 0.892–0.904 | — |

Iterations:

| θ target | pair | coupler (V) | θ period (rad) |
|---|---|---|---|
| 0.30 | q1_q2 | 0.0875 → 0.08692 | 0.317 → 0.292 |
| 0.30 | q2_q3 | 0.0725 → 0.0714 | 0.340 → 0.306 |
| 0.60 | q1_q2 | 0.0961 → 0.0958 | 0.6135 → 0.602 |
| 0.60 | q2_q3 | 0.0797 | 0.5985 |

Each iteration costs about 2.5 minutes of instrument time.

## Evidence

5Q4C runs (all `20260922-` unless dated otherwise):

- **Flux maps:** q1_q2 `094311-796`; q2_q3 `20260921-215258-520`, `20260921-202125-547`.
- **θ 0.30, q1_q2:** round 1 `190651-183`, `190952-035`; round 2 `191202-726`, `191430-328`.
- **θ 0.30, q2_q3:** round 1 `192421-043`, `192643-893`; round 2 `192844-092`, `193104-093`.
- **θ 0.30, gap-252 re-measurements:** `200343-879`, `200454-326`, `200547-676`, `200707-291`.
- **θ 0.60, q1_q2:** round 1 `213123-952`, `213557-000` (and `213409-297`, Trap 6); round 2
  `213742-598`, `213932-285`.
- **θ 0.60, q2_q3:** `220355-949`, `220541-535`.
- **Invalid (stale readout):** `181347-904`.

## Open issues

`BACKLOG.md`:

- **F15:** an error bar on `compensating_theta_rad`
- **F16:** recording the actual round length
- **F18:** a stark amplitude-to-phase conversion, so the one-turn bound can live in code
- **I21:** a flag for a member whose readout collapsed
