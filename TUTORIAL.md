# SCQO tutorial — measure and find your data

The student guide to the lab's measurement system. You run experiments by *physics
name* (resonator spectroscopy, Ramsey, power Rabi), get fitted device parameters back,
and can find every dataset you ever took. You never touch instrument code, and you
never edit anything in the repos.

**Prerequisites** (done once per machine — see [INSTALL.md](INSTALL.md), or ask
whoever set up the PC): a venv activated and a lab config in place (your own
`~\.scqo\config.toml`, or the server's shared one). **Which venv? One rule:** activate
**view** to look at data — the run-viewer, browsing, `scqo find`, `scqo tag`
(`D:\github\.venv-view\Scripts\Activate.ps1`, prompt `(view)`; macOS/Linux
`source ~/github/.venv-view/bin/activate` — the venvs live NEXT TO the repos, not
inside them, so use the full path or run from the repos' parent folder) — and an
instrument env only to measure:
`.venv-qblox` for `scqo run`/`scqo state` on the Qblox
cluster, `.venv-qm` on the OPX1000. Cooldowns are no longer a tag you maintain:
the manager registers each cycle (`scqo device cooldown`), and every run you take is
auto-stamped with it — findable via `scqo find --cooldown`.

Everything below works identically on the simulated backend (the practice mode) and
on real hardware: you select a **device** (the sample) — and, when its cycle
declares several named measurement **setups**, which one you measure with
(`scqo user`). Everything behind a setup (instrument, wiring, config files) is
recorded by the manager, never typed into a command.

## 1. The system in one picture

```
you (script / notebook / later: GUI or AI agent)
        │  experiment name + parameters (plain JSON)
        ▼
   scqo.Session ──── catalog() · run() · find_runs() · device_state() · history()
        │
   Experiment  = probe (instrument half)  +  estimator (analysis half, scqat)
        │
   Backend     = Simulated | Qblox (LCHQBDriver) | QM (LCHQMDriver)
        │
   DataStore   = every run saved to a folder + searchable SQLite index
```

- **You think in physics**: qubit names, spans, idle times, π-amplitudes.
- **The estimator** (scqat) fits the data and reports extracted quantities + a
  per-qubit `successful/failed` verdict.
- **The datastore** saves *every* run — raw data, parameters, fit result, device
  snapshots, and the fit figures — under one folder per run, and indexes it so you
  can ask "what T2* did q1 get this week?" without remembering any filename.

## 2. Your first measurement

Every command below is the **`scqo` command** — it works from ANY directory once the
right venv is active (the `scqo` command is the one CLI; there are no
`python scripts\...` wrapper forms).

First, know where your runs will land — `scqo user` answers before any instrument
time is spent (no arguments = pure diagnosis, it changes nothing):

```bash
scqo user                            # my selection + what a run resolves to (or the exact refusal)
scqo user --device chipA             # select YOUR sample (validated, written to ~\.scqo\user.toml)
scqo user --setup qblox_main         # needed only when the ACTIVE cycle has several setups
```

A single-setup cycle selects itself — most of the time picking the device once is
all there is. Then:

```bash
scqo run                                         # no arguments = show the menu
```

```
pair_swap_chevron                   qubit_spectroscopy_cryoscope
pair_swap_flux_map                  qubit_spectroscopy_flux_pulse
pair_zz_coupler                     qubit_spectroscopy_overlap
qc_n_stark_amp                      qubit_sqrb
qc_n_swap_amp                       qubit_stark_phase_echo
qubit_deterministic_benchmarking    qubit_t1_ade
qubit_drag_alternating              qubit_t1_bayesian
qubit_drag_equator                  qubit_thermal_population
qubit_echo                          qubit_tomography
qubit_echo_flux_pulse               qubit_xyz_delay
qubit_parity_switch_continuous      readout_frequency
qubit_parity_switch_discrete        readout_power
qubit_pi_pulse_error                resonator_spectroscopy
qubit_power_rabi                    resonator_spectroscopy_flux
qubit_ramsey                        resonator_spectroscopy_power_amp
qubit_ramsey_cryoscope              resonator_spectroscopy_power_chain
qubit_relaxation                    single_shot_readout
qubit_relaxation_flux_pulse         single_shot_readout_gef
qubit_spectroscopy
# capabilities: state_readout(14) flux(4) qubit_reset(30) flux_pulse(3) amplitude(4) drive_detuning(4) none(3)
# filter: scqo run --capability <name>    detail: scqo run <name> --help
```

(The menu is names only on purpose — a name tells you the family and the
method, and the full description plus every parameter lives one step away in
`scqo run <name> --help`. The footer counts **capabilities**, derived from each
experiment's parameters: `scqo run --capability flux` narrows the menu to the
experiments that sweep a flux window, repeating the flag ANDs the filters, and
`--capability none` shows the experiments with no capability mixin yet — a
legitimate state for a new experiment. Capabilities are not "tags": a tag is
the searchable label YOU attach to a saved run, as in the next command.)

Start with **resonator spectroscopy** — always the first measurement on a device: you
have to find the readout resonance before any qubit experiment means anything. Tag it
so you can find it later:

```bash
scqo run resonator_spectroscopy --targets q1 --tag mytest --note "first try"
```

You get the structured result as JSON — extracted physics, not raw traces:

```json
{
  "outcomes": { "q1": "successful" },
  "fit": { "q1": { "readout_freq_hz": 5907471431.6,     // dip position (suggested update)
                    "dip_detuning_hz": -1795822.3,       // how far the dip sat from the old value
                    "old_readout_freq_hz": 5909267253.9,
                    "f_dress0_hz": 5907471431.6,              // the dip IS the dressed resonator freq
                    "kappa_tot_hz": 1327410.5 } },       // fitted FWHM = total resonator linewidth
  "error": null,
  "run_id": "20260704-225450-SQ_demo-resonator_spectroscopy-01",
  "data_path": "D:\\qpu_data\\SQ_demo\\2026-07-04\\...-01",
  "suggestions": [ { "entity": "q1_ro", "field": "readout_freq_hz", "role": "knob",
                     "before": 5909267253.9, "after": 5907471431.6, "status": "pending" },
                   { "entity": "q1_res", "field": "f_dress0_hz", "role": "fact", "..." : "..." },
                   { "entity": "q1_res", "field": "kappa_tot_hz", "role": "fact", "..." : "..." } ]
}
```

Notice the two kinds of proposal: the *setting* lands on the readout CHANNEL
(`q1_ro`, role `knob`), the *measurement* on the resonator MODE (`q1_res`, role
`fact`) — section 9 explains the entities, section 10 the roles.

**Nothing is applied automatically.** The fitted `readout_freq_hz` is a *suggested
update*: after the JSON, `scqo run` shows the suggestion table and asks you —

```
suggested updates (3 pending):
    # entity     field              role              current         suggested   status
    1 q1_ro      readout_freq_hz    knob          5.90927e+09 ->    5.90747e+09 Hz   pending
    2 q1_res     f_dress0_hz             fact         (unmeasured) ->    5.90747e+09 Hz   pending
    3 q1_res     kappa_tot_hz       fact         (unmeasured) ->    1.32741e+06 Hz   pending
apply which updates? [a]ll / [n]one (default) / rows, component, field or component.field:
```

Press Enter to apply **nothing** (the default) — the device state is then unchanged
and the next experiment still runs on the OLD calibration; `a` applies everything,
or pick a subset (`1 3`, `q1_res`, `readout_freq_hz`, `q1_res.kappa_tot_hz`) —
partial acceptance is normal. Every applied value lands in the change history linked to
this run. In a script or a pipe there is no prompt: the run is saved with its
suggestions **pending**, and you decide later — by run id, even days later:

```bash
scqo find --pending                          # runs with undecided suggestions
scqo accept                                  # the same list, decision-oriented
scqo accept <run_id> --list                  # look at the table again
scqo accept <run_id>                         # terminal: interactive picker
scqo accept <run_id> --field readout_freq_hz --comment "matches the punchout map"
scqo accept <run_id> --reject --comment "fit chased a noise spike"
```

Applying goes through the live instrument config, so `scqo accept <run_id>` needs
the device's venv; `--list`, `--reject` and `find --pending` are datastore-only and
run anywhere the data drive is mounted. Two guards protect a deferred apply: a run
from an **older cooldown/setup era**, and a value whose *before* no longer matches
the device (someone recalibrated in between — **stale**). **At a terminal you never
need to know a flag**: a guard trip becomes a warning plus a [y/N] question showing
the exact values involved, and Enter always answers No — nothing changes unless you
explicitly confirm. In scripts nobody can answer, so `--force` pre-answers yes to
the era and stale questions.

**Changed your mind later?** A decided suggestion isn't dead. At a terminal, just
`scqo accept <old_run_id>` and pick the row — the picker asks
*"re-apply (rollback, overwriting the current …)?"* (or, for a rejected item,
*"accept it after all?"*). In a script, pass the answer as a flag:

```bash
scqo accept <old_run_id> --reapply --field readout_freq_hz --comment "rolling back - the newer fit chased a spike"
```

A rollback deliberately overwrites the current value, so re-applied rows get no
stale question (the summary shows exactly what was overwritten); the cooldown-era
guard still applies. Every re-application is a fresh change-history entry linked to
the run it came from, so the viewer's Device page tells the whole story: A applied →
B applied → A re-applied.

**The estimator failed but the figure shows the value?** It happens — the dip is
plainly visible, the fit chased a noise spike past it. Don't write the number into
the device by hand (that loses the link to the data); attach it to the run instead:

```bash
scqo suggest <run_id> q1.readout_freq_hz=5.912e9 --comment "read off the dip, fit missed it"
scqo suggest <run_id> q1_res.f_dress0_hz=5.912e9 q1_res.kappa_tot_hz=1.1e6   # several at once; either store
```

(Assignments are `entity.field`; the qubit name works as sugar —
`q1.readout_freq_hz` routes to `q1_ro`, `q1.f_dress0_hz` to `q1_res` — see section 9.)

Your value lands on that run as a pending suggestion marked `[operator: <you>]`
(the viewer shows the same badge), and from there everything above applies
unchanged — the interactive picker follows immediately at a terminal, `scqo
accept <run_id>` works later, era + stale guards included. The applied value is
credited to the run whose figure justified it, so trends and `--sources` stay
truthful. Hand-editing the state files instead would skip the instrument push and
show up as `(externally changed)` — the honest label for an untraceable write.

Once the readout is in place, the qubit experiments follow the same pattern:

```bash
scqo run qubit_ramsey --targets q1 --set num_points=201            # drive_freq_hz + T2*
scqo run qubit_power_rabi --accept                                # apply updates immediately
scqo run resonator_spectroscopy --no-update ...                   # analyze only, nothing suggested
scqo run qubit_ramsey --params my.json                            # parameters from a file
```

**See the sequence before you run it** — `--preview` builds and compiles the
experiment exactly as a run would, then stops after `probe()` and renders the
vendor's own view of it to `./scqo_preview/<experiment>_<timestamp>/`
(override with `--out DIR`), auto-opening each file — suppress with
`--no-open`. On Qblox that is the interactive pulse diagram
(`pulse_diagram.html`, zoomable in the browser) plus the absolute
`timing_table.html`; on QM it is the generated QUA script (`qua_script.py`)
plus — when the OPX1000 gateway answers — `simulated_waveforms.html`, the
gateway simulator's actual analog outputs (tried automatically, skipped with
a warning when the cluster is unreachable; `--simulate-ns N` widens the
simulated window from its 20 µs default, `--no-simulate` guarantees a fully
offline preview; a thermal-reset shot starts with a millisecond wait, so its
default window can be legitimately empty — the warning says so). No qubit
ever plays: simulation runs on the gateway server. Nothing touches hardware,
nothing is saved, no updates are suggested — and the simulated backend refuses
by name (it never builds a vendor sequence). One cost note: the Qblox pulse
diagram draws every sweep point of EVERY repetition (points x averages), so a
lab-sized schedule is refused with the exact `--set` that shrinks it — e.g.
`--set num_averages=2 --set num_points=5`. The per-shot sequence is identical
at small counts, and the real run is unaffected.

```bash
scqo run qubit_ramsey --targets q1 --preview                       # look, don't touch
scqo run qubit_ramsey --preview --out D:\tmp\ramsey_check          # pinned folder, overwrites
```

One more distinction worth knowing: **instrument settings vs sample physics** —
the suggestion table's `role` column says which side each value belongs to. Both
land in YOUR context's `<device>/<cooldown>/<setup>/scqo/` folder, so two users on
two setups of one sample never see (or overwrite) each other's numbers.
Calibration knobs (`readout_freq_hz`, `pi_amp`, ... — they live on the CHANNEL
entities `q1_ro`/`q1_xy`/`q1_z`; `role: knob`) are pushed to the instrument on
accept and recorded in `scqo_state.json`. Measured physics — facts (`role: fact`):
T1, T2*, T2echo on the qubit mode, the flux maps' `flux_offset`/`flux_per_phi0`
on the z channel, `ej_sum_hz`/`f_bare_hz`/`g_hz` — lands in `physical.json` beside
it (same accept flow). A third role, `monitor` (`fidelity_g`/`fidelity_e`, the
blob positions), records measured performance OF the current knobs: stored in
`scqo_state.json` but never pushed anywhere. The context's full change history
lives in `history.sqlite` beside the two values files (one database per
(cooldown, setup), both stores) — never edit any of them by hand: a hand-edit
skips the instrument push and shows as `(externally changed)`; use
`scqo suggest` instead. An estimate is only as clean as the chain it came through (a noisy drive
line shortens the measured T2; the flux transfer function depends on the wiring), so each context's
physics stands on its own — compare across contexts via `scqo find` / the trends
page, never average. The setup-independent "true" sample physics is a future
*inference* over these measurements (`sample.json`, Phase 3).

```bash
scqo state --physical               # this context's measured physics (one row per entity/field)
scqo state --physical --history     # who accepted what, when, from which run
scqo state --sources                # which run set each CURRENT value (both stores)
```

`--sources` answers *"which runs is my device built from?"* — the values in use
matter more than the pending ones. Every current value names the run that set it,
**strictly**: a value the vendor reseeded or another tool wrote shows
`(externally changed)` and credits no run; direct manual writes — `scqo set
q1.readout_freq_hz=5.912e9` or a notebook assignment — show `(manual)` with the
operator's login. `scqo set` is for values you know from EXPERIENCE (no run to
credit): it previews current -> new with units, asks once, then writes through
the normal recorded path immediately (`--yes` in scripts).

