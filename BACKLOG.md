# BACKLOG — deferred features and known issues

The memory that CLAUDE.md is not allowed to be. Append an entry whenever you DEFER a feature
or DISCOVER a non-urgent problem while working on something else; give it the date, what you
were working on when you found it, the pointer (file / function), and what "done" looks like.
Delete the entry when it lands and name the commit or `RELEASES.d/` fragment in its place for
one release, then drop it. Priorities: **high** = blocks or corrupts data, **medium** = wrong
provenance or a trap a user can walk into, **low** = hygiene.

## Planned features (deferred by decision)

### F1 Pull-seed drift rows — record hand edits of the vendor config as history (medium)
- Added 2026-09-03 while doing the drive-frequency audit + setup snapshots.
- Problem: `RecordingDevice._seed_pull` writes no history rows by design (seeding is not a
  change), so a hand edit of the vendor config leaves a hole; `Store.record` takes `old` from
  scqo_state.json, not the vendor, so the NEXT row records a wrong `old` (even a first-ever
  write records `old=None` while `scqo set`'s confirmation shows the vendor value); and CLI
  `scqo state --sources` (live values) and the viewer (file values) disagree after a hand edit.
- Where: `scqo/device.py` `_seed_pull` / `_set_knob`, `scqo/stores.py::Store.record`,
  `scqo/provenance.py`, `scqo/viewer/app.py::_context_sources`, `scqo/changes.py` (a new
  column, e.g. `origin`).
- Done when: at pull-seed a vendor value that differs (exact compare) from the last recorded
  one appends a ChangeRecord marked as vendor-originated (observer login, discovery time);
  provenance gains that status; CLI and viewer agree.

### F2 Read the written field back after the push (medium)
- Added 2026-09-03 while explaining push-first.
- Problem: `_set_knob` records the REQUESTED value and `_sync_coupled` skips the written
  field, so grid-rounding setters (QM `thermalization_time_s` 4 ns, `readout_depletion_s`
  4 ns, `flux_delay_s` 1 ns) leave history `new` != vendor value; the next session's
  provenance shows `(externally changed)` with no hand edit (measured: requested
  2.34567e-4 s, vendor 2.34564e-4 s).
- Where: `scqo/device.py` L440-453; `scqo-qm/scqo_qm/quam_fields.py`
  `set_thermalization_time` / `set_readout_depletion` / `set_flux_delay`.
- Done when: the recorded `new` is the vendor readback (or both are stored) and a rounded
  write no longer flips provenance to external.

### F3 Experiment-reported run-scoped amendments in the run record (medium)
- Added 2026-09-03 with the setup-snapshot feature (deliberately left out).
- Problem: the setup snapshot is the STANDING config at run start; the broadband probes'
  per-segment LO/band/RF tables and the cryoscope's run-scoped drive op never appear in any
  record (they are restored before the run ends).
- Where: `scqo/experiment.py` (a `note_vendor_amendment(**fields)` accumulator persisted as
  record.json `vendor_amendments`), `scqo-qm/scqo_qm/experiments/broadband_*.py`,
  `qubit_spectroscopy_cryoscope.py::install_drive_op`,
  `scqo-qblox/scqo_qblox/experiments/broadband_*.py`.
- Done when: a broadband run's record lists every segment's LO/band/RF and the cryoscope
  run its drive op parameters; additive only, no sequence change.

### F4 Snapshots listing + index column (low)
- Added 2026-09-03 with the setup-snapshot feature.
- A device-level viewer page listing the distinct setup snapshots (hash, first/last run,
  source setup), and `scqo find --snapshot <hash>` — the latter needs a `runs` column, so a
  `SCHEMA_VERSION` bump and a full reindex on every lab.

### F5 `drive_freq_hz` read side: `f_01` or `xy.RF_frequency`? (decision)
- Added 2026-09-03 with the drive-frequency audit.
- Today `get_drive_freq` reads `f_01` (bookkeeping) while the line plays `xy.RF_frequency`;
  the startup audit keeps them equal. Reading the RF instead would change the "uncalibrated
  qubit reads None" contract (`anchor()` falls back to design.toml today).
- Where: `scqo-qm/scqo_qm/quam_fields.py::get_drive_freq`, `qm_backend.py::_read_or_none`.

### F6 Run the whole-tree audits from `scripts/check_real_config.py` (low)
- Added 2026-09-03. The QM self-test loads the state directly and runs none of
  `flux_point_problems` / `flux_headroom_problems` / `drive_frequency_problems`.

### F7 Lift the TEMPORARY push refusal when the qualibrate GUI stops writing QUAM
- Added 2026-09-03 (fragment `hardware-push-refusal`).
- Delete the guard in `scqo/labconfig.py::make_session` + its tests
  (`test_make_session_refuses_push_on_hardware_backends`,
  `test_hardware_setup_with_push_config_refuses_without_traceback`) and, for QM, the driver
  guard in `scqo-qm/scqo_qm/scqo_backend.py`; keep the `state_sync` parse validation.

### F8 PR #29 viewer dashboard — held pending a CHARTER decision
- Carried over; the reviewed defects live only in the session memory `pr29-viewer-dashboard-held`.
  Decide the charter, then either land with the fixes or close.

### F9 Lab report: value from physical/state, the +/- from the campaign that produced it (medium)
- Added 2026-09-05 while fixing the lab report's dressed-frequency source.
- Problem: the authority for a reported value should be `physical.json` / `scqo_state.json`,
  which is accept-gated by construction - a campaign's aggregate suggestion is `role="fact"`
  (`t1_s` / `t2_star_s` / `t2_echo_s` / `n_th`) and reaches the store only once a human
  accepts it. But `extract_chip_metrics` takes t1 / t2_ramsey / t2_echo / readout_fidelity /
  n_th CAMPAIGN-FIRST with the physical value as the fallback, and
  `query_campaign_statistics` never looks at suggestion status - it takes the newest
  qualifying campaign, reviewed or not. So one sheet carries two rules: the `#1` columns are
  reviewed, the `#100` columns are not. It was a fair workaround while campaigns did not
  offer their accept step (that landed 2026-09-04, `06ef197`); it is not one now.
- Where: `scqo/viewer/lab_report/metrics.py::extract_chip_metrics` - the `state`/`phys`
  dicts drop each row's `"source"` key, and the statistics are fetched independently by
  device/cooldown. Both inputs already exist: `scqo/viewer/app.py::_param_rows` puts the
  full `scqo.provenance.live_sources` info dict on every row, and that dict carries
  `campaign_id` whenever the current value traces to a campaign accept.
- Design already decided (do not re-litigate):
  - VALUE from physical/state; the +/- and `n` from the ONE campaign named by
    `source["campaign_id"]`, never `query_campaign_statistics`' newest-first pick.
  - That route also dissolves the "statistics are keyed by FIT key, suggestions by CATALOG
    field" join problem - the change history already made the link - and `live_sources` is
    strict-match, so a drifted value reports "external" instead of crediting a campaign.
  - NO CORRESPONDING VALUE MEANS AN EMPTY CELL (operator's call, 2026-09-05): the export
    shape is someone else's template and must not grow rows. So when the physical value's
    provenance is not a campaign (status run / manual / external / unrecorded), `T1 #100`
    is blank - and since `DATA_ROWS` has no `T1 #1` row, that chip's T1 leaves the Data
    sheet entirely until a campaign is run AND accepted.
  - Flip the `n_th` precedence in the same change (it feeds `temperature_mk`). Deliberately
    NOT a separate entry: it cannot land alone without leaving two rules in one sheet.
  - Trap: `suggestions_pending == 0` is NOT "reviewed". A campaign whose quantities all fell
    below `[writeback] min_n` proposes nothing, so its pending count is zero too
    (`scqo/datastore.py::_upsert_campaign`). The check must read the manifest's suggestion rows.
  - The unified (whole-cooldown) context merges rows by `source["timestamp"]` and keeps each
    winning row dict whole, so `value` and `source` stay same-sourced - but a row whose
    status is "external" still competes on timestamp.
- Done when: a `#100` cell carries a number only while that physical value's provenance is a
  campaign, its +/- comes from that same campaign, and `tests/test_lab_report.py` pins that
  an un-accepted campaign does not reach the sheet.

### F10 OPX+ flux predistortion: the FIR/IIR writeback (medium)
- Deferred 2026-09-11 when the OPX+/Octave support landed (scqo-qm, phases 0-5 + 7;
  phase 6 was skipped by decision because the lab's OPX+ wiring is not settled).
- Problem: `apply_distortion` writes an LF-FEM `opx_output.exponential_filter`, a list
  of (A, tau) pairs. An `OPXPlusAnalogOutputPort` has no such field - it predistorts
  through `feedforward_filter` (FIR) + `feedback_filter` (IIR), both on the shared
  `LFAnalogOutputPort` base. That is a DIFFERENT decomposition, not a renamed field, so
  the conversion is real arithmetic and not a mapping.
- Today it refuses BY NAME rather than doing the wrong thing, and
  `operator_commands()` hides the CLI on a tree whose flux lines are all OPX+, so an
  operator is never sent at a door that is walled up. That refusal is the correct
  interim behaviour, not a stopgap to rush past: before it, quam ACCEPTED the
  assignment on a port class with no such field and then dropped it on `to_dict()` -
  printed success, saved state.json with no filter in it, flux line still distorted.
- Both cryoscopes still MEASURE the distortion on an OPX+ and their facts land in
  physical.json as usual; only the vendor writeback is missing.
- Where: `scqo-qm/scqo_qm/backend/_distortion.py::_exponential_filter_port` is the one
  door both entry points resolve through, so the FIR/IIR path has exactly one place to
  attach. `scqo_qm/_family.py::flux_port_family` already answers which family a channel
  is on.
- Only worth doing once an OPX+ with z lines actually exists - see the hardware
  validation row below, which has to come first.
- Done when: an accepted cryoscope's facts reach `feedforward_filter` / `feedback_filter`
  on an OPX+ port, the round trip is pinned offline against a real built tree
  (`scripts/make_opxp_fixture.py`), and the CLI stops being hidden on those trees.

### F11 Offline re-estimate: the `scqo estimate` verb (P2) and its surroundings (P3)
- Added 2026-09-20, when P1 of `docs/reestimate-plan.md` landed (the self-contained
  `dataset.nc`: `Experiment.run_estimate`, `scqo/estimate_inputs.py`, the embedded
  acquisition-time snapshot). The plan itself was not in this file at all, so the
  remaining two thirds were invisible to "consult BACKLOG.md before planning".
- P2 is the verb: `Session.reestimate()` + `scqo estimate <run_id> [--set k=v]`, the
  estimate-stage field marking (`estimate_field` in `parameters.py`; only a field no
  probe reads may be overridden), the derived-run shape (`reestimate_of`, era inherited
  from the root, no campaign stamp), index `SCHEMA_VERSION` 10 -> 11, and the
  whole-registry round-trip test. Old runs (no embedded attrs) take their inputs from
  `device_before.json` + the setup snapshot — with an UNAVAILABLE physical store when
  the run predates 2026-09-03, because an empty one would silently downgrade
  `fact_sourced()` from measured to design.
- Still to decide before P2 starts (plan §1): D1 verb name, D2 derived-run dataset is a
  COPY, D4 `update="apply"` refused at re-estimate, D5 the first batch of
  estimate-stage fields. D3 (embed the whole snapshot) was decided and is implemented.
- P3 is the surroundings: viewer links between a root and its re-fits, TUTORIAL/AGENTS
  sections, the two drivers' AST tests (no `self.estimate()`), release fragment.
- Also deferred with the plan (§8): campaign statistics recomputed after a child is
  re-fitted; BATCH re-estimation (refresh a window of old runs after a fit fix — needs a
  folder-multiplication policy first); rebuilding pre-2026-09-03 facts from
  `history.sqlite` by `started_at` instead of `--facts live`; `design.toml` in the setup
  snapshot; folding the 5 `run()` overriders into base `run()` + an `acquire()` hook
  (they are all "boundary write around acquire", and none calls
  `attach_acquisition_coords()`).
- Done when: `scqo estimate <run_id> --set analysis_method=circle` produces a derived run
  on a REAL old 5Q4C `resonator_spectroscopy` run, with the figure and the absolute
  frequency checked by eye; and `docs/reestimate-plan.md` is deleted, its content moved
  into TUTORIAL/CLAUDE.md.

### F12 Finish and review the `peak_inverted` polarity carrier (medium)
- Added 2026-09-20, when the WIP branch `claude/vigilant-nobel-92e114` was merged into
  scqat main (merge `0b14f0a`, one commit `193b3f0`). That commit calls ITSELF
  "UNFINISHED AND UNREVIEWED": it was last edited 2026-08-26 and committed only to
  preserve worktree state across the `D:\github` -> `D:\project\scqo_system` move, so it
  has never had a review pass. It merged clean (main had touched none of its 9 files) and
  the three touched test files pass, which is why it is a backlog entry and not a revert.
- What landed: `peak_fit.fit_peaks` picks the stronger polarity per row and, when the dip
  wins, fits POSITIVE Lorentzians on the negated trace — so `peak_amplitude` is
  polarity-NORMALIZED and cannot by itself tell an absorption dip from an emission peak
  (a NEGATIVE amplitude means a badly-conditioned fit, not a dip). `peak_inverted` (per
  peak) and `n_inverted` (per map) now carry that distinction through `track_peaks`'
  pooling and out through both estimators' plot_data/attrs; `reduced_map` is never negated.
- What is NOT finished: the flag has NO consumer. Both visualizations only state that it
  "rides along in plot_data for consumers that need dip-vs-peak" — nothing plots it,
  nothing branches on it, and nobody takes the signed
  `np.where(peak_inverted, -peak_amplitude, peak_amplitude)` the docstrings advertise. A
  dip-heavy map still reads as if every peak were an emission peak, which is the trap the
  commit set out to close.
- Second trap, for whoever writes that consumer: this CHANGED the result contract of
  `track_peaks` and of both estimators' plot_data. A consumer must tolerate
  `peak_inverted` being ABSENT, or replotting any `plotdata.nc` written before
  2026-09-20 breaks — the same way the readout average-mode change already stopped old
  readout plotdata from replotting.
- Where: `scqat/tools/peak_map.py` (`track_peaks`);
  `scqat/estimators/parametric_drive_resonance/{estimator,visualization}.py`;
  `scqat/estimators/qubit_spectroscopy_flux/{estimator,peaks,visualization}.py`. Tests:
  `tests/test_peak_map.py`, `tests/test_parametric_drive_resonance_estimator.py`,
  `tests/test_qubit_spectroscopy_flux_estimator.py` (23 passed, 0 skipped at the merge).
- Done when: the polarity work is reviewed and actually CONSUMED — at minimum the two
  visualizations render dip-fitted peaks distinguishably and the docstrings stop promising
  a consumer that does not exist. The release fragment is already written
  (`RELEASES.d/peak-inverted-polarity.toml`, kind `additive`) and says in as many words
  that the flag has no consumer, so the next combo ANNOUNCES the gap instead of shipping
  it silently — do not treat that fragment as evidence the feature is finished.

## Known issues / potential problems (found in passing)

### I1 Qblox broadband probes swallow a failed clock restore (medium)
- Found 2026-09-03. `scqo-qblox/scqo_qblox/experiments/broadband_resonator_spectroscopy.py`
  (the `clock_freqs.readout` restore) and `broadband_qubit_spectroscopy.py` (the
  `clock_freqs.f01` restore) wrap each restore in `try/except Exception: pass`; a failed
  restore leaves the element parked at a sub-band frequency for the rest of the session.
  Since the setup-snapshot feature this shows up as `setup_snapshot.drift`; the LO restore
  next to it raises, and the clock restore should too.

### I2 A vendored node can re-create the f_01 / RF split (medium, cannot be fixed here)
- Found 2026-09-03. `scqo-qm/calibrations/17_pi_vs_flux_long_distortions.py` L144 shifts
  `xy.RF_frequency` alone; after a GUI run the next scqo session refuses with
  "drive frequencies ..." and the operator re-aligns `f_01` by hand. Vendored file — never
  edit; document in the operator notes if it bites.

### I3 scqo-qblox CLAUDE.md mixer-AMC premise no longer literal (low)
- Found 2026-09-03. The paragraph says the AMC survives a run only because hw_config has no
  `hardware_options.mixer_corrections`; the live chipA `hw_config.json` DOES carry that block
  (`auto_lo_cal` / `auto_sideband_cal` modes, no `mixer_corr_*` values). Verify the failure
  mode and reword.

### I4 Qblox LO numbers disagree between docs, fixtures and the live config (low)
- Found 2026-09-03. `tests/fixtures/hw_config_min.json` + `hw_config_2q.json` and
  `tests/test_qblox_power.py` use 5.8e9 / 4.5e9; the live chipA `hw_config.json` runs
  5.1e9 / 3.0e9; `D:\qpu_data_dev\chipA\cd1\qblox\backend_config\EDIT_ME.md` still says
  5.8 GHz. Only `power_context` / the setup snapshot record which one ran. Refresh EDIT_ME.md.

### I5 `quam_state/state.json` has case-colliding keys (low)
- Found 2026-09-03: `CZ_time` and `Cz_time`. Python is fine; PowerShell `ConvertFrom-Json`
  refuses the file, and any case-insensitive consumer of a setup snapshot would break.

### I6 `QUAM_STATE_PATH` is set process-wide and never unset (low)
- Found 2026-09-03. `QMBackend.load` exports it; `QMDeviceModel(state_dir=None)` relies on
  it for `save()`; a second session or script in the same process inherits the folder.

### I7 `state_lib/10Q` resonator `f_01` vs `resonator.RF_frequency` disagree (low)
- Found 2026-09-03: 1.0–1.4 MHz apart on q3/q4/q5. scqo reads the RF only, so it is not a
  silent failure today; decide whether the readout pair needs an audit like the drive pair.

### I8 `tests/test_index_scale.py` hardcodes `schema_version` 9 (low)
- Found 2026-09-03. A value in the INSERT tuple, not the constant — harmless until a schema
  bump changes what the column means.

### I9 A resolved-but-NaN aggregate still explains nothing (low)
- Found 2026-09-04 while making the campaign accept step visible.
- `suggestion_notes` covers the `min_n` shortfall only. A target whose statistic
  RESOLVED (`n >= min_n`) but is NaN/Inf is dropped by the finiteness gate in
  `scqo/session.py::_campaign_suggestions` and still says nothing — deliberately, since
  it is a different cause with a different remedy (the fits are bad, not too few), and
  folding it into the min_n wording would be a lie. Pinned as silent by
  `tests/test_campaign.py::test_generation_filters_nonfinite_and_nonscalar`.
- Done when: a NaN-only target gets its own note naming the fit failure, not the floor.

### I10 Nothing machine-checks the per-repo test commands (low)
- Found 2026-09-04 writing `ENVIRONMENTS.md`.
- Each sibling's `ENVIRONMENTS.md` stub repeats its own test command verbatim (so a reader who
  does not click the URL cannot reach the wrong one), and `scripts/check_contribution.py`'s
  `TEST_COMMANDS` holds a fourth copy. Four strings that must agree, with nothing enforcing it —
  the same drift that produced the four arrangements in the first place, one level up.
- Done when: a test in `SCQO/tests/` asserts `ENVIRONMENTS.md` exists and that every command in
  its per-repo table appears verbatim in `TEST_COMMANDS`. `tests/test_docs_current.py` is the
  precedent for "a doc block that must not rot".

### I11 `scqo-qm/.venv` exists and is unusable (low)
- Found 2026-09-04. It holds no `qm`, no `quam`, no `qualibrate`, no `scqat`, `scqo` frozen at
  2.3.0 — residue of a stray `uv run`, which resolves from `pyproject.toml` instead of
  `requirements-qm.lock.txt`. The docs now say it is not a thing and `check_contribution.py`
  warns when it is present, but the directory itself is still sitting there for the next person
  to point an interpreter at.
- Done when: deleted on the lab machine (nothing references it), or `scqo-qm` grows a
  `[tool.uv]` guard that makes a stray `uv run` fail loudly instead of building it.

### I12 Stale names and version lines (hygiene)
- `scqo/cli/__main__.py::_usage()` still names LCHQBDriver / LCHQMDriver.
- `scqo-qm/scqo_qm/backend/qm_backend.py` module docstring: "shared with the qualibrate
  writebacks" (retired).
- `scqo-qm/quam_state/` holds six `*.bak*` files; QUAM merges any `*.json` under its state
  directory, so a backup must never be named `*.json`.

### I13 `RB fidelity #100` exists only through the campaign bypass (low)
- Found 2026-09-05 while planning F9.
- `qubit_sqrb` has no `update()` (only `define_sweep` / `simulate` / `estimate` / `probe`), so
  it never proposes anything, and `scqo/catalog.py` has no `rb_fidelity` field on any kind.
  `metrics.py`'s `rb_fidelity_single` is therefore ALWAYS None, and the row's only source is
  the direct campaign-statistics read - which F9 removes, blanking the row permanently.
- Same shape, second instance in the same function: `state.get((ro, "readout_fidelity"))` is
  dead too - the catalog gives the readout channel `fidelity_g` / `fidelity_e` / `fidelity_f`
  and no `readout_fidelity`. The live path is the `(fidelity_g + fidelity_e) / 2` branch
  above it.
- Where: `scqo/experiments/qubit_sqrb.py`; the transmon mode fields in `scqo/catalog.py`;
  the `ro_single` and `rb_single` fallbacks in `scqo/viewer/lab_report/metrics.py`.
- Done when: `rb_fidelity` has a catalogued home and `qubit_sqrb` an `update()` that writes
  it, or the row and both dead `_first` branches leave the report together.

### I14 The `--note` backslash value is never asserted to survive (low)
- Found 2026-09-07 while fixing the `\q` SyntaxWarning on the same line.
- `test_start_escapes_metadata_and_validates_cycle_id` promises in its docstring that quotes
  AND backslashes in `--fridge` / `--packaging` / `--note` "must never corrupt the shared
  registry", but the only survival assertion is `'PCB "rev3"' in show.stdout` - the QUOTE
  case. The backslash note (`r"D:\qpu\chipA path"`) is covered only indirectly, by exit
  code 0 and "the registry re-parses cleanly", so a TOML writer that silently ate, doubled
  or normalised a backslash would still pass this test green.
- Where: `tests/test_cli_cooldown.py::test_start_escapes_metadata_and_validates_cycle_id`,
  the `show.stdout` asserts at the end.
- Done when: the note's exact value is asserted to round-trip (`assert r"D:\qpu\chipA path"
  in show.stdout`), or it is read back out of `cooldowns.toml` and compared.

### I15 Stale cp950 comment in `test_cli_doctor.py` (low)
- Found 2026-09-07 while pinning the subprocess encodings for the 7 cp950 test failures.
- The comment above the `UV_PYTHON_INSTALL_DIR` assert says the `§` "can mangle when
  subprocess stdout round-trips through the OS locale encoding on Windows". Both ends of
  that pipe are now pinned to UTF-8 (`encoding="utf-8"` on the `subprocess.run` plus
  `PYTHONIOENCODING` in the env), so the premise is false and the ASCII-safe-token
  workaround it justifies is no longer needed - the assert could name the `§1` pointer.
- Where: `tests/test_cli_doctor.py:296`, end of `test_doctor_renders_the_profile_witness_rows`.
- Done when: the comment is dropped or rewritten and the assert reflects what is actually
  guaranteed now that both ends of the pipe are pinned.

**5Q4C q1_q2 `decouple_offset` = 0.08 V does not decouple (found 2026-09-15, hardware).**
Every probe parks the coupler there (`initialize_qpu` -> `apply_all_couplers_to_min()` ->
`to_decouple_idle()`), and coupler pulses ride ON TOP of it — so the park is the standing
condition for every run that plays no coupler pulse. Measured residual at that park:
t_pi ~ 145-152 ns, i.e. J/2pi ~ 1.7 MHz, a FULL swap whenever the members are brought into
resonance. The real J minimum is at a LINE voltage of ~0.148-0.165 V.
- Evidence: flux map `20260915-162437-647` (40 ns fixed) column maxima — line 0.080 V ->
  0.21 transfer, line 0.1475-0.160 V -> 0.01-0.02, line 0.190 V -> 0.45; and the chevron A/B
  `20260915-183235-061` (`coupler_flux_v=0.08`, line 0.16 V -> transfer 0.17, failed) vs
  `20260915-183332-183` (`coupler_flux_v=0.0`, line 0.08 V -> transfer 0.95, t_pi 152 ns).
- Worked around, NOT fixed: the `partial_swap` macro's coupler pulse was re-baked at 0.08 V
  so the GATE lands on the J zero (line 0.16 V). The park itself is untouched, so idle /
  readout / single-qubit runs still sit on the residual coupling. `interaction_offset` is
  also still 0.0 (unset).
- Side effect of that workaround: `partial_swap` now sits AT the J zero, so as a gate its
  angle is ~0. A real partial swap needs its coupler amplitude picked off a chevron theta
  curve instead.
- Done when: `decouple_offset` is re-measured (a `pair_swap_chevron` coupler scan, or
  `pair_zz_coupler`) and re-parked at the true zero, the `partial_swap` coupler bake is
  re-derived against the NEW park, and TUTORIAL section 12 step 0's premise ("the swap only
  happens while the coupler pulse plays") actually holds on this chip.

## Hardware validation owed (from earlier session notes — verify before acting)
- Ramsey phasor family; parametric-drive family (`_amp` + `_time`); cryoscope Qblox port;
  `qubit_tomography` interleaved noise; XY-Z delay (`qubit_xyz_delay`); readout average mode;
  `qc_n_stark_amp` (+ `register_stark.py`); broadband RESONATOR variant (offline-only on both
  backends); scqo-agent Phase C; `qm-session-hardening` fa1ba06 reverted, QPX1000_4 restart
  owed; the setup-snapshot feature's first real run (compare
  `<device>/setup_snapshots/<hash>/backend_config/state.json` with the setup's file, then
  `scqo restore` + `scqo doctor`); `QbloxBackend.release_instruments` (added 2026-09-04
  with the campaign accept step) — offline-pinned with fakes in
  `scqo-qblox/tests/test_release_instruments.py`, but that a real `Cluster.close()`
  returns inside `_CLUSTER_CLOSE_TIMEOUT_S` and actually frees the four sockets can
  only be seen on hardware: run a campaign at a terminal, leave the prompt open, and
  check that a second process can connect.

**OPX+ / Octave (added 2026-09-11, scqo-qm)** - the whole family has NEVER run on
hardware: six startup audits, the Octave gain+amplitude power solve, the mixer
calibration CLI, the chain-filtered `scqo state --fields`, and the two broadband probes'
Octave LO path. Offline it is covered by 636 tests including 22 against a real tree built
by `quam_builder` (`scripts/make_opxp_fixture.py`), which is as far as offline can go.
The walkthrough is `scqo-qm/OPX-PLUS.md` and its ORDER matters: mixer calibration FIRST,
because every step after it depends on it and an Octave has no MW-FEM-style "it just
works" default. Watch two things in particular - whether any `*_power_dbm` write moved
the Octave gain (it invalidates that RF output's mixer calibration, and on a multiplexed
feedline it moved every qubit on the line), and whether `power_context` in the run record
carries the calibration db's digest.

