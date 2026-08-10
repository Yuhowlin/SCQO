# Greenfield device-description schema

**Status: SHIPPED — this is the live device model of `scqo/`, and of both driver repos.**
Design finalized 2026-07-25.
Built in reviewed phases — catalogs, roster loader, `design.toml`, stores, device layer, testing
substrate, session, all 21 experiments, and the operational surfaces (lock, doctor witnesses,
report data). Each phase was gated by an adversarial review; where a review changed a design
decision, this document was amended in the same commit, so it stays the single source of truth.
**Mandate:** straightforward, clean code first; no backward compatibility (one fresh-start cutover release when implemented).
**Provenance:** designed interactively, then hardened by three multi-agent passes — (1) three independent
design drafts + two adversarial attacks; (2) expressiveness stress test against four published devices
(MIT fluxonium-transmon-fluxonium pair, PRX 13.031035; RIKEN/Toshiba double-transmon-coupler CZ,
PRX 14.041050; Tencent parametric qubit-resonator reset, Nat. Commun. 12 5924; Alice&Bob dissipative
cat qubit, arXiv:2307.06617); (3) a cleanliness audit (consistency / implementation-cost / YAGNI lenses,
27/27 deduplicated proposals adopted). This document is the single source of truth for the design;
when it conflicts with memory of the conversation that produced it, this document wins.

This schema **replaces** the v0.10–v0.12 component model (one-name/two-category-slots,
`[components.*]`, ReadableTransmon knob lump) when implemented. See §10 for the settled decisions it
formally relitigates.

---

## 1. Overview: designed → measured → operated

One value shape, three files, scope encoded by placement; format encodes the writer
(TOML = hand-edited, JSON = machine-written):

```
<data_root>/<device>/
  components.toml        # TOPOLOGY — hand-edited; freezes (append-only) at the production cut
  design.toml            # DATASHEET — hand-edited declarations; stays editable after the cut
  cooldowns.toml
  <cooldown>/<setup>/scqo/
    physical.json        # MEASURED facts   {schema, values: {entity: {field: float|float[]}}}
    scqo_state.json      # OPERATING knobs+monitors — same shape (top-level key "values" in BOTH)
    history.sqlite       # the context's change-history TRUTH (both stores, `store` column;
                         #   per-context so server aggregation stays a folder copy — never
                         #   deleted, never rebuilt; see scqo/changes.py)
```

The roster describes the **sample** only. Instrument wiring — which port feeds which line, LOs,
diplexers, hybrids, amplifier chains — lives in the setup's vendor config folder, keyed by roster
**line names**. That join key is the only contract between the two sides.

### The core invariant

**Every fact is recorded exactly once, flat, under the one name that owns it; every grouping a human
or the AI loop wants is assembled from refs at read time.** No nesting, no duplicated edges, no
declared capabilities.

### The boundary rule

**An entity (or schema feature) earns roster entry only when standing calibrated content keys on its
name.** Consequences applied throughout: the qubit-resonator dispersive link is a ref + fields on the
resonator, not a composite; there is no `readable_qubit` composite (readability is derived from
wiring); QEC composite kinds are registered only when per-stabilizer calibrated content exists.

---

## 2. Entity model

Every roster entity shares one base: `name`, `kind`, `derived` flag (minted-entity provenance),
`retired` flag (post-cut decommissioning, §7). One **global flat namespace** across all sections —
names key the stores, history, trends, and `design.toml`. Four thin dataclasses over the base —
modes carry their kind's scalar refs, composites carry `roles` (role → member names) plus
`operations`, channels carry `target`/`line`/`via` — deliberately *not* one dataclass with an
untyped attrs dict.

All four sections use the same discriminator word: `kind = "<lowercase token>"`.