Three tiers of parameters — each overriding the previous:

1. **Code defaults** — every knob ships a sensible built-in default; see them all
   with `... <experiment> --help`.
2. **Your standing defaults** (optional) — put semi-permanent project settings in
   `~\.scqo\parameters.toml`, one table per experiment (format and rules in
   [INSTALL.md](INSTALL.md) §2). Edit it once per project or cooldown and every run
   picks the values up; `--help` marks them like
   `default=15e6 [parameters.toml]`. With this file in place, most runs need no
   parameter flags at all.
3. **The command line** — always wins. **`--set KEY=VALUE`** changes *one* knob
   (repeat it for several), while **`--params`** loads a *whole set* as JSON — a file
   path or an inline object like `--params "{""num_points"": 201}"`. Don't mix the
   two syntaxes.

See every knob an experiment has — with your standing defaults marked — via
`scqo run <experiment> --help`.

```bash
scqo run resonator_spectroscopy --targets q1 --set frequency_span_hz=15e6
scqo run resonator_spectroscopy --help
```

The **standard bring-up** is the same command three times — resonator spectroscopy
→ qubit spectroscopy → power Rabi (Ramsey is the fine-tuning follow-up once a pi pulse
exists); accept each run's suggestions so the next step measures with them:

```bash
scqo run resonator_spectroscopy --targets q0 q1 --tag cooldown1
scqo run qubit_spectroscopy     --targets q0 q1 --tag cooldown1
scqo run qubit_power_rabi       --targets q0 q1 --tag cooldown1
```

(There is deliberately no sequence command at this phase; a sequence runner
belongs to the AI loop and arrives with it.)

And the device's calibration state / change log any time (the first output line
names the device, YOUR resolved setup and its state file — state is per setup,
so that line says whose numbers follow):

```bash
scqo state                      # current values, one table per entity kind (your setup)
scqo state --history 20         # who changed what, when, in which run
```

### Readout power — two modes

Behind the readout drive are TWO knobs, and two punchout experiments named for
the knob each one sweeps. They take **identical parameters** (an absolute-dBm
window: `min_power_dbm`/`max_power_dbm`, default −50…−20), report the same
absolute axis, and propose the same fields (`readout_power_dbm` +
`readout_freq_hz`) — they differ only in mechanism, and each figure prints its
mode in the title so you can never confuse the two.

The knobs:

- **`readout_power_dbm`** — the ABSOLUTE readout power (dBm at the instrument
  port). Setting it re-solves the output chain (QM `full_scale_power_dbm` in 3 dB
  steps / Qblox `output_att` in 2 dB steps) so the digital amplitude lands at
  **≤ 0.5 of full scale** — the canonical operating point. `readout_amp` moves as
  a *coupled* side effect (the history marks such echoes with the causing field).
