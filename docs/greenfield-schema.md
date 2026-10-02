# Greenfield device-description schema

**Status: SHIPPED — this is the live device model of `scqo/`, and of both driver repos.**
Design finalized 2026-07-25. Since 4.0.0 every stored value is addressed by its OWNER — a mode,
a composite, an operation `<composite>.<op>`, a line, or a channel `<line>.<target>` (§2); the
spec of that cutover is `docs/store-by-line-plan.md`, and this document states the resulting
model.
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
    physical.json        # MEASURED facts   {"schema": 4, "values": {owner: {field: float|float[]}}}
    scqo_state.json      # OPERATING knobs+monitors — same shape (top-level key "values" in BOTH)
    history.sqlite       # the context's change-history TRUTH (both stores, `store` column;
                         #   per-context so server aggregation stays a folder copy — never
                         #   deleted, never rebuilt; see scqo/changes.py)
```

The roster describes the **sample** only. Instrument wiring — which port feeds which line, LOs,
diplexers, hybrids, amplifier chains — lives in the setup's vendor config folder, keyed by roster
**line names**. That join key is the only contract between the two sides.

### The core invariant

**Every value is recorded exactly once, under the one OWNER that holds it (§2); every grouping a
human or the AI loop wants is assembled from refs at read time.** The store files nest an owner
under the first name of its address (`xy1.q1` under `xy1`, `q1_q2.iswap` under `q1_q2`, beside
that name's own fields), and that nesting is only the owner's path: no duplicated edges, no
crossing groupings, no declared capabilities.

### Store files

Both value files carry `"schema": 4` and nest a dotted owner under its first name — in
scqo_state.json `"xy1": {"q1": {"pi_amp": ...}}` and `"q1_q2": {"iswap": {"coupler_flux": ...}}`,
in physical.json `"z1": {"distortion_amp": [...], "q1": {"flux_per_phi0": ...}}` (full excerpts
in §8). A key under an owner is a FIELD when its value is a number or a list and a SUB-OWNER when
it is a table; the roster guarantees no entity is named like a field (§7). In memory the store is
flat, `{owner: {field: value}}` with dotted owner names; `history.sqlite` (schema v2) names the
same owners in its `entity` column. A schema-3 file (3.x: rider-minted channel names such as
`q1_xy`) is refused by name — never read, never archived — until the one-time conversion has run
(§10).

### The boundary rule

**An entity (or schema feature) earns roster entry only when standing calibrated content keys on its
name.** Consequences applied throughout: the qubit-resonator dispersive link is a ref + fields on the
resonator, not a composite; there is no `readable_qubit` composite (readability is derived from
wiring); QEC composite kinds are registered only when per-stabilizer calibrated content exists.

---

## 2. Entity model

Every roster entity shares one base: `name`, `kind`, `derived` flag (minted-entity provenance),
`retired` flag (post-cut decommissioning, §7). One **global namespace** across all sections —
names key the stores, history, trends, and `design.toml`. Five thin dataclasses over the base —
modes carry their kind's scalar refs, composites carry `roles` (role → member names) plus
`operations`, each declared operation is an entity of its own (`composite` + `op`), lines carry
only their name, channels carry `kinds`/`target`/`line`/`via` plus the `borrowed`/`broadcast`
flags — deliberately *not* one dataclass with an untyped attrs dict. Declared names are
identifiers (no dot, no `__`, no leading `_`, not `schema`, never a field name); the two
DERIVED kinds of name carry one dot — a channel's `<line>.<target>` (`<line>.<label>` for a pump
or a joint readout, §5) and an operation's `<composite>.<op>` — which is exactly the nesting of
the store files.

All four sections use the same discriminator word: `kind = "<lowercase token>"`.

| section | admission rule | kinds (initial catalog) |
|---|---|---|
| `[modes.*]` | a quantum degree of freedom (has a spectrum) | `transmon`, `flux_transmon`, `fluxonium`, `cavity`, `resonator` |
| `[composites.*]` | a named mode group with **joint** calibrated physics | `qubit_pair`, `cat_system` |
| `[lines.*]` | one physical control path / signal port reaching the sample | (implied `line`) |
| `[channels.*]` | one signal aimed at one target, riding a line (escape hatch only) | `drive`, `readout`, `flux`, `pump` |

### Owners — what every stored value belongs to

| owner | address | example | store |
|---|---|---|---|
| mode | `<mode>` | `q1`, `q1_res`, `q1_q2_c` | physical.json (facts) |
| composite | `<composite>` | `q1_q2` | physical.json (facts) |
| operation | `<composite>.<op>` | `q1_q2.iswap` | scqo_state.json (gate knobs) |
| line | `<line>` | `z1`, `zc12` | scqo_state.json (line knobs), physical.json (line facts) |
| channel | `<line>.<target>` | `xy1.q1`, `fl1.q1`, `z1.q1`, `xy2.q1_q2_c` | scqo_state.json (knobs, monitors), physical.json (facts) |

A value's address is `<owner>.<field>`, the field after the LAST dot: `q1.f_01_hz`,
`z1.idle_flux`, `xy1.q1.pi_amp`, `z1.q1.flux_per_phi0`, `q1_q2.iswap.coupler_flux`. Only a flux
line owns fields today (§6); a drive or readout line is a name that channels ride. A channel is
either DESIGNED (declared in components.toml) or BORROWED (a drive route no entry declares) — §5.

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
- Kind ≠ capability. Whether a mode can be driven / read / flux-biased is decided **only** by
  DESIGNED channel existence (§5, §7) — a borrowed channel derives no operation. Two
  flux_transmons with different wiring are the same kind with different capabilities.

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
  from topology), and exist **only when knob families key on them**. Each declared operation is
  an entity of its own, `<composite>.<op>` (`q1_q2.iswap`), owning that gate's knobs by plain
  name (`q1_q2.iswap.coupler_flux`, §6); the composite itself owns FACTS only. An operation may
  not be named like a field — in the store it is a key beside the composite's facts.
- The composite/ref boundary: a relationship earns a composite only when it carries joint calibrated
  content with no natural single owner (ZZ between qubits: yes; a resonator's g to its one qubit:
  no — the resonator owns it).

---

## 5. `[lines.*]` — physical wires, and the channels they carry

```toml
[lines.fl1]
readout = ["q1", "q2", "q3"]   # rider list: one wire, three channels fl1.q1 fl1.q2 fl1.q3
                               # = frequency multiplexing