| section | admission rule | kinds (initial catalog) |
|---|---|---|
| `[modes.*]` | a quantum degree of freedom (has a spectrum) | `transmon`, `flux_transmon`, `fluxonium`, `cavity`, `resonator` |
| `[composites.*]` | a named mode group with **joint** calibrated physics | `qubit_pair`, `cat_system` |
| `[lines.*]` | one physical control path / signal port reaching the sample | (implied `line`) |
| `[channels.*]` | one signal aimed at one target, riding a line (escape hatch only) | `drive`, `readout`, `flux`, `pump` |

Kind catalogs are extensible **by demand**: a new kind is registered the day a real device calibrates
content for it, never speculatively. Word choices are deliberate and guarded: *mode* (not "element" —
Qblox/QUAM collision; not "component" — that is the genus of all entities), *composite* (standard QI
"composite system"; not "port"/"channel" for chip structures — vendor collisions), *operations* (not
"macros" — QUAM collision).

---

## 3. `[modes.*]` — quantum degrees of freedom

```toml
[modes.q1]
kind = "flux_transmon"
```

- Qubits **and couplers** are modes (a tunable coupler is an ordinary `flux_transmon`; "coupler" is a
  composite's *ref*, not a kind). Cavity/buffer modes (cat chips) are declared explicitly.
- **Readout resonators are usually not declared** — they are *minted* by readout riders (§5) as
  `<target>_res`, kind `resonator`, with ref `qubit = <target>`. An explicit `kind = "resonator"`
  declaration is legal for a resonator no rider mints (it must then supply its `qubit` ref itself).
- Kind ≠ capability. Whether a mode can be driven / read / flux-biased is decided **only** by channel
  existence (§5, §7). Two flux_transmons with different wiring are the same kind with different
  capabilities.

---

## 4. `[composites.*]` — named mode groups with joint physics

```toml
[composites.q1_q2]
kind       = "qubit_pair"
high       = "q1"            # roles + typing + checks belong to the KIND, not the section
low        = "q2"
coupler    = "q1_q2_c"       # name or list (two-mode couplers, e.g. a DTC)
operations = ["iswap"]       # DECLARED: gates and protocols that carry knob families
```

- Each **kind** declares its role vocabulary, member-kind typing, field catalog, and doctor checks.
  For `qubit_pair`: roles `high`/`low` (any qubit-like mode — transmon-family or fluxonium),
  optional `coupler` (name or list).
- **`high`/`low` are design-nominal**: bound at declaration against `design.toml` frequency targets
  (the other hand-authored layer), so the frozen topology file never encodes a mutable measurement.
  Doctor's primary check compares roles to design targets; a live-f01 inversion is an
  *informational warning* only (ordering legitimately crosses during tuning).
- Refs may point at modes **or other composites**; the ref graph must be a **DAG** (cycle = load
  error). Membership is **non-exclusive** (q2 sits in q1_q2 and q2_q3; a coupler may serve two
  pairs). This is what a surface-code hierarchy uses: stabilizer composites referencing modes, a
  logical composite referencing stabilizers — the *mechanism* ships now; QEC kinds are registered
  when per-entity calibrated content exists (boundary rule).
- **`operations` are declared, never derived** (vendor gate macros and protocols are not derivable
  from topology), and exist **only when knob families key on them**. Per-operation knobs live on the
  composite as full field names prefixed by the operation name (§6).
- The composite/ref boundary: a relationship earns a composite only when it carries joint calibrated
  content with no natural single owner (ZZ between qubits: yes; a resonator's g to its one qubit:
  no — the resonator owns it).

---

## 5. `[lines.*]` — physical wires, and the channels they mint

```toml
[lines.fl1]
readout = ["q1", "q2", "q3"]   # rider list: one wire, three channels = freq multiplexing

[lines.xyz2]
drive = ["q2"]                 # two functions on one metal trace:
flux  = ["q2"]                 # q2_xy and q2_z share the wire
```

- **One table per physical control path reaching the sample** (an off-package dc bias coil on a
  twisted pair qualifies; how many conductors realize the path is vendor wiring, not roster content).
- **Rider lists mint channels** via the frozen suffix map — `readout → <t>_ro` (also mints
  `<t>_res` when the target has no explicit `via`), `drive → <t>_xy`, `flux → <t>_z`. A rider list
  with N targets = N channels sharing one wire; this single mechanism expresses frequency-multiplexed
  readout, shared drive lines, combined drive+flux wires, and borrowing a neighbor's wire
  (`drive = ["q1", "q1_q2_c"]`).
- Rider legality is checked per target against the (channel kind × target kind) table (§7):
  e.g. a flux rider naming a fixed `transmon` is a **load error** — capability by construction,
  not field pruning.

### `[channels.*]` — the explicit escape hatch

For irregular signal paths only:

```toml
[channels.q1_q2_c_ro]          # coupler read through a neighbor's resonator
kind   = "readout"
target = "q1_q2_c"
line   = "fl1"
via    = "q1_res"              # mediator: ANY mode (a resonator, a cat buffer, ...)
```

- Keys per kind (structural-key legality enforced): all have `target` + `line`; `via` is
  readout-only. `target` = one name, a list of mode names, or a composite name.
- `via` default: the unique resonator whose `qubit == target`; **zero or more than one candidate →
  `via` is required** (doctor names the candidates).
- `pump` is **explicit-only** (never rider-derived, no suffix): AC tones at combination frequencies
  addressing parametric processes. `target` may be a mode, a composite (e.g. a `cat_system`), or a
  mode list (e.g. `["q1", "q1_res"]` for parametric reset).
- **Multi-target channels** (broadcast flux coil, joint two-mode readout) are one entity with one
  knob set and N targets; they never consume the default addressing slot (§7). Per-target values on
  such a channel use the `__<target>` field grammar (§6).
- Target spelling rule: `target = <composite>` means the channel's calibrated content keys on the
  composite; a list is a multi-mode physical path (subsets of a composite's membership stay lists).
  Doctor emits a suggestion only when a list exactly equals a composite's full member set.
- Scalar-or-list is accepted in TOML anywhere a list is legal and normalized to a list at parse; the
  internal model contains no unions (multi-target simply = `len > 1`).

---

## 6. Field machinery

`FieldSpec = {unit, doc, role, portable, design_ok, shape, paired_with, design_source}` with
`role ∈ {fact, knob, monitor}` — the store router:

| role | store | pushed to vendor | meaning |
|---|---|---|---|
| `fact` | physical.json | never | property of the sample independent of current knob settings |
| `knob` | scqo_state.json | yes | standing set-point realized on the instrument |
| `monitor` | scqo_state.json | never | performance **of** the current knobs; invalidated when they move |

One entity name may span both stores (`q1_z`: `flux_offset`/`flux_per_phi0` facts + `idle_flux`
knob). `shape` is `float` or `float[]` (arrays are intra-field sequences only — waveform samples,
distortion taps; **never** entity-aligned positions). Legal `(role, portable, design_ok)`
combinations are enforced at catalog registration; `portable`'s consumer is cross-setup
carry-forward per the placement rule. `paired_with` declares the equal-length partner of a paired
array (on multi-target channels the compiled `__<target>` instances re-point it per target).
`design_source` on a channel knob names the (ref-role hop, fact field) that seeds bring-up
(`drive_freq_hz ← target.f_01_hz`, `readout_freq_hz ← via.f_r_hz`); the anchor order stays
standing state, else design value, else code default.

### Naming rules (lint-enforced at catalog registration)

- A dimensioned field's name carries its trailing unit token (`_hz`, `_s`, `_rad`, `_dbm`);
  dimensionless fields (amps, ratios, thresholds, `drag_beta`) carry none.
- **Flux set-points are unit-neutral and source-native** (`idle_flux`, `flux_offset`,
  `flux_per_phi0`, `iswap_coupler_flux`; `unit = "source-native"` — volts for an AWG line, amperes
  for a current-source coil, resolved from the line's source type in vendor wiring). These are the
  lint's stated exemption; a unit suffix in a name is otherwise always true.
- **`__<param>` grammar** is reserved for fields parameterized by another *roster entity*, validated
  against a live entity set — sole current use: per-target values on multi-target channels
  (`flux_per_phi0__q1` on a broadcast coil). Closed enumerations (`fidelity_g`, `j_high_c_hz`,
  `cz_vz_high_rad`) are plain full names in the catalog — no grammar.
- No field name may appear in two channel-kind catalogs, nor collide across catalogs sharing the
  `q1.<field>` addressing sugar — asserted at import (this is the invariant behind default
  addressing).
- Every `*_waveform[]` has a mandatory time-base companion (`*_waveform_dt_s`); declared paired
  arrays (`distortion_amp[]` / `distortion_tau_s[]`) are equal-length-checked at store write.

### Field catalogs (initial; assembled per kind by spread + override — no shared-facts tier)

**Modes** (facts unless noted; `design_ok` marked ✎):

| kind | fields |
|---|---|
| `transmon` | `f_01_hz` ✎, `anharmonicity_hz` ✎, `t1_s`, `t2_star_s`, `t2_echo_s`, `n_th` |
| `flux_transmon` | transmon set **minus** f_01 designability (`f_01_hz` is bias-dependent → not design-legal) **plus** `ej_sum_hz` ✎, `ej_diff_hz` ✎, `f_q_max_hz` ✎ |
| `fluxonium` | `e_c_hz` ✎, `e_l_hz` ✎, `e_j_hz` ✎, `f_01_hz`, `anharmonicity_hz`, `t1_s`, `t2_star_s`, `t2_echo_s`, `n_th` (`n_jj` is design.toml-only) |
| `cavity` | `f_r_hz` ✎, `kappa_tot_hz` ✎, `n_th` |
| `resonator` | `f_r_hz` ✎, `f_r0_hz`, `kappa_tot_hz` ✎, `g_hz` ✎, `chi_hz`, `n_th`; ref `qubit` |

**Composites**:

| kind | fields |
|---|---|
| `qubit_pair` | facts `zz_hz`, `j_hz` ✎, `j_high_c_hz`, `j_low_c_hz` (per-leg couplings; legal only on single-coupler pairs); per-operation knob families below |
| `cat_system` | facts `g2_hz`, `g_bs_hz`, `g_long_hz` (each read at the referencing pump channel's standing amplitude); roles `memory`/`buffer` |

Per-operation knob families on composites (full names, `<op>` = a declared operation):
flux-activated `<op>_coupler_flux`; microwave-activated `<op>_drive_freq_hz`, `<op>_amp`,
`<op>_rel_phase_rad`, `<op>_amp_ratio`; generic `<op>_duration_s`, `<op>_vz_high_rad`,
`<op>_vz_low_rad`, `<op>_waveform[]` + `<op>_waveform_dt_s`.

**Channels**:

| kind | knobs | monitors | facts |
|---|---|---|---|
| `drive` | `drive_freq_hz`, `drive_amp`, `drive_power_dbm`, `pi_amp`, `pi_amp_x90`, `drag_beta`, `drag_beta_x90`, `pi_duration_s`, `thermalization_time_s` | `parity_delta_f_hz` | — |
| `readout` | `readout_freq_hz`, `readout_amp`, `readout_power_dbm`, `readout_duration_s`, `readout_integration_s`, `readout_rotation_rad`, `readout_threshold`, `readout_rus_threshold` | `fidelity_g`, `fidelity_e`, `fidelity_f`, `pos_g_i`, `pos_g_q`, `pos_e_i`, `pos_e_q`, `pos_f_i`, `pos_f_q` | — |
| `flux` | `idle_flux`, `flux_delay_s` | — | `flux_offset`, `flux_per_phi0`, `distortion_amp[]`, `distortion_tau_s[]` |
| `pump` | `pump_freq_hz`, `pump_amp`, `pump_phase_rad`, `pump_duration_s` | — | — |

Notes: `drive_amp`+`drive_power_dbm` (and the readout twins) are the settled portable/non-portable
twin pattern — orthogonal planes (dimensionless DAC scale vs absolute level at a declared plane);
doctor warns when a vendor mapping consumes only one of a pair that has both set. A coupler's
standing/decouple bias **is** its flux channel's `idle_flux` (no `coupler_decouple_v`); gate
operating points are per-operation composite knobs (survives a coupler shared by two pairs and a
pair with two gates). There is no aggregate `readout_fidelity` (derivable),
no `drive_phase_rad`, no flux-crosstalk family — all deferred until a writer exists (re-adding is
append-only-safe vocabulary). The qutrit READOUT monitors (`fidelity_f`, `pos_f_i`, `pos_f_q`)
landed on exactly those terms once `single_shot_readout_gef` became their writer; the EF DRIVE
knobs stay deferred (no backend governs an EF pulse through SCQO yet).

---

## 7. Rules

### The (channel kind × target kind) table — single authority

One frozen table drives **derivation, rider validation, and escape-hatch validation** identically;
absence of a row = load error. It also carries the rider-suffix and knob-catalog columns, making it
the sole home of the function/suffix/op vocabulary (doctor always prints both spellings:
`q1_z (flux)`).

| channel kind | target kind | derived operation | rider suffix |
|---|---|---|---|
| drive | transmon, flux_transmon, fluxonium | `rx` | `_xy` |
| drive | cavity | `displace` | `_xy` |
| readout | transmon-family, fluxonium, cavity | `readout` | `_ro` (+ mints `<t>_res`; qubit kinds only — see caveat) |
| flux | flux_transmon, fluxonium | `flux_bias` | `_z` |
| pump | any mode / composite / list | *(none — legal, no derived op)* | *(explicit-only)* |

Single-mode operations are **derived** from this table (wiring cannot drift from declared
capability); composite operations are **declared** (§4). List targets validate per element.

*Rider caveat:* a readout **rider** serves qubit kinds only — a rider cannot name a `via`
mediator, and its minted resonator's `qubit` ref is qubit-typed, so cavity readout (emission
collection) always uses the explicit `[channels.*]` hatch with `via`; the loader's error says
exactly that. The (readout × cavity) row is the hatch's legality, not the rider's.

### Addressing

Exactly one channel per (target, kind) resolves default addressing — experiments target modes and
composites by name; `scqo set q1.pi_amp` routes through the unique owner in q1's channel closure
(provable via the cross-catalog field-name uniqueness assertion). Extra same-kind channels are
declared with explicit names and addressed explicitly; multi-target channels never occupy the
default slot.

### Validation & collision

- **Namespace collision rule**: any derived name (channel *or* minted resonator) colliding with any
  declared name, in any section = load error. Every minted entity is stamped with
  (line, rider, index) provenance for error messages; `derived` is rejected as a hand-written key.
- **Compiled legal-field sets**: immediately after roster expansion, each entity's exact finite set
  of legal field names is computed (kind catalog + full-name per-operation knobs + validated
  `__<param>` instances), tagged with why-legal provenance. All later validation of both stores and
  `design.toml` is set membership — no prefix parsing anywhere; every rejection names its exact
  cause ("operation cz not declared on this composite" vs "unknown field").
- **Relation validation is batch-aware** (settled in implementation): a *proposal* (suggestion
  capture, `suggest`) validates shape and finiteness only — its partner may itself only be
  proposed. A *direct write* (`set_values`) validates the batch as a sequence: the waveform→dt
  prerequisite per step (satisfiable by ordering the assignments) and paired-array lengths at
  batch END, so a redone fit changes both partners in one call. `accept` applies per-entity groups
  in catalog field order with a group-level pre-check; the store enforces pair equality at save.
  Two assignment keys resolving to one (entity, field) are refused.

### `design.toml`

Entity-named tables (`[q1]`, `[q1_res]`, `[q1_q2]` — the `design.` prefix dissolved into the
filename), loaded and validated **after** roster expansion: unknown entity or kind-illegal /
non-`design_ok` field = load error. Context-free vocabulary only (`f_01_hz` design-legal solely on
fixed `transmon`; flux-tunables use `f_q_max_hz` etc.). The doctor's design-vs-measured column joins
`design.toml` against `physical.json` key-for-key. Design values are declarations: hand-editable
after the production cut.

### Append-only production cut

`scqo device freeze` writes `components.lock`: the expanded name set with per-name signature
(entity class, name, kind, target(s) for channels) — and **nothing more**. Post-cut, every load
must produce a **superset by signature**; provenance, line, via, roles, and operations are
diagnostic or wiring, never compared, so declaring a new operation on a frozen composite or
rewiring a rider to another line stays legal (the doctor's vendor/wiring witnesses cover the
rewire). Deleting a rider entry deletes its minted names and fails the check; appending a rider to
a frozen line only adds names — post-cut evolution is always an append. Retirement is
`retired = true`, never deletion, so store keys and history keep resolving. Freezing happens once:
a second `freeze` refuses rather than blessing whatever drifted.

### Doctor witnesses (vendor cross-checks)

Keyed by line names against the vendor wiring annotation: (a) every channel resolves within its
line's declared vendor port set; (b) same-kind channels sharing a line = same RF output, distinct
IFs; (c) a combined line's port set covers all its channel kinds (MW + LF at the bias-tee);
(d) a fixed `transmon` with a vendor z element = error; (e) `qubit_pair` roles vs design targets
(primary) and vs live f01 (informational); (f) design-vs-measured key-for-key.

---

## 8. Worked example

Flux-tunable q1 + q2 with a tunable-coupler pair, fixed-frequency q3; three readouts
frequency-multiplexed on one feedline; q2's drive and flux share one combined wire.

### `components.toml`

```toml
# The SAMPLE's topology. Which instrument port feeds which line lives in the
# setup's vendor config folder, keyed by the line names declared here.
schema = 3

# ---- modes: the quantum degrees of freedom ----------------------------------
[modes.q1]
kind = "flux_transmon"

[modes.q2]
kind = "flux_transmon"

[modes.q3]                     # fixed frequency: a flux rider naming q3
kind = "transmon"              # below would be a load error

[modes.q1_q2_c]                # the coupler: an ordinary flux-tunable mode;
kind = "flux_transmon"         # "coupler" is the composite's ref, not a kind

# (q1_res/q2_res/q3_res are minted by the readout riders, ref qubit=...)

# ---- composites: named mode groups with joint physics -----------------------
[composites.q1_q2]
kind       = "qubit_pair"      # roles high/low/coupler + design-nominal check
high       = "q1"              # are THIS KIND's rules, not the section's
low        = "q2"
coupler    = "q1_q2_c"         # a list for a two-mode coupler (DTC case)
operations = ["iswap"]         # knobs key on THIS entity: iswap_coupler_flux, ...

# ---- lines: one table per physical control path; riders mint channels -------
[lines.fl1]
readout = ["q1", "q2", "q3"]   # ONE feedline -> q1_ro q2_ro q3_ro (+ resonator
                               # modes); three readout_freq_hz = multiplexing
[lines.xy1]
drive = ["q1"]                 # -> q1_xy
[lines.z1]
flux = ["q1"]                  # -> q1_z

[lines.xyz2]                   # ONE combined wire to q2 carrying BOTH
drive = ["q2"]                 # functions: q2_xy and q2_z are two channels
flux  = ["q2"]                 # riding the same metal

[lines.xy3]
drive = ["q3"]                 # -> q3_xy

[lines.zc12]
flux = ["q1_q2_c"]             # -> q1_q2_c_z; its idle_flux IS the
                               # composite's decouple point

# Coupler two-tone THROUGH q1's wire = append a rider (adds q1_q2_c_xy):
#   [lines.xy1]  drive = ["q1", "q1_q2_c"]
# Coupler readout through q1's resonator = the explicit escape hatch:
#   [channels.q1_q2_c_ro]
#   kind = "readout"; target = "q1_q2_c"; line = "fl1"; via = "q1_res"

# Derived operations (printed by `scqo device`, never declared):
#   q1, q2: rx, readout, flux_bias | q3: rx, readout | q1_q2_c: flux_bias
```

### `design.toml`

```toml
# As-designed targets (declarations, not measurements). Validated against the
# EXPANDED roster; shape mirrors physical.json key-for-key.
schema = 1

[q1]
f_q_max_hz       = 5.15e9      # flux_transmon: context-free targets only
anharmonicity_hz = -2.0e8

[q2]
f_q_max_hz       = 4.90e9
anharmonicity_hz = -2.0e8

[q3]
f_01_hz          = 4.70e9      # design-legal ONLY on kind transmon
anharmonicity_hz = -2.1e8

[q1_q2_c]
f_q_max_hz = 7.5e9

[q1_res]                       # design on DERIVED entities is fine —
f_r_hz = 5.93e9                # validation runs after roster expansion
g_hz   = 8.0e7

[q2_res]
f_r_hz = 6.02e9

[q3_res]
f_r_hz = 6.10e9

[q1_q2]
j_hz = 1.0e7
```

### Store excerpts

```jsonc
// physical.json — measured
{ "schema": 3, "values": {
    "q1":     { "f_01_hz": 5.136e9, "ej_sum_hz": 1.78e10, "f_q_max_hz": 5.139e9 },
    "q1_res": { "f_r_hz": 5.9359e9, "kappa_tot_hz": 3.24e6 },
    "q1_z":   { "flux_offset": 0.0134, "flux_per_phi0": 0.969 },
    "q1_q2":  { "zz_hz": -1.2e4 } } }

// scqo_state.json — operated
{ "schema": 3, "values": {
    "q1_xy":     { "drive_freq_hz": 5.136e9, "pi_amp": 0.209, "pi_amp_x90": 0.104,
                   "drag_beta": -1.0, "drag_beta_x90": -1.9,
                   "thermalization_time_s": 3.7e-4 },
    "q1_ro":     { "readout_freq_hz": 5.934e9, "fidelity_g": 0.96, "fidelity_e": 0.94 },
    "q1_z":      { "idle_flux": 0.118 },
    "q1_q2_c_z": { "idle_flux": 0.081 },
    "q1_q2":     { "iswap_coupler_flux": 0.0 } } }
```

### Resulting store keys

| store | keys | fields |
|---|---|---|
| physical.json | q1, q2, q3, q1_q2_c | transmon facts (+ ej/f_q_max on flux_transmons) |
| | q1_res, q2_res, q3_res | f_r_hz, f_r0_hz, kappa_tot_hz, g_hz, chi_hz |
| | q1_z, q2_z, q1_q2_c_z | flux_offset, flux_per_phi0 |
| | q1_q2 | zz_hz, j_hz |
| scqo_state.json | q1_xy, q2_xy, q3_xy | drive knobs |
| | q1_ro, q2_ro, q3_ro | readout knobs + monitors |
| | q1_z, q2_z, q1_q2_c_z | idle_flux |
| | q1_q2 | iswap_coupler_flux |

---

## 9. Expressiveness record

Verified expressible (stress-test workflows): the 5Q flux-tunable QCQ chip; frequency-multiplexed
readout; combined drive+flux wires; coupler driven/read through a neighbor; fixed-frequency qubits
(no flux anything, by construction); the MIT FTF fluxonium pair (fluxonium kind, microwave-activated
CZ knob family, broadcast 3-target bias coil, per-leg J facts); the RIKEN/Toshiba DTC (two-mode
coupler list, joint two-mode readout, array-valued CZ waveform + distortion taps); the Tencent
parametric reset (pump channel on the z wire targeting `[q1, q1_res]`); the Alice&Bob cat qubit
(cavity + buffer modes, `cat_system` composite, three pump channels, emission readout `via = buffer`
with no probe tone); surface-code hierarchy (composite→composite DAG; kinds deferred per the
boundary rule); post-cut evolution as pure appends.

---

## 10. Relationship to current SCQO (for the cutover release notes)

**Formally relitigated settled decisions** (authorized by the greenfield mandate):
1. One-name/two-category-slots → one kind per entity + per-field `role` (fact/knob/monitor) routing.
2. Instrument knobs on the qubit's ReadableTransmon → re-homed onto channels.
3. `coupler_decouple_v` on the pair → the coupler flux channel's `idle_flux`; `coupler_interaction_v`
   → per-operation composite knobs.
4. Single-qubit `operations` declared on components → derived from wiring (composites still declare).
5. Design values in-roster (`[components.*.design]`) → the separate `design.toml`.
6. Flat `[components.*]` → four typed sections; `[transmons]` intermediate form → `[modes]`.
7. `members`-on-satellite topology → line rider lists + channel refs; `resonator=` → `via=`.
8. Field spellings: `drive_freq`/`readout_freq` → `_hz`-suffixed; `idle_flux_v`/`v_offset_v`/
   `v_per_phi0_v` → source-native `idle_flux`/`flux_offset`/`flux_per_phi0`; scqo_state top-level
   `"config"` → `"values"`.

**Preserved**: qubit-anchored instance names and the `_res/_ro/_xy/_z` suffixes; declared
`high`/`low` (now design-nominal); flat per-(cooldown, setup) stores + per-context change
history (sidecars at the cutover, `history.sqlite` since the change-history cutover) +
suggest→accept flow; the TOML/JSON writer rule; vendor wiring outside the roster; the placement
rule's portable/twin doctrine; governed readout discriminator fields; terminology bans
("port", "element", "macro" for chip-side concepts).

**Cutover economics** (when scheduled): one combo release, fresh-start stores (schema 3);
`state_sync="pull"` reseeds every pushed knob from the vendor config; monitors are refreshed by one
single-shot run per qubit; trends either restart at the cutover epoch or the viewer ships a one-time
old→new key map. Both drivers' fieldmaps and every experiment's `update()` re-target in the same
release.

## 11. Decisions the implementation settled

Beyond the amendments folded into §2–§7 above, the build settled these (each with tests):

- **Bring-up seeds take candidate facts.** `design_source` may name several facts, first declared
  wins: `drive_freq_hz` seeds from `f_01_hz` on a fixed transmon and falls through to `f_q_max_hz`
  on a flux-tunable (park at the sweet spot). `seed_anchor` resolves the structural half (entity +
  candidates) for the field catalog; `seed_value` picks against the datasheet.
- **Per-experiment pre-probe gating** is an `Experiment.validate_targets(roster, targets)` hook,
  used by `pair_zz_coupler` to require a tracked coupler with a flux channel — the successor to the
  deleted `coupler_bias` operation. The session runs it inside the machine-readable gate.
- **A foreign `flux_component` is kind-agnostic**: any entity with a default flux channel (another
  qubit's z, a coupler's z). Such runs stay RECORD-ONLY. The old per-class category narrowing has
  no successor and needed none.
- **`readout_fidelity` stays deleted**: `single_shot_readout` proposes `fidelity_g`/`fidelity_e`;
  the aggregate is derived by whoever displays it.
- **Doctor witnesses and report rows are model-side, renderer-free** (`checks.py`, `report.py`), so
  the CLI is a table printer and the same data serves tests, the viewer, and an AI loop. Vendor
  inventory and the line→port annotation are inputs — the drivers supply them at the cutover, and
  every witness degrades to a clear WARN without them.

## 12. Open decisions

- Kind-name spellings on registration by demand (`cat_system` vs alternatives; QEC kind names).
- Naming convention for deliberate extra same-kind channels (e.g. a second drive channel for a
  cross-drive study) — reserve `<target>_<suffix><n>`?
- Whether doctor warns when a `design.toml` value is edited after measurements exist for that field
  (lightweight provenance for the one hand-edited file that has none).
- Derived capability tags, the `maturity` field, and the contrib entry-point merge return to the
  registry at the namespace cutover (tracked in `scqo/model/experiments/__init__.py`).