- **`readout_amp`** — the digital amplitude, relative to whatever the chain is
  set to. Fast and fine-grained, but the digital "1.0" means a different dBm on QM
  vs Qblox, which is why the punchouts work in absolute power instead.

The two experiments:

- **`resonator_spectroscopy_power_amp`** (fast) solves the chain for the WINDOW
  TOP once (`readout_power_dbm = max_power_dbm` — a recorded write, auto-reverted
  after the run), then sweeps the digital amplitude down from it in ONE hardware
  program. Every qubit hits the same absolute window exactly, whatever its
  standing power. Minutes fast; the trade-off is SNR — best at the top of the
  window, degrading toward the bottom where the DAC amplitude gets tiny.
- **`resonator_spectroscopy_power_chain`** (careful) steps the chain per power
  point: a Python loop (the chain knobs cannot change inside the FPGA loop)
  re-solves the chain so the digital amplitude stays at ~0.5 full scale for good
  SNR at EVERY point, and runs one 1D detuning scan per point — ascending,
  constant power within each scan, so resonator ring-down from a power jump can
  never contaminate. Wide and cross-backend comparable, but each point is a
  separate compile+run cycle (the default 21 points adds a few minutes).

Both record the boundary set/revert pair honestly (2 change records + coupled
echoes per qubit) and both leave the device exactly as found — accepting the
suggestion is what actually re-centers the chain. Both refuse to run on a qubit
whose `readout_power_dbm` is unknown (an unconfigured chain or zero amplitude:
the revert target would be undefined) — set it once, or fix `readout_amp`, first.

The workflow: run **`_amp`** for the quick look; reach for **`_chain`** when the
low-power end matters (the dispersive dip near the knee is faint) or for a
calibrated cross-backend sweep, then fine-tune with `_amp` again.

Absolute-scale honesty: on QM the dBm axis is exact at the port; on Qblox it is
derived from the nominal +5 dBm module full scale, good to ±a few dB (a per-setup
photon-number anchor is a Phase-3 refinement). BOTH experiments sweep a uniformly
spaced dBm axis on BOTH backends (`_chain` by re-solving the chain per point;
`_amp` with exact geometric amplitudes — on Qblox the amplitude axis is unrolled
point-by-point, since the hardware only loops linearly). Both figures share ONE format: the map
plus a SUBPLOT underneath (shared power axis) showing the per-point **digital
amplitude** and the used `output_att` / `full_scale_power_dbm` — for `_amp` the
chain curve is flat and the amplitude sweeps; for `_chain` the chain steps and
the amplitude sawtooths around 0.5 — so every map records what the instrument
was actually doing. Every run also records the raw chain values (`power_context`
in record.json), so past axes stay interpretable even after the chain changes.

### Repeating a measurement — campaigns

One run gives one T1. A **campaign** gives you a hundred, each with its own timestamp,
so you can ask whether the qubit actually drifted:

```bash
scqo run qubit_relaxation --targets q1 --repeat 100 --period 300
```

That is 100 separate saved runs, one every 5 minutes, plus a table:

```
experiment             target   quantity                n  miss        mean         std         sem         min         max  std/err
qubit_relaxation       q1       t1_s                   98     2  4.1310e-05  3.1000e-06  3.1300e-07  3.4200e-05  4.9300e-05     3.88
```

The last column is the one to read. **`std/err` is the across-repeat scatter divided
by the mean per-fit standard error**: much greater than 1 means the qubit really moved
between repeats; around 1 means the spread is just fit noise and the qubit was stable.

(For T1 specifically there is also an IN-RUN alternative on the QM backend:
`qubit_t1_ade` and `qubit_t1_bayesian` produce a whole T1-vs-lab-time trace inside
one run — hundreds of estimates per minute with per-point error bars, versus one
estimate per repeat here. The campaign remains the cross-experiment, cross-backend
tool; the trackers are the high-cadence single-quantity ones.)
A `-` means that fit publishes no standard error for that quantity (`t2_star_s` is
the common case), not that the scatter is zero.

To repeat a **bundle** of different experiments, write a plan file. The bundle is
walked in order, and the whole bundle is what repeats — so all four numbers in one
repeat come from the same few minutes and are comparable with each other:

```toml
# t1_stability.toml
label = "t1_stability"
repeat = 100
period_s = 300          # minimum seconds between repeat STARTS (a floor, never padding)
max_duration_s = 43200  # give up after 12 h whatever happens
skip_artifacts = true   # 400 runs x per-qubit figures is a lot of disk
tags = ["stability", "overnight"]

[writeback]             # optional: how the aggregate becomes proposed updates
stat = "mean"           # "mean" (default) or "median" (outlier-robust)
min_n = 3               # successful repeats a quantity needs before proposing

[defaults]
targets = ["q1"]

[[steps]]
experiment = "qubit_relaxation"
[[steps]]
experiment = "qubit_ramsey"
[[steps]]
experiment = "qubit_echo"
[[steps]]
experiment = "single_shot_readout"
params = { num_shots = 4000 }
```

```bash
scqo campaign t1_stability.toml --dry-run   # validate + show what would run
scqo campaign t1_stability.toml             # ...then actually run it
scqo campaign --list                        # what has run
scqo campaign --show <campaign_id>          # plan + statistics + every child run
scqo find --campaign <campaign_id>          # the children as ordinary runs
```

**Always `--dry-run` first for a long one.** It builds every step's parameters and
checks every target against the roster without touching the instrument, so a typo
shows up now instead of at 3 a.m. on repeat 100.

While it runs you get a live log, so an overnight campaign is never a silent box:

```
# repeat    1/100  started 20:15:03
#   qubit_relaxation      q1       ok     t1_s=4.13100e-05                          2.4s
#   qubit_ramsey          q1       ok     f_01_hz=3.8e+09  t2_star_s=7.99580e-06     2.9s
#   qubit_echo            q1       ok     t2_echo_s=3.05340e-05                      2.1s
#   single_shot_readout   q1       ok     p_e_given_g=0.0425                         3.1s
# waiting 4m47s for the next repeat (period_s=300)
# repeat    2/100  started 20:20:03   eta 04:22 (+8h02m)
#   qubit_relaxation      q1       ok     t1_s=4.09800e-05                           2.4s
#   qubit_ramsey          q1       FAILED ValueError: fit did not converge           2.8s
```

Reading it: the ETA appears from the second repeat (the first has no history to
extrapolate from), a step that fails says so and keeps going, and the values shown
are the *measured* ones — settings you configured, like `drive_freq_hz`, are left
out because they cannot drift. All of it goes to **stderr**, so stdout stays clean
for `--json | jq`; silence it with `2>$null` or capture it with `2>run.log`.

#### Which readout number to trust

`single_shot_readout` reports the leftover excited population **two ways**, and they
are not the same quantity:

| | how | what it contains |
|---|---|---|
| `p_e_given_g` | **counted** — every shot assigned to its nearest blob centre | residual population **+ discrimination error** |
| `pop_e_prep_g` | **fitted** — the weight of the excited blob in the fit | residual population **alone** |

So the *gap between them* is roughly your discrimination error, and that is what
makes the pair worth having. A real 20-repeat chipA campaign:

- The count scattered by **125%** and spiked to 11.5% on one repeat. The fit sat at
  0.98% ± 0.22 throughout. That spike was the **readout failing to separate the
  blobs**, not the qubit suddenly becoming 11% excited.
- Between two campaigns 1.7 h apart the count mean was flat (1.86% → 1.88%) while
  the fit rose **48%** (0.98% → 1.45%). The count would have told you nothing had
  changed.

`pop_g_prep_e` is the mirror image — the ground-state leftover when you *prepare*
excited. It runs ~8.5% against ~1% for the g side, which is expected: that is T1
decay during the readout window, so it is a direct handle on how long your readout is.

One caveat, because it decides how much to trust the number: the fit floats only the
blob **amplitudes**. The centres and widths are pinned to a median/MAD seed, so a bad
seed gives a bad population. It is not a free two-Gaussian fit.

Campaigns you ran before this existed have no `pop_*` values stored — but the raw
per-shot data does, so they can be recovered offline with no instrument:

```bash
scqo campaign --show <campaign_id> --plot --recompute-readout
```

Things worth knowing before you leave one running overnight:

- **The device is not touched while it runs.** A campaign runs with updates off,
  because 100 repeats would each propose the same change and accepting one makes the
  other 99 stale. The writeback happens ONCE instead, at finish: the statistics are
  replayed through each experiment's own `update()` and stored on the campaign as
  **pending suggested updates** (one `t1_s` series proposes both the fact and the
  derived thermalization wait, exactly like a single run would). The plan's
  `[writeback]` table picks the statistic. Review and decide them like a run's:

  ```bash
  scqo accept --campaign <campaign_id>
  ```

  Same interactive review, same era/staleness guards — and the applied values carry
  the CAMPAIGN as their provenance: `scqo state --sources` and the viewer link them
  to the campaign page (with its full distribution and every child run), not to any
  single run. For campaigns run before this feature existed,
  `scqo campaign --suggest <campaign_id>` regenerates the proposals from the stored
  statistics. `scqo set` remains the manual escape hatch. (`--accept` still exists,
  for a deliberate repeated *re-tuning* — but then the device moves under its own
  measurement, the spread describes the tuning loop, and no aggregate is proposed.)