[lines.xyz2]
drive = ["q2"]                 # two functions on one metal trace to one target:
flux  = ["q2"]                 # ONE channel xyz2.q2, kinds drive + flux
```

- **One table per physical control path reaching the sample** (an off-package dc bias coil on a
  twisted pair qualifies; how many conductors realize the path is vendor wiring, not roster content).
- **A channel is named by its address, `<line>.<target>`.** Each rider entry declares that the
  line carries that function to that target: `readout` on `fl1` → `fl1.q1` (and mints the
  resonator mode `q1_res`, ref `qubit = q1`), `drive` on `xy1` → `xy1.q1`, `flux` on `z1` →
  `z1.q1`. A rider list with N targets = N channels sharing one wire; this single mechanism
  expresses frequency-multiplexed readout and shared drive or flux lines.
- **One (line, target) is one channel**, whatever functions ride it: a combined drive+flux wire is
  the single channel `xyz2.q2` with `kinds = (drive, flux)`, owning the drive knobs and the
  target's flux transfer facts together. It carries at most ONE function with knobs (a drive and a
  readout to one target on one line is a load error), and the same function twice on it is a load
  error naming both declarations.
- **The line is an owner too.** What a wire has ONCE, however many targets ride it, lives on the
  line (§6): a flux line's `idle_flux` and `flux_delay_s` (one DC offset, one output delay) and its
  `distortion_amp[]`/`distortion_tau_s[]` (one impulse response). What belongs to one target on
  that wire — the transfer function `flux_offset`/`flux_per_phi0` — lives on the channel
  `<line>.<target>`. A drive or readout line owns no fields.
- Rider legality is checked per target against the (channel kind × target kind) table (§7):
  e.g. a flux rider naming a fixed `transmon` is a **load error** — capability by construction,
  not field pruning.

### Designed and borrowed channels

- **Designed** channels are the ones components.toml declares — rider entries and `[channels.*]`
  tables. They alone decide default addressing (§7), the derived single-mode operations (`rx`,
  `readout`, `flux_bias`), the lock signatures, and which channels the doctor expects the
  instrument to realize.
- **Borrowed** channels need no declaration: through every line that carries a designed DRIVE
  channel, every mode with a (drive × kind) row in the table (§7) that the line does not carry by
  design is reachable as `<line>.<mode>` — a coupler through either neighbour's line
  (`xy1.q1_q2_c`, `xy2.q1_q2_c`), a qubit through a foreign line for a microwave-crosstalk study
  (`xy2.q1`). Which line drives the mode is part of the address, so the routes of one coupler
  coexist, each with its own values and history. On 5Q4C (drive lines `xy1`..`xy5`, five qubits,
  four couplers) that is 40 borrowed channels. Retired lines and modes lend and borrow nothing; an
  address a designed channel already holds (another function on a combined wire) stays that
  channel's.
- Only drive is borrowable (`catalog.BORROWABLE`); borrowing flux (flux crosstalk) or readout
  waits for a writer.
- A borrowed channel carries the drive channel's fields minus the kind's `designed_only` set:
  `thermalization_time_s` and `parity_delta_f_hz` are the TARGET's own business (its reset wait,
  its parity monitor) and live only on its designed drive channel — a coupler with no designed
  drive channel has neither.
- A borrowed channel is never a default, never reached by the `q1.<field>` shorthand, never
  derives an operation and is never locked. On the instrument it exists only once the setup's
  vendor config carries an element for it — the route is ADOPTED; until then the backend does not
  realize it (a read finds no value, a write is refused by the driver), and the doctor reports an
  adopted one without ever expecting it. `Roster.channels()` lists the designed channels,
  `Roster.borrowed_channels()` the rest. Declaring the route instead (a rider,
  `[lines.xy1] drive = ["q1", "q1_q2_c"]`) makes it a designed channel with the full drive field
  set — the coupler's default while it is the only one.

### `[channels.*]` — the explicit escape hatch

For irregular signal paths only:

```toml
[channels.q1_q2_c_ro]          # coupler read through a neighbor's resonator -> fl1.q1_q2_c
kind   = "readout"
target = "q1_q2_c"
line   = "fl1"
via    = "q1_res"              # mediator: ANY mode (a resonator, a cat buffer, ...)
```

- Keys per kind (structural-key legality enforced): all have `target` + `line`; `via` is
  readout-only. `target` = one name, a list of mode names, or a composite name.
- **The label names the declaration, not the channel.** A single-target table joins the channel
  `<line>.<target>` (above: `fl1.q1_q2_c`) — the same channel a rider would declare, so one
  function declared both ways is a load error. A pump and a multi-target (joint) readout have no
  single target and are `<line>.<label>`. A multi-target FLUX table (a broadcast coil) is one
  channel per target, `<line>.<t>`, because what a flux channel owns is each target's transfer
  function; the coil's bias is its line's `idle_flux`. A label may not reuse an entity's name.
- `via` default: the unique resonator whose `qubit == target`; **zero or more than one candidate →
  `via` is required** (doctor names the candidates).
- `pump` is **explicit-only** (never rider-derived): AC tones at combination frequencies
  addressing parametric processes, addressed `<line>.<label>`. `target` may be a mode, a composite
  (e.g. a `cat_system`), or a mode list (e.g. `["q1", "q1_res"]` for parametric reset).
- **Multi-target channels never consume the default addressing slot (§7)**: a joint readout
  `<line>.<label>` is one entity with one knob set and N targets; a broadcast coil's per-target
  channels are marked `broadcast` and leave each target's default with its own flux line.
- Target spelling rule: `target = <composite>` means the channel's calibrated content keys on the
  composite; a list is a multi-mode physical path (subsets of a composite's membership stay lists).
  Doctor emits a suggestion only when a list exactly equals a composite's full member set.
- Scalar-or-list is accepted in TOML anywhere a list is legal and normalized to a list at parse; the
  internal model contains no unions (multi-target simply = `len > 1`).

---

## 6. Field machinery

`FieldSpec = {unit, doc, role, portable, design_ok, design_only, shape, paired_with,
design_source}` with `role ∈ {fact, knob, monitor}` — the store router:

| role | store | pushed to vendor | meaning |
|---|---|---|---|
| `fact` | physical.json | never | property of the sample independent of current knob settings |
| `knob` | scqo_state.json | yes | standing set-point realized on the instrument |
| `monitor` | scqo_state.json | never | performance **of** the current knobs; invalidated when they move |

One owner may span both stores (the flux line `z1`: `idle_flux`/`flux_delay_s` knobs +
`distortion_amp`/`distortion_tau_s` facts; the combined-wire channel `xyz2.q2`: drive knobs +
`flux_offset`/`flux_per_phi0` facts). `shape` is `float` or `float[]` (arrays are intra-field
sequences only — waveform samples, distortion taps; **never** entity-aligned positions: a
per-target value lives on that target's channel `<line>.<target>`). Legal `(role, portable,
design_ok)` combinations are enforced at catalog registration; `portable`'s consumer is
cross-setup carry-forward per the placement rule; `design_only` marks design.toml-only vocabulary
(a fabrication constant, never in a store). `paired_with` declares the equal-length partner of a
paired array (`distortion_amp` → `distortion_tau_s`, both on the line). `design_source` on a
channel knob names the (ref-role hop, fact field) that seeds bring-up (`drive_freq_hz ←
target.f_01_hz`, `readout_freq_hz ← via.f_dress0_hz`); the anchor order stays standing state,
else design value, else code default. A line field has none — a line has no target to seed from.

### Naming rules (lint-enforced at catalog registration)

- A dimensioned field's name carries its trailing unit token (`_hz`, `_s`, `_rad`, `_dbm`);
  dimensionless fields (amps, ratios, thresholds, `drag_beta`) carry none.
- **Flux set-points are unit-neutral and source-native** (`idle_flux`, `flux_offset`,
  `flux_per_phi0`, `coupler_flux`; `unit = "source-native"` — volts for an AWG line, amperes
  for a current-source coil, resolved from the line's source type in vendor wiring). These are the
  lint's stated exemption; a unit suffix in a name is otherwise always true.
- **A field name never contains `__` or `.`** — an address splits at its LAST dot (§7), and no
  field is parameterized by another entity: a per-target value lives on that target's own channel
  (a broadcast coil's transfer function for q1 is `bias.q1.flux_per_phi0`), a gate's knob on its
  operation (`q1_q2.iswap.vz_high_rad`). Closed enumerations (`fidelity_g`, `j_high_c_hz`,
  `vz_high_rad`) are plain full names in the catalog — no grammar.
- No field name may appear in two channel kinds (channel- or line-level), nor on both a mode kind
  and a channel kind — asserted at import; this is the invariant behind the `q1.<field>`
  shorthand (§7). Operation fields stay out of it: an operation is only ever addressed in full, so
  `drive_freq_hz` is legally both a drive-channel knob and an operation knob.
- Every waveform array (an operation's `waveform[]`, any `*_waveform[]`) has a mandatory
  time-base companion (`<name>_dt_s`); declared paired arrays (`distortion_amp[]` /
  `distortion_tau_s[]`) are equal-length-checked at store write.

### Field catalogs (initial; assembled per kind by spread + override — no shared-facts tier)

**Modes** (facts unless noted; `design_ok` marked ✎):

| kind | fields |
|---|---|
| `transmon` | `f_01_hz` ✎, `anharmonicity_hz` ✎, `t1_s`, `t2_star_s`, `t2_echo_s`, `n_th`, `parity_rate_hz` |
| `flux_transmon` | transmon set **minus** f_01 designability (`f_01_hz` is bias-dependent → not design-legal) **plus** `ej_sum_hz` ✎, `ej_diff_hz` ✎, `f_q_max_hz` ✎ |
| `fluxonium` | `e_c_hz` ✎, `e_l_hz` ✎, `e_j_hz` ✎, `f_01_hz`, `anharmonicity_hz`, `t1_s`, `t2_star_s`, `t2_echo_s`, `n_th` (`n_jj` is design.toml-only) |
| `cavity` | `f_r_hz` ✎, `kappa_tot_hz` ✎, `n_th` |
| `resonator` | `f_bare_hz` ✎, `f_dress0_hz` ✎, `f_dress1_hz`, `kappa_tot_hz` ✎, `g_hz` ✎, `g_coeff` ✎, `chi_hz`, `n_th`; ref `qubit` |

The transmon base also carries `ec_hz` ✎, `junction_resistance_ohm` and `gap_delta_hz` ✎ —
the fab's normal-state junction resistance and the effective gap that turns it into
`E_JSigma` (Ambegaokar–Baratoff), so `f_q_max` can be predicted before the qubit answers.
`junction_resistance_ohm` is deliberately NOT design-legal: it is the as-FABRICATED value,
and a designed one would merely restate the designed `f_q_max_hz`.

**Composites** (facts only — a gate's knobs live on its operation):

| kind | fields |
|---|---|
| `qubit_pair` | facts `zz_hz`, `j_hz` ✎, `j_high_c_hz`, `j_low_c_hz` (per-leg couplings; legal only on single-coupler pairs) |
| `cat_system` | facts `g2_hz`, `g_bs_hz`, `g_long_hz` (each read at the referencing pump channel's standing amplitude); roles `memory`/`buffer` |

**Operations** — every declared operation `<composite>.<op>` owns `OPERATION_FIELDS` by plain
name. Knobs: flux-activated `coupler_flux`; microwave-activated `drive_freq_hz`, `amp`,
`rel_phase_rad`, `amp_ratio`; generic `duration_s`, `vz_high_rad`, `vz_low_rad`,
`waveform_dt_s` + `waveform[]` (`q1_q2.iswap.coupler_flux`, `q1_q2.iswap.duration_s`). One
monitor: `theta_rad`, a swap-type operation's measured per-application angle
(`q1_q2.partial_swap_040.theta_rad`, written by `qc_n_stark_amp`, read by the chain analysis).

**Channels and lines** — `ChannelKind.fields` belong to the channel `<line>.<target>`,
`ChannelKind.line_fields` to the line it rides (only flux has any), and
`ChannelKind.designed_only` names the channel fields a borrowed channel lacks (†). A line's legal
fields are the `line_fields` of the kinds its designed channels carry; a combined-wire channel has
the fields of both its kinds.

| kind | owner | knobs | monitors | facts |
|---|---|---|---|---|
| `drive` | channel | `drive_freq_hz`, `pi_amp`, `pi_amp_x90`, `drag_beta`, `drag_beta_x90`, `pi_duration_s`, `thermalization_time_s` †, `drive_amp`, `drive_power_dbm` | `parity_delta_f_hz` † | — |
| `readout` | channel | `readout_freq_hz`, `readout_amp`, `readout_power_dbm`, `readout_duration_s`, `readout_integration_s`, `readout_depletion_s`, `readout_rotation_rad`, `readout_threshold`, `readout_rus_threshold` | `fidelity_g`, `fidelity_e`, `pos_g_i`, `pos_g_q`, `pos_e_i`, `pos_e_q` | — |
| `flux` | line | `idle_flux`, `flux_delay_s` | — | `distortion_amp[]`, `distortion_tau_s[]` |
| `flux` | channel | — | — | `flux_offset`, `flux_per_phi0` |
| `pump` | channel | `pump_freq_hz`, `pump_amp`, `pump_phase_rad`, `pump_duration_s` | — | — |

Notes: `drive_amp`+`drive_power_dbm` (and the readout twins) are the settled portable/non-portable
twin pattern — orthogonal planes (dimensionless DAC scale vs absolute level at a declared plane);
doctor warns when a vendor mapping consumes only one of a pair that has both set. One vendor
setting can move knobs across a whole WIRE (a port-level full scale shared by every channel on
it), so a knob write on a channel or a line re-reads the knobs of that owner, of its line and of
every channel riding the line, and records each one that moved as a coupled change naming its
cause (`readout_power_dbm` on the written owner itself, `fl1.q1.readout_power_dbm` on a wire
mate). A coupler's standing/decouple bias **is** its flux LINE's `idle_flux` (`zc12.idle_flux`;
no `coupler_decouple_v`); gate operating points are knobs of the pair's operations
(`q1_q2.iswap.coupler_flux` — survives a coupler shared by two pairs and a pair with two gates).
There is no aggregate `readout_fidelity` (derivable),
no `drive_phase_rad`, no flux-crosstalk family — all deferred until a writer exists (re-adding is
append-only-safe vocabulary). The qutrit surface is deferred the same way, on both sides: the
READOUT monitors (`fidelity_f`, `pos_f_i`, `pos_f_q`) existed while `single_shot_readout_gef`
wrote them and left with it, and the EF DRIVE knobs never landed at all (no backend governs an
EF pulse through SCQO). The names above are what a rebuilt three-state readout should reuse.

---

## 7. Rules

### The (channel kind × target kind) table — single authority

One frozen table (`catalog.DERIVATION`) drives **derivation, rider validation, escape-hatch
validation and borrowing** identically; absence of a row = load error. With the kind catalogs it
is the sole home of the function/op vocabulary. A channel's NAME carries no function (`xyz2.q2` is
one channel with two kinds), so the roster report rows (`report.expansion_rows`) give each channel
its kinds and each line what it carries and what it lends.

| channel kind | target kind | derived operation | declared by |
|---|---|---|---|
| drive | transmon, flux_transmon, fluxonium | `rx` | rider or hatch; BORROWED through any drive line (§5) |
| drive | cavity | `displace` | rider or hatch; borrowed likewise |
| readout | transmon-family, fluxonium, cavity | `readout` | rider (+ mints `<t>_res`; qubit kinds only — see caveat) or hatch |
| flux | flux_transmon, fluxonium | `flux_bias` | rider or hatch |
| pump | any mode / composite / list | *(none — legal, no derived op)* | hatch only |

Single-mode operations are **derived** from this table over a mode's DESIGNED channels (wiring
cannot drift from declared capability; a borrowed channel derives nothing); composite operations
are **declared** (§4). List targets validate per element.

*Rider caveat:* a readout **rider** serves qubit kinds only — a rider cannot name a `via`
mediator, and its minted resonator's `qubit` ref is qubit-typed, so cavity readout (emission
collection) always uses the explicit `[channels.*]` hatch with `via`; the loader's error says
exactly that. The (readout × cavity) row is the hatch's legality, not the rider's.

### Addressing

A value's address is `<owner>.<field>`, split at the LAST dot: `q1.f_01_hz`, `z1.idle_flux`,
`xy1.q1.pi_amp`, `z1.q1.flux_per_phi0`, `q1_q2.iswap.coupler_flux`. A target's DEFAULT channel of
a kind is its one designed, single-target, non-broadcast channel of that kind (zero or several: no
default). Experiments target modes and composites by name and reach knobs through the defaults
(`device.channel(q, "drive")`, the flux line `device.flux_line(q)`), and the `q1.<field>`
shorthand (`Roster.resolve_field`, behind `scqo set` / `scqo suggest` / `Session`) searches, first
hit wins:

1. the mode's own fields (`q1.f_01_hz`);
2. its default channels' fields (`q1.pi_amp` → `xy1.q1`, `q1.flux_per_phi0` → `z1.q1`);
3. the LINE such a channel rides, for line fields — only while q1 is that line's sole designed
   target of the kind (`q1.idle_flux` → `z1.idle_flux`); on a shared line the shorthand refuses and
   names `<line>.idle_flux`, because the value belongs to every target on the wire;
4. the attached resonator (`q1.f_dress0_hz` → `q1_res`).

The field-name uniqueness assertion (§6) keeps steps 1–3 disjoint; the order settles the rest
(`q1.n_th` is the qubit's own, the resonator's is `q1_res.n_th`). A BORROWED channel is
never reached this way: `q1_q2_c.pi_amp` on a coupler with no designed drive line refuses and
lists every route (`xy1.q1_q2_c.pi_amp, xy2.q1_q2_c.pi_amp, …`) — naming the line is the point. A
composite's shorthand reaches only its facts: `q1_q2.coupler_flux` refuses and names
`q1_q2.iswap.coupler_flux`. Extra same-kind designed channels on one target (declared through a
second line) remove its default, and the shorthand then refuses as ambiguous and names them;
multi-target and broadcast channels never occupy the default slot. The qubit CLOSURE
(`Roster.closure`: the mode, its default channels, the flux line such a channel rides, its
resonator) is exactly what the shorthand searches and what `Session.qubit_state` assembles.

### Validation & collision

- **Namespace collision rule**: ONE namespace across all sections, declared and derived names
  alike — a minted resonator, a channel address or an operation colliding with any other name =
  load error naming both origins. No entity and no operation may be named like a field: in the
  nested store files a key under an owner is either one of its fields or a sub-owner. A
  `[channels.<label>]` label names the declaration, not an entity, but may not reuse an entity's
  name either. Every rider-minted entity is stamped with (line, rider, index) provenance for
  error messages; `derived` is rejected as a hand-written key.
- **Compiled legal-field sets**: immediately after roster expansion, each owner's exact finite set
  of legal field names is computed — the kind catalog of a mode or composite, `OPERATION_FIELDS`
  on an operation, the `line_fields` of the kinds a line carries by design, the channel `fields`
  of every kind on a channel (minus `designed_only` on a borrowed one) — tagged with why-legal
  provenance. All later validation of both stores and `design.toml` is set membership — no prefix
  parsing anywhere; every rejection names its exact cause ("operation `cz` is not declared on
  `q1_q2`", "a gate knob lives on an operation — `q1_q2.iswap.coupler_flux`" vs "unknown field").
- **Relation validation is batch-aware** (settled in implementation): a *proposal* (suggestion
  capture, `suggest`) validates shape and finiteness only — its partner may itself only be
  proposed. A *direct write* (`set_values`) validates the batch as a sequence: the waveform→dt
  prerequisite per step (satisfiable by ordering the assignments) and paired-array lengths at
  batch END, so a redone fit changes both partners in one call. `accept` applies per-entity groups
  in catalog field order with a group-level pre-check; the store enforces pair equality at save.
  Two assignment keys resolving to one (entity, field) are refused.

### `design.toml`

Entity-named tables (`[q1]`, `[q1_res]`, `[q1_q2]` — the `design.` prefix dissolved into the
filename; a dotted header would name a dotted owner, nested exactly as in the stores, but no
line, channel or operation field is design-legal), loaded and validated **after** roster
expansion: unknown entity or kind-illegal / non-`design_ok` field = load error. Context-free
vocabulary only (`f_01_hz` design-legal solely on fixed `transmon`; flux-tunables use
`f_q_max_hz` etc.). The doctor's design-vs-measured column joins `design.toml` against
`physical.json` key-for-key. Design values are declarations: hand-editable after the production
cut.

### Append-only production cut

`scqo device freeze` writes `components.lock`: the expanded name set of the declared and DESIGNED
entities with per-name signature (entity class, name, kind — for a channel its sorted kinds and
target(s)) — and **nothing more**. Operations (declared on their composite) and borrowed channels
(declared by nobody) are never locked. Post-cut, every load must produce a **superset by
signature**; provenance, via, roles, and operations are diagnostic or wiring, never compared, so
declaring a new operation on a frozen composite or re-mediating a readout stays legal (the
doctor's vendor/wiring witnesses cover it). A channel's name is its address, so moving a rider to
another line REMOVES a name (`xy1.q1` gone, `xy1b.q1` new) and fails the check, as deleting a
rider entry does. Appending a rider to a frozen line adds names — except when it adds a second
function to an existing (line, target) channel, which changes that channel's kinds and fails the
check too. Post-cut evolution is always an append. Retirement is `retired = true`, never
deletion, so store keys and history keep resolving. Freezing happens once: a second `freeze`
refuses rather than blessing whatever drifted.

### Doctor witnesses (vendor cross-checks)

Keyed by line names against the vendor wiring annotation: (a) every channel resolves within its
line's declared vendor port set; (b) same-kind channels sharing a line = same RF output, distinct
IFs; (c) a combined line's port set covers all its channel kinds (MW + LF at the bias-tee);
(d) a fixed `transmon` with a vendor z element = error; (e) `qubit_pair` roles vs design targets
(primary) and vs live f01 (informational); (f) design-vs-measured key-for-key. The inventory
witness expects every DESIGNED owner with knobs — channels, flux lines, operations — to be
realized by the backend; an adopted borrowed channel is reported, never expected.

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
operations = ["iswap"]         # each is an entity: q1_q2.iswap owns the gate's
                               # knobs (coupler_flux, duration_s, ...)

# ---- lines: one table per physical control path; riders declare channels ----
[lines.fl1]
readout = ["q1", "q2", "q3"]   # ONE feedline -> fl1.q1 fl1.q2 fl1.q3 (+ resonator
                               # modes); three readout_freq_hz = multiplexing
[lines.xy1]
drive = ["q1"]                 # -> xy1.q1
[lines.z1]
flux = ["q1"]                  # -> z1.q1 (transfer function); the line z1
                               # itself owns the bias: z1.idle_flux
[lines.xyz2]                   # ONE combined wire to q2 carrying BOTH
drive = ["q2"]                 # functions: ONE channel xyz2.q2 (kinds drive +
flux  = ["q2"]                 # flux); q2's bias is xyz2.idle_flux

[lines.xy3]
drive = ["q3"]                 # -> xy3.q3

[lines.zc12]
flux = ["q1_q2_c"]             # -> zc12.q1_q2_c; zc12.idle_flux IS the
                               # pair's decouple point

# Coupler two-tone THROUGH q1's wire needs NO declaration: xy1.q1_q2_c is a
# BORROWED channel (so are xyz2.q1_q2_c and xy3.q1_q2_c). Appending a rider,
#   [lines.xy1]  drive = ["q1", "q1_q2_c"]
# would make that route DESIGNED instead (the coupler's default drive channel).
# Coupler readout through q1's resonator = the explicit escape hatch
# (joins the channel fl1.q1_q2_c):
#   [channels.q1_q2_c_ro]
#   kind = "readout"; target = "q1_q2_c"; line = "fl1"; via = "q1_res"

# Derived operations (Roster.operations, from the DESIGNED channels; never declared):
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
f_dress0_hz = 5.93e9                # validation runs after roster expansion
g_hz   = 8.0e7

[q2_res]
f_dress0_hz = 6.02e9

[q3_res]
f_dress0_hz = 6.10e9

[q1_q2]
j_hz = 1.0e7
```

### Store excerpts

```jsonc
// physical.json — measured
{ "schema": 4, "values": {
    "q1":     { "f_01_hz": 5.136e9, "ej_sum_hz": 1.78e10, "f_q_max_hz": 5.139e9 },
    "q1_res": { "f_dress0_hz": 5.9359e9, "kappa_tot_hz": 3.24e6 },
    "z1":     { "distortion_amp": [0.021, -0.004], "distortion_tau_s": [1.2e-7, 2.9e-6],
                "q1": { "flux_offset": 0.0134, "flux_per_phi0": 0.969 } },
    "q1_q2":  { "zz_hz": -1.2e4 } } }

// scqo_state.json — operated
{ "schema": 4, "values": {
    "xy1":   { "q1": { "drive_freq_hz": 5.136e9, "pi_amp": 0.209, "pi_amp_x90": 0.104,
                       "drag_beta": -1.0, "drag_beta_x90": -1.9,
                       "thermalization_time_s": 3.7e-4 } },
    "fl1":   { "q1": { "readout_freq_hz": 5.934e9, "fidelity_g": 0.96, "fidelity_e": 0.94 } },
    "z1":    { "idle_flux": 0.118 },
    "zc12":  { "idle_flux": 0.081 },
    "q1_q2": { "iswap": { "coupler_flux": 0.0 } } } }
```