- **Ctrl-C is safe, at any moment.** Every repeat already done is saved, and the
  repeat you interrupted keeps the steps that had already finished — they show up in
  the statistics and are counted as `repeats_partial`, separately from the whole
  repeats in `repeat_done`. Only the single step that was actually running is lost,
  and its half-written folder is ignored (no `record.json`). This holds whether you
  interrupt during a measurement or during a `period_s` wait.
- **You can watch it from another terminal.** The manifest is rewritten after every
  repeat, so `scqo campaign --show <campaign_id>` gives live statistics while the
  campaign is still running.
- **It stops itself** when the wall-clock budget runs out, or after 5 consecutive
  repeats in which nothing succeeded (a disconnected instrument won't spend the night
  filling your disk with failures). One flaky fit among four steps is not a failed
  repeat — it shows up as `miss` in the table.
- **A failed fit is counted, not hidden.** `n` and `miss` always add up to the repeats
  attempted, so you can see how often the measurement worked.
- Each child is an ordinary run in the ordinary day folder, stamped with its campaign
  and its repeat/step number — `scqo find --show <run_id>`, figures and all.

## 3. Finding your data (the whole point)

```bash
scqo find                                   # latest runs, newest first
scqo find --cooldown cd8                    # everything from this cooldown cycle
scqo find --cooldown cd8 --setup qblox_main # ...narrowed to one of its measurement setups
scqo find --experiment resonator_spectroscopy --target q1 --since 2026-07-01
scqo find --outcome failed                  # what went wrong lately?
scqo find --show 20260704-225450-SQ_demo-resonator_spectroscopy-01   # one run, in full
```

```
20260704-225450-SQ_demo-resonator_spectroscopy-01    successful q1           tim        cooldown1,mytest     pend:3   SQ_demo/2026-07-04/20260704-225450-SQ_demo-resonator_spectroscopy-01
```

(One row per run: id, outcome, targets, operator login, tags, a `pend:N` marker
when suggested updates are still undecided, and the folder path.)

- Dates in filters are **local lab time** and match the folder names; a bare date in
  `--until` includes that whole day.
- `find_runs` touches no instrument — it runs anywhere the data drive is mounted.
- Several samples share one data_root: every run is stamped with its device (= sample)
  name, so `--device chipA` (or the viewer's device dropdown) narrows to one chip.
- `--setup` filters by the named setup stamped on each run; names are unique per
  cycle only, so combine it with `--cooldown`.
- Realized a week later that a run mattered? Tag it retroactively:
  `scqo tag 20260704-...-01 --add thesis-fig3 --note "best T2* so far"`
  (also backend-free).
- `--pending` narrows to runs whose suggested updates are still undecided —
  `scqo accept` shows the same list and is where you decide (section 2).
- `--campaign <campaign_id>` narrows to one campaign's children; `scqo campaign --show`
  lists the same runs in execution order instead of newest-first.

## 4. What's inside a run folder

```
<data_root>/SQ_demo/2026-07-04/20260704-225450-SQ_demo-resonator_spectroscopy-01/
    record.json          run manifest (its absence = run was incomplete/crashed)
    dataset.nc           the raw I/Q dataset (xarray/netCDF, dims: target × detuning_hz)
    parameters.json      exactly what you asked for
    result.json          outcomes + fitted quantities + error (if any)
    device_before.json   calibration state before ...
    device_after.json    ... and after the run (differs only where updates were applied)
    analysis/q1/         per-qubit fit artifacts from scqat:
        resonator_spectroscopy.png                         ← the dip + fit, already drawn
        resonator_spectroscopy_metadata.json               fit parameters, fit quality
        resonator_spectroscopy_plotdata.nc                 arrays to redraw without refitting
```

(A Ramsey run looks the same with its own artifacts: `ramsey_time_domain.png`,
`ramsey_fft_spectrum.png`, etc.)

A campaign adds one folder of its own — a sibling of the day folders, because an
overnight campaign crosses midnight:

```
<data_root>/SQ_demo/campaigns/20260727-221503-472-SQ_demo-t1_stability-01/
    campaign.json        the plan, the status, the statistics table, and the
                         aggregate suggested updates with their decisions
    repeats.jsonl        one line per completed repeat: which runs, when, outcome
    statistics.png       histogram + drift trace per quantity, drawn when it finishes
```

`statistics.png` is written automatically — including for a campaign you stopped
early, so an overnight run always has its picture waiting. Each row is one measured
quantity: the histogram on the left, the value vs hours-since-start on the right
with median/MAD outliers ringed, and a title carrying `n`, `mean`, `std`, the drift
`slope` per hour and `std/err`. Re-draw an old one any time:

```bash
scqo campaign --show <campaign_id> --plot
```

`repeats.jsonl` deliberately stores no fitted numbers — the child runs' `result.json`
stays the one place every number lives.

**The folder is the truth.** The SQLite index (`<data_root>/index.sqlite`) is only a
cache — if it is ever missing or stale, rebuild it losslessly:

```powershell
python -m scqo <data_root>
```

### The run viewer — your daily data GUI

One command opens the lab's data as a website (one-time: viewer extras via
`uv pip install fastapi uvicorn jinja2`, already installed on the lab PC):

```powershell
python -m scqo.viewer            # -> http://127.0.0.1:8080
```

Six pages (port convention: **8001 qualibrate · 8080 viewer · 8081 datasette** —
all can run at once):

- **Runs** — filter by experiment / qubit / tag / outcome / date / campaign, plus a
  **pending only** checkbox for runs with undecided suggested updates; click any run.
  Runs whose accepted values are still **LIVE on the device** carry a green
  `live:` line naming those fields — the at-a-glance answer to *"which runs is my
  device built from?"*. A campaign child links to its campaign right in the row,
  and the setup column links each run's own setup page.
- **Campaigns** — every campaign with its status, repeat progress and a pending
  badge for undecided aggregate suggestions; the detail page shows the statistics
  table, `statistics.png`, the suggestion groups with their decisions (deciding
  stays on the CLI: `scqo accept --campaign <id>`) and the children in execution
  order. A still-running campaign renders live — the manifest rewrites after
  every repeat.
- **Run page** — outcome badges, the fit table, **every figure inline** (the dip,
  the fringe, the 2D power map...), your parameters, the **suggested updates** table
  (pending / accepted / rejected, who decided, comments — deciding stays on the CLI:
  `scqo accept <run_id>`) with an **on device** column (LIVE, or superseded —
  linking the run that superseded it), and the device before → after diff. You can
  **add/remove tags and edit the note right here** — the viewer's only write,
  equivalent to `scqo tag`.
- **Samples** — opens with an overview of EVERY sample in the data_root
  (description, the active cycle's setups as links, latest run — including
  freshly added samples with no runs yet); click one for its detail page: the
  `devices.toml` card, **every cooldown cycle with every setup linked** (closed
  cycles stay browsable), and the **physical-properties matrix** — one row per
  measured fact, one column per (cooldown, setup), each cell the latest value
  there; a row opens that fact's cross-cooldown trend. The viewer is
  account-independent: it serves the whole lab regardless of who launched it or
  what their `user.toml` selects.
- **Setup page** (`/setup/<device>/<cooldown>/<setup>`) — one page per setup of
  every cycle: the cycle facts + setup metadata, the current **calibration** and
  **physical parameters** tables where each row shows the value, its unit, the
  run the CURRENT value came from, and the run the PREVIOUS value came from
  (`(manual)` and `(externally changed)` marked honestly). Every parameter NAME
  links its change trend, scoped to this setup.
- **Trends** — charts the **change history** (the accepted lineage — what the
  device actually used), never per-run fits. Two ports: from a setup page, a
  parameter name shows its **latest 50 changes in that setup**; from the device
  page's matrix, a physical fact charts **across every cooldown and setup**
  (points colored per context, dashed lines at cooldown boundaries, every point
  linking its run). A trend is always scoped to ONE sample — qubit names repeat
  across chips, so there is no cross-sample union and no silent default.

Power users: `python -m scqo.browse` still serves raw datasette on **8081** for
ad-hoc SQL, facets and CSV export (same canned queries as before).

## 5. Working from your own laptop (nothing to install)