The flux line `z1` holds its own taps beside its channel `q1`, and the pair `q1_q2` its facts in
one file and its operation `iswap` in the other. Once the coupler route through q1's line is
adopted and calibrated, `"xy1"` also holds `"q1_q2_c": {"drive_freq_hz": ..., "pi_amp": ...}`
beside `"q1"`; a route through `xyz2` would sit under `"xyz2"` — both coexist, each with its own
history.

### Resulting store keys

| store | owners | fields |
|---|---|---|
| physical.json | q1, q2, q3, q1_q2_c | transmon facts (+ ej/f_q_max on flux_transmons) |
| | q1_res, q2_res, q3_res | f_bare_hz, f_dress0_hz, f_dress1_hz, kappa_tot_hz, g_hz, g_coeff, chi_hz, n_th |
| | z1, xyz2, zc12 (flux lines) | distortion_amp[], distortion_tau_s[] |
| | z1.q1, xyz2.q2, zc12.q1_q2_c (flux channels) | flux_offset, flux_per_phi0 |
| | q1_q2 | zz_hz, j_hz, j_high_c_hz, j_low_c_hz |
| scqo_state.json | xy1.q1, xyz2.q2, xy3.q3 | drive knobs + the parity_delta_f_hz monitor |
| | fl1.q1, fl1.q2, fl1.q3 | readout knobs + monitors |
| | z1, xyz2, zc12 (flux lines) | idle_flux, flux_delay_s |
| | q1_q2.iswap | coupler_flux, drive_freq_hz, amp, …, waveform_dt_s, waveform[] + the theta_rad monitor |
| | xy1.q2, xy1.q3, xy1.q1_q2_c, xyz2.q1, xyz2.q3, xyz2.q1_q2_c, xy3.q1, xy3.q2, xy3.q1_q2_c (borrowed; values once adopted) | drive knobs minus thermalization_time_s |

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

**Preserved**: qubit-anchored instance names and the `_res/_ro/_xy/_z` suffixes (4.0.0 retired
the channel suffixes, below; `_res` stays); declared `high`/`low` (now design-nominal); flat
per-(cooldown, setup) stores (flat in memory; the files nest by owner since 4.0.0) + per-context
change history (sidecars at the cutover, `history.sqlite` since the change-history cutover) +
suggest→accept flow; the TOML/JSON writer rule; vendor wiring outside the roster; the placement
rule's portable/twin doctrine; governed readout discriminator fields; terminology bans
("port", "element", "macro" for chip-side concepts).

**Cutover economics** (when scheduled): one combo release, fresh-start stores (schema 3);
`state_sync="pull"` reseeds every pushed knob from the vendor config; monitors are refreshed by one
single-shot run per qubit; trends either restart at the cutover epoch or the viewer ships a one-time
old→new key map. Both drivers' fieldmaps and every experiment's `update()` re-target in the same
release.

**4.0.0 — store by line** (spec: `docs/store-by-line-plan.md`; §1–§8 above state the result).
Stored values moved from rider-minted channel names to their OWNERS: a channel is
`<line>.<target>` (`q1_xy.pi_amp` → `xy1.q1.pi_amp`, `q1_ro.*` → `fl1.q1.*`); a flux wire's bias,
delay and taps moved to the LINE (`q1_z.idle_flux` → `z1.idle_flux`, `q1_z.distortion_amp` →
`z1.distortion_amp`) and the target's transfer function to the channel (`q1_z.flux_per_phi0` →
`z1.q1.flux_per_phi0`); gate knobs moved to operations (`q1_q2.iswap_coupler_flux` →
`q1_q2.iswap.coupler_flux`); the `<field>__<target>` grammar is gone (`coil.flux_per_phi0__q1` →
`bias.q1.flux_per_phi0`); drive channels may be BORROWED without declaration. components.toml did
not change (still `schema = 3`), so the old→new map is computed from it (`scqo/v3_names.py`).
Unlike the greenfield cutover the stores are CONVERTED, not restarted:
`python scripts/convert_store_v4.py <data_root>` lists what moves, `--apply` rewrites each
context's `physical.json` / `scqo_state.json` (schema 3 → 4) and `history.sqlite` (v1 → v2, row
count unchanged) after `*.v3.bak` copies, and it refuses while any run or campaign holds a pending
suggestion (`--reject-pending` rejects them all). Values are unchanged — only their addresses
move; the script ships with 4.0.0 only. Immutable run data keeps its 3.x names forever (run
folders, `dataset.nc`, setup snapshots, campaign folders); where 4.0.0 has to USE such names it
maps them through `scqo/v3_names.py`, which is permanent: a pre-4.0.0 `dataset.nc`
(`scqo_schema = 1`) re-fits through the map, and `scqo restore` of a pre-4.0.0 snapshot converts
its store files on the way in. The CLI keeps `scqo accept --entity`, and history and suggestion
rows keep the key `entity` — "owner" is this document's word.

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
  qubit, a coupler), whose flux LINE is swept instead of the target's own. Such runs stay
  RECORD-ONLY. The old per-class category narrowing has no successor and needed none.
- **Extra same-kind channels need no naming convention** (4.0.0): a channel's name is its address,
  so a second drive route for a cross-drive study is a borrowed `<line>.<target>` (no declaration)
  or a rider on another line — the line in the name tells them apart. Two same-kind channels on
  one (line, target) stay a load error.
- **`readout_fidelity` stays deleted**: `single_shot_readout` proposes `fidelity_g`/`fidelity_e`;
  the aggregate is derived by whoever displays it.
- **Doctor witnesses and report rows are model-side, renderer-free** (`checks.py`, `report.py`), so
  the CLI is a table printer and the same data serves tests, the viewer, and an AI loop. Vendor
  inventory and the line→port annotation are inputs — the drivers supply them at the cutover, and
  every witness degrades to a clear WARN without them.

## 12. Open decisions

- Kind-name spellings on registration by demand (`cat_system` vs alternatives; QEC kind names).
- Whether doctor warns when a `design.toml` value is edited after measurements exist for that field
  (lightweight provenance for the one hand-edited file that has none).
- Derived capabilities (catalog key `capabilities`), the `maturity` field, and the contrib
  entry-point merge return to the registry at the namespace cutover (tracked in
  `scqo/model/experiments/__init__.py`).