Once the lab server is running, your own laptop needs **no Python, no venv, no
config file** — just two addresses:

**To see data — the browser.** Open `http://<server>:8080` (ask the manager for the
server's name/IP). Everything in the viewer section above works from any machine on
the lab network, including tag/note editing.

**To measure — SSH.** Every OS ships an ssh client (Windows PowerShell, macOS
Terminal, Linux). Ask the manager for an account on the server, then a session looks
like this:

Your account carries your own settings — no shared file to fight over:

- `~/.scqo/user.toml` — YOUR sample and setup selection plus your project tags.
  No editor needed for the selection: `scqo user --device chipA` writes it
  (validated), and `scqo user --setup <name>` picks a setup when the device's
  ACTIVE cycle has several — the instrument follows the selection via the device's
  cooldown registry. Only personal keys are allowed.
- `~/.scqo/parameters.toml` — your standing experiment parameters (three-tier rule
  in section 2). Applies automatically — no user.toml line needed; the optional
  `parameters_file` key in user.toml exists only to swap in a DIFFERENT file.

**Editing these files from an SSH terminal** (the device/setup selection needs no
editor — `scqo user --device <name>` writes it; hand-editing covers the rest, e.g.
`default_tags`) — GUI editors do NOT work over SSH: `notepad user.toml` starts an
invisible process on the server and no window ever appears (clean strays with
`Get-Process notepad | Stop-Process`). Use one of:

1. **PowerShell here-string** (no tools; writes UTF-8 without BOM — never use
   `Set-Content -Encoding UTF8`, its BOM breaks the TOML parser):

   ```powershell
   type ~\.scqo\user.toml                     # read
   [IO.File]::WriteAllText("$env:USERPROFILE\.scqo\user.toml", @'
   device = "chipA"
   default_tags = ["projA"]
   '@)
   ```

   PowerShell shows `>>` until the closing `'@` — type it at the start of the line.
   To ADD an experiment table to parameters.toml, use `AppendAllText` the same way.
2. **scp round-trip** from your laptop (OpenSSH lands in your profile, so relative
   paths work): `scp <you>@<server>:.scqo/parameters.toml .` → edit locally →
   `scp parameters.toml <you>@<server>:.scqo/`.
3. **VS Code Remote-SSH** if you edit these often — a real editor saving directly
   on the server, correct encoding by default.
- Don't know what's available? `scqo device list` prints every known sample, one
  row per setup of its active cooldown cycle (backend, config folder — wiring lives
  inside the vendor config, not here), with `<- selected` marking yours and the
  `scqo user` command to change it. It touches no instrument, so it is always safe.

```
ssh <your-account>@<server>            # password prompt on first login
D:\github\.venv-qblox\Scripts\Activate.ps1     # (or .venv-qm for the OPX1000)
scqo run resonator_spectroscopy --targets q1 --tag mytest    # any directory works
scqo find --limit 5
exit
```

The run executes on the server (which owns the instruments and the data), your
laptop is only the keyboard — closing the lid mid-run kills the run, so let a
measurement finish before disconnecting. Figures appear in the viewer seconds later.

Rules that keep shared instruments sane:

- Every run records **you** as its operator (your login name) — visible in the
  viewer and `scqo find --operator <name>`. Your work is attributable; so are
  your mistakes. Both are fine — failed runs are searchable on purpose.
- **One measurement at a time per instrument.** Check the viewer's latest runs (or
  ask in the lab chat) before starting a long sweep; a second program on the same
  instrument will fail or corrupt both.
- SSH is for *measuring*. For looking at data, use the browser — it can't break
  anything.

## 6. Working in Python / Jupyter

**Where do my notebooks/scripts live?** Anywhere OUTSIDE the governed repos — e.g. a
personal `lab-notebooks/` folder (make it your own git repo if you want history).
Because `scqo`/`scqat` are installed in every env, imports work from any directory;
just select the right venv as your interpreter/kernel (VS Code: pick
`.venv-view\Scripts\python.exe` for analysis notebooks, `.venv-qblox\...` if the
notebook drives the instrument; or `uv pip install --python <venv-python> jupyterlab
ipykernel`). If a notebook grows into a new *experiment* or *estimator*, it graduates
to the contrib sandbox (section 8) — never straight into SCQO or a driver repo.

**Analyzing saved data needs no backend at all** — this is what most notebooks are:

```python
from scqo import DataStore, load_lab_config

cfg = load_lab_config()
store = DataStore(cfg.data_root, device_name=cfg.device)

store.find_runs(experiment="resonator_spectroscopy", target="q1", tag="cooldown1")
run = store.load_run("20260704-225450-SQ_demo-resonator_spectroscopy-01")  # record + params + figures
ds = store.open_dataset("20260704-225450-SQ_demo-resonator_spectroscopy-01")
ds["I"].sel(target="q1").plot()
store.tag_run("20260704-225450-SQ_demo-resonator_spectroscopy-01", add=["thesis-fig3"])
```

**Running measurements** from a notebook is the same Session the commands use —
let `build_session` do the wiring (it resolves your device → ACTIVE cycle → YOUR
setup, exactly like `scqo run`, and binds the per-setup state files). A one-off
known value needs no notebook: `scqo set q1.readout_freq_hz=5.912e9` is the same
recorded write (`sess.set_values(...)` from code), with a confirmation prompt.

```python
from scqo.cli import build_session

sess, cfg = build_session()          # your config.toml + user.toml decide everything
```

(Hand-building instead — a custom backend object — still works, but a persisted
session needs its context: `make_session(backend, cfg, roster, backend_label=...,
setup_name=..., cooldown_id=...)`, or pass `scqo_dir` to `Session` directly for a
free-form scratch folder.)

```python
result = sess.run("resonator_spectroscopy", {"targets": ["q1"]})
result["suggestions"]                                    # the proposed updates (pending)
sess.accept(result["run_id"], fields=["readout_freq_hz"], comment="looks right")
sess.reject(result["run_id"], comment="noise spike")     # decline the rest (no instrument)
sess.run("qubit_ramsey", {...}, update="apply")          # unattended / AI loop: apply now
sess.find_runs(experiment="resonator_spectroscopy", target="q1")  # list of dicts, newest first
sess.find_runs(pending=True)                             # undecided suggestions
sess.load_run(result["run_id"])                          # record + params + figure paths

sess.device_state()             # the operating state per entity (this context)
sess.qubit_state("q1")          # one qubit's ASSEMBLED view: mode + its channels + resonator
sess.physical_state()           # this context's measured physics
sess.history()                  # every calibration change: who, what, old → new, which run
sess.history(store="physical")  # same, for the physical-parameter ledger
```

## 7. When things fail (by design)

A failed fit or a bad probe **never crashes and never loses data**: you get
`"error": "..."`, the qubits are marked `failed`/`no_data`, nothing is suggested or
applied — and the run (including the misbehaving dataset) is still saved and
searchable via `--outcome failed`, because failed data is exactly what you want to
look at when debugging. Even "measurement fine, but applying an accepted value
failed" comes back structured: the fit stays intact and the item stays *pending*
with the error noted on it, so you can decide again once the cause is fixed.

## 8. Rules of the road (who edits what)

1. **Students**: run the commands; your only writes are your own `config.toml`,
   `parameters.toml` and `user.toml` — and the device/setup selection goes through
   `scqo user` (it writes your user.toml, validated). The repos and the shared
   registries are read-only for you.
2. **Advanced users**: prototype new experiments + estimators in the sandbox
   (`scqo-contrib`, entry-point group `scqo.experiments.contrib`) — your runs land
   in the same datastore, so your evidence is findable.
3. **The manager** owns the shared registries — cooldown cycles
   (`scqo device cooldown start`/`end`), the hand-added `[<cycle>.setup.<name>]`
   blocks in each device's `cooldowns.toml`, each device's `components.toml`
   roster + `design.toml` datasheet (section 9), and `devices.toml` — and promotes
   proven experiments into `scqo/experiments/` + the driver repos (checklist in
   [CLAUDE.md](CLAUDE.md)). A setup block is just `backend` (+ `note`) — folder
   locations are DERIVED from the keys: put each real setup's vendor files in
   `<cooldown>/<setup>/backend_config/`; SCQO keeps its own state + physics in
   the sibling `<cooldown>/<setup>/scqo/`, auto-created on first save.

## 9. The device model — modes, lines, channels

A device is declared once per sample in `<data_root>/<device>/components.toml`
(sibling of `cooldowns.toml` — a SAMPLE fact, one copy above all cooldowns and
setups). The file is the TOPOLOGY: which quantum degrees of freedom the chip
has, and which physical wires reach them. Four sections; every entry says
`kind = "<token>"`:

- `[modes.*]` — quantum degrees of freedom (kinds `transmon`, `flux_transmon`,
  `fluxonium`, `cavity`, `resonator`). Qubits AND tunable couplers are modes;
  "coupler" is a role in a composite, not a kind.
- `[composites.*]` — named mode groups with JOINT calibrated physics
  (`qubit_pair`, `cat_system`).
- `[lines.*]` — one table per physical control path reaching the sample; its
  RIDER LISTS mint the channels.
- `[channels.*]` — the explicit escape hatch for irregular paths (e.g. reading
  a coupler through a neighbor's resonator, or a pump tone).

```toml
schema = 3

[modes.q1]
kind = "flux_transmon"
[modes.q2]
kind = "flux_transmon"

[lines.fl1]
readout = ["q1", "q2"]     # ONE feedline, two riders -> channels q1_ro + q2_ro
                           # (+ minted resonator modes q1_res + q2_res): two
                           # readout_freq_hz on one wire IS frequency-
                           # multiplexed readout
[lines.xy1]
drive = ["q1"]             # -> channel q1_xy
[lines.xy2]
drive = ["q2"]
[lines.z1]
flux = ["q1"]              # -> channel q1_z (a flux rider naming a fixed
[lines.z2]                 # `transmon` would be a LOAD ERROR — capability
flux = ["q2"]              # by construction, not a pruned field)
```

You never declare `q1_res`, `q1_ro`, `q1_xy` or `q1_z` — the riders MINT them
(`readout` → `<t>_ro` plus the `<t>_res` resonator mode, `drive` → `<t>_xy`,
`flux` → `<t>_z`), and single-mode operations (`rx`, `readout`, `flux_bias`)
are DERIVED from the wiring, never declared. Every value then lives on the
entity that owns it: knobs on CHANNELS (`q1_ro.readout_freq_hz`, `q1_xy.pi_amp`,
`q1_z.idle_flux`), facts on MODES (`q1.t1_s`, `q1.f_01_hz`, `q1_res.f_dress0_hz`),
monitors on channels too (`q1_ro.fidelity_g`).

Addressing is `entity.field` — `scqo set q1_z.idle_flux=0.12`,
`scqo set q1_res.kappa_tot_hz=...` — with QUBIT sugar: `q1.pi_amp` routes to
`q1_xy`, `q1.readout_freq_hz` to `q1_ro`, `q1.f_dress0_hz` to `q1_res` (first hit
in the qubit's closure). A wrong home answers with the right one
("`q1_ro.pi_amp`: no entity in `q1_ro`'s closure carries this field — did you
mean `q1_xy.pi_amp`?"). One fit may legally write a knob AND a fact: resonator
spectroscopy proposes `q1_ro.readout_freq_hz` (the setting) and `q1_res.f_dress0_hz`
(the measurement) from the same dip.

**Design targets** live in the sibling `design.toml` (the DATASHEET), never in
the roster — entity-named tables of as-designed, context-free values:

```toml
schema = 1
[q1]
f_q_max_hz = 4.73e9                # flux-tunables design the sweet-spot freq;
anharmonicity_hz = -2.0e8          # a FIXED transmon designs f_01_hz instead
[q1_res]                           # design on a MINTED entity is fine —
f_dress0_hz = 5.93e9                    # validation runs after roster expansion
```

Bring-up sweeps anchor on these when nothing is measured yet — such runs are
tagged `seeded:<entity>.<field>`, and a sweep with neither a standing value nor
a design anchor refuses with the exact `scqo set` / `design.toml` fix.

**Qubit pairs (QCQ tunable-coupler chips).** The coupler is an ordinary
flux-tunable mode with its own flux wire; the pair is a composite naming its
members by role:

```toml
[modes.q1_q2_c]
kind = "flux_transmon"
[composites.q1_q2]
kind       = "qubit_pair"          # zz_hz, j_hz — the pair's measured facts
high       = "q1"                  # design-nominal frequency ordering — NEVER
low        = "q2"                  # control/target ("which qubit moves" is a
coupler    = "q1_q2_c"             # per-operation vendor fact, not topology)
operations = ["iswap"]             # declared gates mint the knob family:
[lines.zc12]                       # iswap_coupler_flux, iswap_duration_s, ...
flux = ["q1_q2_c"]                 # -> channel q1_q2_c_z
```

The coupler's standing (decouple) bias is `idle_flux` on ITS OWN flux channel —
`scqo set q1_q2_c_z.idle_flux=0.081` replaces hand-editing the vendor config —
and per-gate operating points are the pair's per-operation knobs
(`q1_q2.iswap_coupler_flux`, ...). The `pair_zz_coupler` experiment automates
the decouple point: it maps the signed residual ZZ vs coupler bias (echo
fringe) and proposes the zero crossing as the coupler z channel's `idle_flux`
plus the residual `zz_hz` fact on the pair.

Two record-only maps come BEFORE it at bring-up, when no two-qubit gate is
defined yet: `pair_swap_chevron` excites one member and sweeps a flux pulse
(absolute volts) against its duration, drawing the swap arch that locates the
resonance amplitude and the full-swap time; `pair_swap_flux_map` then fixes the
duration and sweeps the coupler flux against the member flux, showing the swap
spot and how the coupler moves it. Both read BOTH members out and report the
excitation transfer onto the UNDRIVEN one, plus `p_ee_max` — the |ee>
population that separates a real swap from heating or a leak. Neither writes
anything back: read the operating point off the map, then set the pair's
per-operation knobs by hand. Both need a calibrated discriminator
(`single_shot_readout`), and `pair_swap_flux_map` additionally needs the pair's
tracked coupler.

**Assignable flux source.** The flux-map experiments take `flux_component`:
ANY entity with a flux channel (another qubit's z, a coupler's z) swept
INSTEAD of each target's own z — `scqo run resonator_spectroscopy_flux
--targets q1 --set flux_component=q1_q2_c`. Such runs are RECORD-ONLY (the
fits describe crosstalk / coupler-induced shift, so nothing is proposed as the
target's own physics), and with a source assigned the targets themselves no
longer need their own flux wiring.

TRIAL-PHASE rule: while a device has no `components.lock`, the roster is
freely editable (`scqo doctor` reports the trial phase). The manager's
production cut freezes the expanded name set into `components.lock`; from then
on names are append-only forever — add entities or retire them
(`retired = true`), never rename or delete, because store keys, trends and
history key on the names — and `scqo doctor` FAILS on drift. Experiments
declare `target_kinds` + `required_operations`, and `scqo run` refuses
mismatched targets BEFORE touching hardware — a flux experiment on a
fixed-frequency chip is machine-refused (its qubits carry no flux channel),
which is exactly the gate the AI loop plans against.

## 10. Where does a value live? (the placement rule)

Every number in this system has exactly one home per role — `fact`, `knob` or
`monitor`, declared per field in the kind catalogs, and the role routes the
store. When you don't know where a value belongs — or why `scqo set` refuses a
name — apply this checklist **in order; first match wins**. Bench form:
`scqo state --rule`. Classify each *use* of a quantity, not each name: one fit
may legally write a knob AND a fact (`resonator_spectroscopy` writes the
`q1_ro.readout_freq_hz` setting and the `q1_res.f_dress0_hz` measurement from the
same dip — two roles, two homes, on purpose).

1. **Gone when the run ends?** Sweep windows, shot counts, analysis assumptions,
   `Optional=None` overrides ("keep the vendor's value") → **per-run experiment
   Parameters.** No standing value survives the run (audit records may: the
   punchout's recorded set-then-revert is the sanctioned pattern).
2. **True of the chip in the dark?** The sample would still have it with every
   instrument off and no pulse ever sent (T1, f_r, EJ, the flux arch) →
   **role `fact` → physical.json.** "Instrument-independent" means *no
   instrument setting realizes it* — a sample fact in setup coordinates
   (`q1_z.flux_per_phi0`, source units per flux quantum at the DAC) still
   lives here, on the flux CHANNEL, in the flux source's native unit. One file
   per (cooldown, setup): each value is conditioned on trusting that instrument,
   and cross-setup disagreement is *information* (instrument systematics).
   Write: estimator suggest→accept, or `scqo set`.
3. **Measured, but a vendor knob realizes the result?** (time of flight) → the
   measurement's product IS the vendor knob's new value: write it at the
   catalogued vendor path, offline, **in the catalog row's unit**. Being
   calibrated by an experiment never changes ownership.
4. **A knob the calibration loop must read/write vendor-neutrally — meaning the
   same signal on every backend?** → **role `knob` → scqo_state.json, on the
   channel that emits it.** Defined in the *experiment's frame*: each driver
   converts instrument ↔ experiment (hub-and-spoke — N converters, never N×N;
   instruments never convert to each other). Two value conventions:
   - absolute at the closest **declared** calibratable plane (Hz; dBm at the
     instrument output port; s) → `portable=True`;
   - dimensionless fraction of a chain scale, or an acquisition-IQ-frame
     quantity (`readout_rotation_rad`, `readout_threshold` — no declarable
     absolute plane) → `portable=False`, never copied across backends; a
     chain fraction must name a portable twin (`readout_amp` →
     `readout_power_dbm`, `drive_amp` → `drive_power_dbm`) or have its chain
     scale catalogued (`pi_amp` → the drive-port scale entries, the tracked
     `drive_power_dbm` realizers; `pi_amp` itself still has no power twin).
   Scope limit: per-qubit only (portable-looking setup plumbing like a TWPA
   pump is still vendor).
5. **Measured, no knob?** Consulted in `scqo state` as standing state to gate
   the next step → **role `monitor` → scqo_state.json, never pushed**
   (`fidelity_g`/`fidelity_e`, the blob positions — performance OF the current
   knobs, invalidated when they move). Only compared across runs → **run record
   only** (`p_e_given_g`, `pop_e_prep_g`, `power_context`) — compare across
   instruments by query, with backend provenance, never as state.
6. **Everything else is the instrument's** → vendor config, vendor-native
   units, catalogued (`scqo state --fields`) when relevant, with a kind:
   `[realizer]` realizes a neutral field — change THAT field via `scqo set`;
   `[candidate]` shared concept awaiting promotion (the visible backlog);
   `[vendor]` permanently vendor-owned, reason stated (LO = many splits give
   the same RF, and it is port-shared);
   `[unique]` exists on THIS backend only — **any experiment touching it runs
   only on this instrument** (the lock-in corollary; `update()` cannot touch
   these by construction, so lock-in only enters through `probe()`).

**Canonical gauge (why `readout_power_dbm = x` picks ONE att/amp split):** when
a neutral field is realized by several vendor knobs, the driver's setter is a
deterministic policy, not a free choice — the quantized coarse knob (even
0–60 dB att / the −11..+16 dBm full-scale grid) is chosen so the continuous
amplitude lands as high as possible but ≤ 0.5 full scale (linearity + sweep
headroom + SNR), and the amplitude absorbs the exact residual. Same target,
same split, stateless; the policy is documented in the binding's `convert`
text and the realized split is stamped into `power_context` every run.

**Playbook — I hand-touched a vendor value (e.g. the LO):** scqo state is
unaffected and unaware (no history row). Reload sessions so the vendor
re-solves against your value; check the catalog row's constraints (IF range,
band, *fused* cross-references) BEFORE editing; per-run truth is
`power_context` in each record.json. If the row is `[realizer]`, the tracked
neutral value now lies — re-assert it through the front door
(`scqo set q1.readout_power_dbm=...`) or revert your edit.

**Playbook — "set integration time to 2000 ns":** find the row with
`scqo state --fields`; the number you type is in **the row's unit, not yours**
(Qblox `integration_time` is seconds — typing `2000` sets a 33-minute window);
on QM the row says the knob is *fused* with the pulse length — the edit has
side effects. Read the cross-reference before promising the change.

**Playbook — I want to FORCE a realizer knob (e.g. `output_att = 10`):**
hand-edit the vendor config offline (the sanctioned route). Pull-mode startup
recomputes the true `readout_power_dbm` from your forced chain, and
`power_context` stamps it per run. **Precedence: the neutral field owns its
realizers whenever written** — any later `readout_power_dbm` write re-solves
the chain and overwrites your forced value. Want att = 10 AND power = x? Force
att offline, then `scqo set q1.readout_amp=...` for the exact amplitude — that
half is recorded, and the coupled sync writes the true resulting power into
state. (A generic `scqo set --vendor` is deliberately not built.)

**Promotion (vendor `[candidate]` → neutral field):** *eligibility* = one SI
convention, pre-declared in the catalog entry, mapping deterministically onto
every backend (extra vendor granularity is never a blocker — it becomes vendor
fine print defaulting to the neutral value). *Trigger* = a loop experiment
needs it — decided at release review, **never at the bench**. *Interim* = the
fitted value goes in `Result.fit` (run record); hand-apply via vendor tools if
needed. *Cost* = one FieldSpec in the kind catalog + one hardware-tested
channel-view setter per driver (conversions live only there) + the fieldmap
binding + tests.

## 11. The readout schema (what a probe's dataset looks like)

Every experiment's dataset is built from the same two questions plus one
multi-qubit choice — declared, never inferred:

1. **Analog or digital?** (`use_state_discrimination`) — does the instrument
   return the raw I/Q voltages, or discriminate each shot on the FPGA?
2. **Shot or average?** (`readout_mode`, on experiments that offer both) — is
   every shot kept (full information, more memory), or does the instrument
   average them (compact)?
3. **For a multi-qubit target** (a pair), digital + average stores the **joint**
   distribution — the probability of each joint outcome — because averaging the
   members independently would throw the correlations away. Marginals are its
   partial trace: derived, never stored.

The variable NAME carries the semantics, so a dataset is self-describing:

| Combo | Vars (units) | Dims |
|---|---|---|
| analog + shot | `I`, `Q` (V) | `(target[, member], *sweeps, shot_idx)` |
| analog + average | `I`, `Q` (V) | `(target[, member], *sweeps)` |
| digital + shot | `state` (int level ≥ 0) | `(target[, member], *sweeps, shot_idx)` |
| digital + average, marginal | `population` (prob) | `(target[, member], *sweeps)` |
| digital + average, joint (pairs) | `joint_population` (prob, sums to 1) | `(target, joint_state, *sweeps)` |

Rules of the schema (each is checked, not a convention):

- **`state` is ALWAYS a per-shot outcome; probabilities are `population` /
  `joint_population`.** A per-shot `state` is an integer LEVEL, not a bit — a
  qubit may read out |2⟩, and a binary reduction counts level ≥ 1 as excited.
- **`sweeps` are the physics axes; readout adds its own dims** — `shot_idx`
  (unit "shot"), `member`, `joint_state`. The experiment's `DatasetContract`
  declares which readout dims each accepted form carries (`readout_dims` /
  `alt_readout_dims`), and they are validated with the same rigor as sweeps.
- **`member` order and `joint_state` digit order are the roster ROLES (high,
  low)** — the leftmost label digit is the high member. The `member` coord
  carries the role labels (`"high"`, `"low"`); the roster maps them to the
  actual qubit names per pair (one flat coordinate cannot carry different
  names for different pairs in a multi-pair run).
- **`joint_state` labels are per-member level digits** — `"00" "01" "10" "11"`
  for a binary pair, `"02"`/`"12"`/… when a member is f-resolved — always
  generated (`joint_state_labels(m, n_levels)`), never hand-listed.
- **A backend that cannot realize a requested combo REFUSES it by name, never
  downgrades** — the same boundary rule as `reset_method`.
- Cross-target correlations (several independently-targeted qubits in one
  program) are NOT modeled — only per-target joints are. A pair is ONE target.

The reduction helpers live once, in `scqo.experiments._capabilities.state_readout`
(`states_to_joint_population`, `joint_to_marginals`, `joint_state_labels`,
`member_order`): the simulated backend, the drivers' `reduce_raw` and the tests
all share them. scqat never imports them — an estimator reads the dataset.

Example — `qc_n_swap_amp` offers both modes on the same physics: `readout_mode=
"average"` stores `joint_population @ (target, joint_state, flux_amp_v,
swap_count)` (4 floats per point); `readout_mode="shot"` stores
`state @ (target, member, flux_amp_v, swap_count, shot_idx)` (every shot,
per member) and `estimate()` reduces it to the same joint distribution — both
modes yield identical maps, the trade is disk vs information.

## 12. Worked workflow: calibrating a partial swap (QCQ pair)

Goal: turn the survey maps into a stored **partial-swap operation** of a chosen
angle θ on a tunable-coupler (QCQ) pair, then verify and fine-tune it with
`qc_n_swap_amp`. This is today's MANUAL chain — three `scqo` runs and one
hand-edited register step; the honest-limits list at the end says what a future
closed loop would automate.

### The physics in five lines

Each application of the swap pulse transfers population `P = sin²θ` with
**θ = J_eff · t_p** (the model lives in
`scqat/notebooks/calculator/repeated_partial_swap.ipynb`; θ = π/2 is a full
iSWAP, and the collisional-reset link is θ = √(γ·dt)). The three knobs have
three distinct roles:

- **qubit (control) flux amplitude — the RESONANCE knob.** It brings the two
  members onto resonance (δ = 0) during the pulse: maximum transfer envelope,
  zero detuning phase per application. It is never the angle knob — an
  off-resonance "partial transfer" is impure (it adds a δ-phase each swap and
  loses contrast).
- **coupler flux amplitude — the ANGLE knob.** `J_eff(Φ_c)` is continuous, so at
  fixed duration θ tunes smoothly from ~0 (the decouple point) upward. This is
  why the flux map is the entry point on a QCQ pair.
- **duration t_p — coarse; keep it FIXED.** It is baked into the shaped pulse
  (4 ns grid, raised-cosine edges), and `flattop_cosine` refuses duration
  overrides for exactly that reason.

Verification is built into `qc_n_swap_amp`'s N-axis: at the operating point the
population oscillates with period **π/θ applications** — a full swap alternates
every application, a √iSWAP (θ = π/4) cycles every 4, θ = π/8 every 8. You read
the angle off the map by counting; no fit needed. (The peak transfer over N
still reaches ~1 at N ≈ (π/2)/θ, so `min_transfer` keeps its meaning.)

### Step 0 — prerequisites

- accepted `single_shot_readout` on both members (the pair maps read out jointly
  discriminated);
- accepted `pair_zz_coupler` (the coupler parks at its decouple point —
  `idle_flux` on the coupler's flux channel — so the swap only happens while the
  coupler pulse plays);
- the pair declared in `components.toml` with its `coupler` role.

### Step 1 — survey the swap spot

```
scqo run pair_swap_flux_map --targets q1_q2
```

Run at the intended gate length: `flux_pulse_shape=flattop_cosine` with
`swap_time_ns` UNSET plays the operation's own native length (a shaped pulse
refuses overrides — truncating the edges changes the pulse area). Read the
joint-population figure (`analysis/q1_q2/pair_swap_flux_map.png`): the
resonance line in qubit flux, bending with coupler bias, and the transfer
growing as the coupler activates. Then pick the operating point:

- **full swap**: `best_qubit_flux_v` / `best_coupler_flux_v` from `result.fit`
  (the transfer peak);
- **arbitrary θ**: stay ON the resonance line and choose the coupler amplitude
  where the transfer column reads `sin²θ_target` — e.g. θ = π/4 → the P ≈ 0.5
  contour. Narrow the window and re-run until the contour is resolved by a few
  grid points.

### Step 2 — materialize the operation (once per pair per angle)

The write-back is a hand-run register step in LCHQMDriver (one named
(pulse, macro) pair PER ANGLE — keep `iswap` as the full swap and add e.g.
`partial_swap` beside it):

1. Edit + run `quam_config/register_flattop_cosine.py`: set `OP` to the angle's
   pulse name (e.g. `partial_swap_flattop_cosine`), `LENGTH`/`EDGE_WIDTH` to the
   surveyed duration, and the per-channel amplitudes — **control-z = the
   resonance amplitude, coupler = the angle amplitude** (the script registers
   separate pulse instances per channel for exactly this reason).
2. Edit + run `quam_config/register_swap_macro.py`: attach
   `pair.macros["partial_swap"] = ISwapImplementation(flux_pulse=<OP>)` — the
   macro KEY is the operation name `qc_n_swap_amp` selects.
3. Declare the operation on the pair in `components.toml`:
   `operations = ["iswap", "partial_swap"]` (appending an operation is legal
   even on a production-frozen roster).

Constraints (the scripts and probes refuse violations by name): length a
multiple of 4 ns and ≥ 16; `2·edge_width ≤ length`; amplitudes below the flux
port's rail; the control-z amplitude ≠ 0 (it is the divisor of the probe's
volts → amplitude_scale conversion).

### Step 3 — verify and fine-tune

```
scqo run qc_n_swap_amp --targets q1_q2 --set swap_operation=partial_swap
```

Window the amplitude sweep around the registered control-z amplitude and give
`swap_counts` at least two target periods (θ = π/4 → `[0..16]`). Read the map:

- the **cleanest oscillation column** (maximum contrast, no drift with N) is the
  resonance — if it sits off the registered amplitude, put the fitted amplitude
  into the register script and re-run it;
- the **oscillation period in N is π/θ** — if the period is off target, nudge
  the COUPLER amplitude along the flux-map contour and re-register.

`readout_mode=shot` keeps every shot (per-member states) when the downstream
analysis wants raw trajectories (the collisional-model notebooks).

### Pin the workflow

`~/.scqo/parameters.toml`:

```toml
[pair_swap_flux_map]
flux_pulse_shape = "flattop_cosine"
drive_side = "low"
flux_side = "low"

[qc_n_swap_amp]
swap_operation = "partial_swap"
min_flux_amp_v = 0.12    # window around the registered control-z amplitude
max_flux_amp_v = 0.18
num_amp_points = 31
swap_counts = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16]
drive_side = "low"
flux_side = "low"
```

### What is manual today (and the future hooks)

- **The angle is read by eye** (period counting). scqat's
  `SwapOscillationEstimator` already fits `f = θ/π` per amplitude row and is
  unused — wiring it per-amplitude is the natural first automation.
- **The register scripts ARE the writeback.** The composite per-operation knobs
  (`partial_swap_duration_s`, …) exist in the catalog, but the QM binding for a
  pair duration is deliberately Unrealized until a calibrating experiment lands
  ("promote to a coupled binding"), and no experiment proposes a composite knob
  yet — the future closed loop gives the flux map and `qc_n_swap_amp` real
  `update()`s.
- **The chevron** (`pair_swap_chevron`) is the directly-coupled sibling survey
  (member flux × duration); on a QCQ pair the flux map is the entry point.

## 13. Troubleshooting

**First move, always: `scqo doctor`** — it checks your venv, drivers, the whole
config chain (shared config, user overlay, parameters file), data_root, the
cooldown registry, and the device model (roster, design coverage, lock drift,
roster-vs-vendor wiring), and tells you what is wrong and how to fix it.

| Symptom | Cause / fix |
|---|---|
| `ModuleNotFoundError` / `lab config not found` / nothing gets saved | setup problem — see [INSTALL.md](INSTALL.md) §1–§2 and the §6 symptom table |
| `scqo: command not found` (or the term is not recognized) | no venv activated — or scqo was upgraded without re-running the INSTALL §1 `uv pip install -e` line (the command registers at install time) |
| `notepad ...` over SSH does nothing | GUI apps have no display in an SSH session (the process starts invisibly on the server) — use the §5 editing methods (here-string / scp / VS Code Remote-SSH) |
| `device ... is on backend 'qblox' ... driver is not registered in this environment` | right command, wrong venv — the message names the venv to activate (or, if you ARE in it, the install line to re-run) |
| `invalid cooldown registry ...` or another refusal naming `cooldowns.toml` at run start | the manager's cycle registry is broken or incomplete (it stamps runs and selects the instrument, so runs refuse BEFORE instrument time) — `scqo device cooldown` (no args) validates it; the message names the fix (INSTALL §6 has the full list) |
| `no ...components.toml — the roster is required` | the device is not described yet — the manager writes its `components.toml` (schema 3; the message prints the smallest valid file) and, usually, a `design.toml` beside it (section 9) |
| `cycle ... has N setups and none is selected` | the ACTIVE cycle offers several measurement setups and a run will not guess — pick yours once: `scqo user --setup <name>` (a single-setup cycle needs no selection) |
| `setup 'x' ... does not exist in the ACTIVE cycle` | your selection went stale (typically after a new cycle started) — `scqo user --setup <name>` picks a current one, `scqo user --clear-setup` returns to auto-selection; bare `scqo user` always shows what a run would resolve to |
| A run shows `datastore_error` | measurement succeeded; only saving failed (disk full/locked). Fix the disk, rerun |
| `invalid parameter-defaults file ...` (even on `--help`) | your `parameters.toml` has a syntax error — it affects measurements, so it never fails silently. Fix the named file |
| `find_runs` misses runs you can see on disk | index stale → `python -m scqo <data_root>` |
| Unknown `run_id` in `--show` | same — rebuild the index |
| Want a clean slate | deleting `index.sqlite*` (all three files) is always safe; the folders are the data |

## 14. What the system does NOT include yet

Everything above is real: **both instruments are hardware-proven** through this path
(Qblox cluster and OPX1000, since 2026-07-05), the catalog holds 35 experiments, and
the GUI you read about in section 4 (viewer + datasette) is shipped. Still ahead:

- **Device-level inference** (Phase 3): combining runs into EJ/EC, anharmonicity,
  flux response via scqat + SCQ.jl — the physical-parameter ledger (`physical.json`:
  T1/T2, the flux transfer function, `ej_sum_hz`, `f_bare_hz`, `g_hz`) is its input.
- **Running measurements — and accepting updates — from the viewer**, plus a
  per-instrument run lock — until then, measuring and deciding stay on the CLI and
  one-measurement-per-instrument stays a social rule.
- **The AI loop**: the catalog/Session JSON surface is built for it, but no agent
  drives it yet.
