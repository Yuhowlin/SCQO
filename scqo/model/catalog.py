"""Kind catalogs — WHAT kinds of entities exist and WHICH fields each owns.

The greenfield replacement for :mod:`scqo.categories` (docs/greenfield-schema.md
sections 2, 6, 7). Three kind families share one field machinery:

* **mode kinds** — quantum degrees of freedom (``transmon`` .. ``resonator``);
* **composite kinds** — named mode groups with joint physics (``qubit_pair``);
* **channel kinds** — one signal on a line (``drive``/``readout``/``flux``/``pump``).

The old two-category-slots-per-name routing is gone: every entity has exactly
ONE kind, and a field's store follows its ROLE — ``fact`` -> physical.json,
``knob`` -> scqo_state.json (pushed to the vendor, in declaration order),
``monitor`` -> scqo_state.json (never pushed).

The frozen ``DERIVATION`` table is the single (channel kind x target kind)
authority: it names the derived single-mode operation, the rider suffix, and —
by the ABSENCE of a row — what is illegal on both birth paths (a flux channel
targeting a fixed transmon is a load error, not a pruned field). Composite
operations are never here: they are DECLARED in the roster and carry the
``OP_KNOBS`` per-operation field family.

Catalogs are assembled per kind by spreading a base dict and overriding — each
kind owns its complete field list; there is no shared-facts inheritance tier.
``_validate_catalog()`` enforces the registration lints at import time.
"""

from __future__ import annotations

from dataclasses import dataclass, field as _field
from typing import Literal

Role = Literal["fact", "knob", "monitor"]
Shape = Literal["float", "float[]"]

#: Mode kinds a qubit-shaped role (composite high/low/coupler, drive/readout
#: targets) may bind. "Qubit-like" = has internal levels you drive and read.
QUBIT_LIKE: tuple[str, ...] = ("transmon", "flux_transmon", "fluxonium")

#: Mode kinds with a flux degree of freedom (legal flux-channel targets).
FLUX_TUNABLE: tuple[str, ...] = ("flux_transmon", "fluxonium")


@dataclass(frozen=True)
class FieldSpec:
    """Descriptor of one neutral field a kind owns."""

    unit: str
    doc: str
    #: The store router: fact -> physical.json; knob -> scqo_state.json, pushed
    #: to the vendor in declaration order; monitor -> scqo_state.json, never
    #: pushed (measured performance OF the current knobs — invalidated when
    #: they move; a fact is true of the sample independent of knob settings).
    role: Role
    #: False = only meaningful within ONE backend's current chain configuration
    #: (dimensionless amplitudes, acquisition-frame quantities, source-native
    #: flux). Metadata for `scqo state --fields` and the AI loop; the era guard
    #: blocks cross-setup accepts at apply time.
    portable: bool = True
    #: True = legal as an as-designed target in design.toml. Facts only, and
    #: only context-free quantities (f_q_max_hz yes; bias-dependent f_01_hz on
    #: a flux-tunable no).
    design_ok: bool = False
    #: True = design.toml vocabulary ONLY — never legal in either store
    #: (fabrication constants like the fluxonium junction count).
    design_only: bool = False
    #: float[] fields are intra-field sequences (waveform samples, filter
    #: taps) — NEVER entity-aligned positions (per-target values on a
    #: multi-target channel use the __<target> field grammar instead).
    shape: Shape = "float"
    #: Name of a same-catalog float[] partner that must stay equal-length
    #: (declared on ONE side of the pair; checked at store write).
    paired_with: str | None = None
    #: Bring-up seed for channel knobs: (ref role to hop, fact field there).
    #: Role None = the channel's target itself. Anchor order stays standing
    #: state, else design, else code default.
    design_source: tuple[str | None, str] | None = None


@dataclass(frozen=True)
class RoleSpec:
    """One member role a composite kind declares."""

    #: Mode kinds (or composite kinds, for hierarchy) the role accepts.
    allows: tuple[str, ...]
    #: True = the roster value may be a list of names (two-mode DTC coupler);
    #: scalars are normalized to one-element lists at parse either way.
    list_ok: bool = False
    optional: bool = False


@dataclass(frozen=True)
class ModeKind:
    """Schema of one quantum degree of freedom."""

    doc: str
    fields: dict[str, FieldSpec]
    #: role -> allowed mode kinds (the resonator's ``qubit`` ref — the ONE
    #: place the dispersive attachment is recorded; everything else derives it).
    refs: dict[str, tuple[str, ...]] = _field(default_factory=dict)


@dataclass(frozen=True)
class CompositeKind:
    """Schema of one named mode group with joint calibrated physics.

    Roles, member typing, and doctor checks belong to the KIND, not the
    section (``qubit_pair``'s high/low design-nominal ordering check does not
    exist for a cat system). ``operations`` declared in the roster instantiate
    the :data:`OP_KNOBS` family as full field names on the entity.
    """

    doc: str
    fields: dict[str, FieldSpec]
    roles: dict[str, RoleSpec]


@dataclass(frozen=True)
class ChannelKind:
    """Schema of one signal function riding a line."""

    doc: str
    fields: dict[str, FieldSpec]
    #: Suffix the rider expansion appends to the target name (frozen: store
    #: keys depend on it). None = never rider-derived (pump is explicit-only).
    rider_suffix: str | None
    #: True = the channel may carry a ``via`` mediator ref (readout only).
    via_ok: bool = False
    #: True = a readout rider also mints the target's ``<t>_res`` resonator
    #: mode (suppressed when an explicit ``via`` is given).
    mints_resonator: bool = False
    #: True = any mode/composite/list target is legal with no derived op
    #: (pump); otherwise legality comes from the DERIVATION table.
    any_target: bool = False


# --------------------------------------------------------------------- modes

_TRANSMON_BASE: dict[str, FieldSpec] = {
    "f_01_hz": FieldSpec(
        "Hz", "Qubit 0->1 frequency at the operating point (the drive_freq_hz "
              "knob is its instrument twin; one fit writes both).",
        role="fact", design_ok=True),
    "anharmonicity_hz": FieldSpec(
        "Hz", "Anharmonicity f_12 - f_01.", role="fact", design_ok=True),
    "t1_s": FieldSpec("s", "Energy-relaxation time T1.", role="fact"),
    "t2_star_s": FieldSpec("s", "Ramsey dephasing time T2*.", role="fact"),
    "t2_echo_s": FieldSpec("s", "Hahn-echo coherence time T2_echo.", role="fact"),
    "n_th": FieldSpec(
        "", "Thermal excited-state population at idle (0..1).", role="fact"),
}

MODES: dict[str, ModeKind] = {
    "transmon": ModeKind(
        doc="Fixed-frequency transmon. f_01 is context-free here, so it is the "
            "design target (flux-tunable kinds design f_q_max_hz instead).",
        fields=dict(_TRANSMON_BASE),
    ),
    "flux_transmon": ModeKind(
        doc="Flux-tunable transmon: transmon set plus the flux arch. The "
            "volts-to-flux transfer function is NOT here — it lives on the "
            "flux CHANNEL (per-target, wiring-dependent).",
        fields={
            **_TRANSMON_BASE,
            # f_01 is bias-dependent on a tunable qubit -> not a design target.
            "f_01_hz": FieldSpec(
                "Hz", "Qubit 0->1 frequency at the CURRENT idle bias (the "
                      "context-free design target is f_q_max_hz).",
                role="fact"),
            "ej_sum_hz": FieldSpec(
                "Hz", "Total Josephson energy EJ1+EJ2 (arch fit).",
                role="fact", design_ok=True),
            "ej_diff_hz": FieldSpec(
                "Hz", "Josephson energy asymmetry |EJ1-EJ2| (SQUID d; arch "
                      "bottom).", role="fact", design_ok=True),
            "f_q_max_hz": FieldSpec(
                "Hz", "Qubit 0->1 frequency at the sweet spot (arch top).",
                role="fact", design_ok=True),
        },
    ),
    "fluxonium": ModeKind(
        doc="Fluxonium: EC/EL/EJ Hamiltonian vocabulary; operating sweet spot "
            "at half a flux quantum where f_01 is MINIMUM — transmon arch "
            "estimators do not apply.",
        fields={
            "e_c_hz": FieldSpec("Hz", "Charging energy EC/h.",
                                role="fact", design_ok=True),
            "e_l_hz": FieldSpec("Hz", "Inductive energy EL/h.",
                                role="fact", design_ok=True),
            "e_j_hz": FieldSpec("Hz", "Josephson energy EJ/h.",
                                role="fact", design_ok=True),
            "n_jj": FieldSpec(
                "", "Junction count of the array inductor — fabrication "
                    "constant, design.toml ONLY.",
                role="fact", design_ok=True, design_only=True),
            "f_01_hz": FieldSpec(
                "Hz", "Qubit 0->1 frequency at the operating bias.", role="fact"),
            "anharmonicity_hz": FieldSpec(
                "Hz", "Anharmonicity f_12 - f_01 (GHz-scale, positive).",
                role="fact"),
            "t1_s": _TRANSMON_BASE["t1_s"],
            "t2_star_s": _TRANSMON_BASE["t2_star_s"],
            "t2_echo_s": _TRANSMON_BASE["t2_echo_s"],
            "n_th": _TRANSMON_BASE["n_th"],
        },
    ),
    "cavity": ModeKind(
        doc="Linear cavity mode declared in its own right (cat-qubit memory, "
            "buffer). Drives on it derive displace, never rx.",
        fields={
            "f_r_hz": FieldSpec("Hz", "Mode frequency.", role="fact",
                                design_ok=True),
            "kappa_tot_hz": FieldSpec(
                "Hz", "Total linewidth (engineered for a buffer).",
                role="fact", design_ok=True),
            "n_th": FieldSpec("", "Thermal occupation at idle (0..1).",
                              role="fact"),
        },
    ),
    "resonator": ModeKind(
        doc="Readout resonator — usually MINTED by a readout rider with its "
            "qubit ref; declared explicitly only when no rider mints it. The "
            "qubit ref is the single record of the dispersive attachment and "
            "what makes g_hz/chi_hz unambiguous.",
        fields={
            "f_r_hz": FieldSpec(
                "Hz", "Dressed resonator frequency (spectroscopy dip).",
                role="fact", design_ok=True),
            "f_r0_hz": FieldSpec(
                "Hz", "Bare resonator frequency (dispersive fit).", role="fact"),
            "kappa_tot_hz": FieldSpec(
                "Hz", "Total linewidth (power-Lorentzian FWHM); the "
                      "coupling/intrinsic split waits for a complex-S21 "
                      "estimator.", role="fact", design_ok=True),
            "g_hz": FieldSpec(
                "Hz", "Qubit-resonator coupling from the dispersive fit.",
                role="fact", design_ok=True),
            "chi_hz": FieldSpec(
                "Hz", "Dispersive shift per excitation.", role="fact"),
            "n_th": FieldSpec("", "Thermal occupation at idle (0..1).",
                              role="fact"),
        },
        refs={"qubit": QUBIT_LIKE},
    ),
}

# ---------------------------------------------------------------- composites

MODES_ANY: tuple[str, ...] = tuple(MODES)

COMPOSITES: dict[str, CompositeKind] = {
    "qubit_pair": CompositeKind(
        doc="Two-qubit interaction + gate surface. high/low are DESIGN-NOMINAL "
            "(bound against design.toml frequency targets; live-f01 inversion "
            "is an informational doctor warning only — ordering legitimately "
            "crosses during tuning). Which qubit moves in a gate is a "
            "per-operation vendor fact, never roster topology. A coupler's "
            "STANDING bias is its own flux channel's idle_flux; per-gate "
            "operating points live here via the OP_KNOBS family.",
        fields={
            "zz_hz": FieldSpec(
                "Hz", "Signed residual ZZ at the standing operating point.",
                role="fact"),
            "j_hz": FieldSpec(
                "Hz", "Exchange coupling J.", role="fact", design_ok=True),
            "j_high_c_hz": FieldSpec(
                "Hz", "high-qubit <-> coupler coupling (single-coupler pairs "
                      "only — validated at roster load).", role="fact"),
            "j_low_c_hz": FieldSpec(
                "Hz", "low-qubit <-> coupler coupling (single-coupler pairs "
                      "only).", role="fact"),
        },
        roles={
            "high": RoleSpec(allows=QUBIT_LIKE),
            "low": RoleSpec(allows=QUBIT_LIKE),
            "coupler": RoleSpec(allows=QUBIT_LIKE, list_ok=True, optional=True),
        },
    ),
    "cat_system": CompositeKind(
        doc="Dissipative cat qubit: high-Q memory + lossy buffer. The "
            "pump-activated rates are read at the referencing pump channel's "
            "standing amplitude.",
        fields={
            "g2_hz": FieldSpec("Hz", "Two-photon exchange rate g2.", role="fact"),
            "g_bs_hz": FieldSpec("Hz", "Beam-splitter (reset) rate.", role="fact"),
            "g_long_hz": FieldSpec("Hz", "Longitudinal (readout) rate.",
                                   role="fact"),
        },
        roles={
            "memory": RoleSpec(allows=("cavity",)),
            "buffer": RoleSpec(allows=("cavity",)),
        },
    ),
}

#: Per-operation knob family on composites: a declared operation <op>
#: instantiates these as full field names <op>_<suffix> on the entity
#: (compiled into the legal-field set — full names, never prefix matching).
OP_KNOBS: dict[str, FieldSpec] = {
    "coupler_flux": FieldSpec(
        "source-native", "Flux-activated gate operating point on the coupler "
                         "line (source-native unit).",
        role="knob", portable=False),
    "drive_freq_hz": FieldSpec(
        "Hz", "Microwave-activated gate drive frequency.", role="knob"),
    "amp": FieldSpec("", "Overall gate drive amplitude.", role="knob",
                     portable=False),
    "rel_phase_rad": FieldSpec(
        "rad", "Relative phase between the two emission channels.", role="knob"),
    "amp_ratio": FieldSpec("", "Amplitude ratio between the two emission "
                               "channels.", role="knob"),
    "duration_s": FieldSpec("s", "Gate duration.", role="knob"),
    "vz_high_rad": FieldSpec("rad", "Virtual-Z correction on the high qubit.",
                             role="knob"),
    "vz_low_rad": FieldSpec("rad", "Virtual-Z correction on the low qubit.",
                            role="knob"),
    "waveform": FieldSpec(
        "", "Optimized pulse samples (dimensionless DAC fraction).",
        role="knob", portable=False, shape="float[]"),
    "waveform_dt_s": FieldSpec(
        "s", "Sample period of <op>_waveform — mandatory companion so the "
             "array is physically interpretable without vendor context.",
        role="knob"),
}

# ------------------------------------------------------------------ channels

CHANNELS: dict[str, ChannelKind] = {
    "drive": ChannelKind(
        doc="Charge/microwave drive aimed at one mode.",
        rider_suffix="_xy",
        fields={
            "drive_freq_hz": FieldSpec(
                "Hz", "Drive frequency (operating CHOICE; the fact is the "
                      "target's f_01_hz).",
                role="knob", design_source=(None, "f_01_hz")),
            "pi_amp": FieldSpec(
                "", "Calibrated pi (x180) pulse amplitude.",
                role="knob", portable=False),
            "drag_beta": FieldSpec(
                "", "DRAG coefficient (QM DragCosine coefficient, Qblox "
                    "rxy.beta).", role="knob", portable=False),
            "pi_duration_s": FieldSpec(
                "s", "Calibrated pi pulse length.", role="knob"),
            # drive_power_dbm AFTER drive_amp: pushes go in declaration order
            # and the absolute power must win (amp is the chain solve's residual).
            "drive_amp": FieldSpec(
                "", "Saturation (spec) drive amplitude — the drive_power_dbm "
                    "chain solve's residual.", role="knob", portable=False),
            "drive_power_dbm": FieldSpec(
                "dBm", "Absolute saturation drive power at the instrument "
                       "port; setting it re-solves the drive chain and moves "
                       "drive_amp as a COUPLED change.", role="knob"),
        },
    ),
    "readout": ChannelKind(
        doc="Dispersive (or emission-collection) readout of one target, "
            "possibly frequency-multiplexed with others on the same line.",
        rider_suffix="_ro",
        via_ok=True,
        mints_resonator=True,
        fields={
            "readout_freq_hz": FieldSpec(
                "Hz", "Readout tone / demodulation frequency (operating "
                      "CHOICE; the fact is the resonator's f_r_hz).",
                role="knob", design_source=("via", "f_r_hz")),
            "readout_amp": FieldSpec(
                "", "Readout pulse amplitude (zero for emission-collection "
                    "readout).", role="knob", portable=False),
            "readout_power_dbm": FieldSpec(
                "dBm", "Absolute readout power at the output port; re-solves "
                       "the chain, moving readout_amp as a COUPLED change.",
                role="knob"),
            # duration BEFORE integration: growing both in one command must
            # lengthen the pulse before the window that fits inside it.
            "readout_duration_s": FieldSpec(
                "s", "Readout pulse length (positive multiple of 4 ns; "
                     "drivers refuse off-grid).", role="knob"),
            "readout_integration_s": FieldSpec(
                "s", "Integration window; contract <= readout_duration_s.",
                role="knob"),
            "readout_rotation_rad": FieldSpec(
                "rad", "Demod rotation landing |0>->|1> on I (acquisition "
                       "frame).", role="knob", portable=False),
            "readout_threshold": FieldSpec(
                "", "g/e discrimination threshold on rotated I (acquisition "
                    "frame).", role="knob", portable=False),
            "readout_rus_threshold": FieldSpec(
                "", "Repeat-until-success exit threshold (QM-only; Unrealized "
                    "on Qblox).", role="knob", portable=False),
            "fidelity_g": FieldSpec(
                "", "P(assign g | prepared g) — monitor of the CURRENT "
                    "readout settings.", role="monitor"),
            "fidelity_e": FieldSpec(
                "", "P(assign e | prepared e).", role="monitor"),
            "pos_g_i": FieldSpec("", "|g> blob center, I (acquisition frame).",
                                 role="monitor", portable=False),
            "pos_g_q": FieldSpec("", "|g> blob center, Q.", role="monitor",
                                 portable=False),
            "pos_e_i": FieldSpec("", "|e> blob center, I.", role="monitor",
                                 portable=False),
            "pos_e_q": FieldSpec("", "|e> blob center, Q.", role="monitor",
                                 portable=False),
        },
    ),
    "flux": ChannelKind(
        doc="Standing flux bias of one target. Facts here are the DAC-plane "
            "transfer function — per-TARGET (each target on a shared line has "
            "its own), which is why they key on the channel, not the line.",
        rider_suffix="_z",
        fields={
            "idle_flux": FieldSpec(
                "source-native",
                "Standing bias set-point in the flux source's native unit "
                "(volts for an AWG line, amperes for a coil). A coupler's "
                "decouple point IS this knob on its own flux channel.",
                role="knob", portable=False),
            "flux_offset": FieldSpec(
                "source-native", "Sweet-spot offset of the transfer function "
                                 "flux/Phi0 = (x - flux_offset)/flux_per_phi0.",
                role="fact", portable=False),
            "flux_per_phi0": FieldSpec(
                "source-native", "Source units per flux quantum on this "
                                 "channel.", role="fact", portable=False),
            "distortion_amp": FieldSpec(
                "", "Flux-transient predistortion tap amplitudes (measured "
                    "cryo-wiring physics, per cooldown).",
                role="fact", portable=False, shape="float[]",
                paired_with="distortion_tau_s"),
            "distortion_tau_s": FieldSpec(
                "s", "Flux-transient tap time constants (equal length with "
                     "distortion_amp).",
                role="fact", portable=False, shape="float[]"),
        },
    ),
    "pump": ChannelKind(
        doc="AC tone at a combination frequency activating a parametric "
            "process. Explicit-only (no rider, no suffix); target may be a "
            "mode, a composite, or a mode list.",
        rider_suffix=None,
        any_target=True,
        fields={
            "pump_freq_hz": FieldSpec("Hz", "Pump carrier frequency.",
                                      role="knob"),
            "pump_amp": FieldSpec("", "Pump amplitude.", role="knob",
                                  portable=False),
            "pump_phase_rad": FieldSpec("rad", "Pump phase.", role="knob"),
            "pump_duration_s": FieldSpec(
                "s", "Pump duration (continuous stabilization pumps omit it).",
                role="knob"),
        },
    ),
}

# ------------------------------------------- the (channel x mode) authority

#: The frozen legality-and-derivation table: (channel kind, target mode kind)
#: -> the derived single-mode operation. ABSENCE of a row = that channel kind
#: may not target that mode kind, enforced identically for riders and explicit
#: [channels.*] tables (list targets validate per element). Pump bypasses the
#: table via ChannelKind.any_target (legal everywhere, derives nothing).
DERIVATION: dict[tuple[str, str], str] = {
    ("drive", "transmon"): "rx",
    ("drive", "flux_transmon"): "rx",
    ("drive", "fluxonium"): "rx",
    ("drive", "cavity"): "displace",
    ("readout", "transmon"): "readout",
    ("readout", "flux_transmon"): "readout",
    ("readout", "fluxonium"): "readout",
    ("readout", "cavity"): "readout",
    ("flux", "flux_transmon"): "flux_bias",
    ("flux", "fluxonium"): "flux_bias",
}


def derived_op(channel_kind: str, target_kind: str) -> str | None:
    """The single-mode operation a channel of this kind derives on its target.

    Returns None for legal-with-no-derived-op (pump); raises KeyError when the
    combination is illegal — callers turn that into the load error.
    """
    if CHANNELS[channel_kind].any_target:
        return None
    return DERIVATION[(channel_kind, target_kind)]


def op_knob_fields(operation: str) -> dict[str, FieldSpec]:
    """The full-name knob fields a declared composite operation instantiates
    (e.g. 'cz' -> {'cz_coupler_flux': ..., 'cz_waveform': ...})."""
    return {f"{operation}_{suffix}": spec for suffix, spec in OP_KNOBS.items()}


# ------------------------------------------------------------------- lints

#: unit -> mandatory trailing name token for dimensioned fields. Source-native
#: flux fields and dimensionless fields are the stated exemptions.
_UNIT_TOKENS = {"Hz": "_hz", "s": "_s", "rad": "_rad", "dBm": "_dbm", "V": "_v"}


def _validate_fields(owner: str, fields: dict[str, FieldSpec]) -> None:
    for name, spec in fields.items():
        token = _UNIT_TOKENS.get(spec.unit)
        if token is not None:
            assert name.endswith(token), (
                f"{owner}.{name}: unit {spec.unit!r} requires the name to end "
                f"with {token!r} — a unit suffix in a name must always be true")
        assert not (spec.design_ok and spec.role != "fact"), (
            f"{owner}.{name}: design_ok is a fact-only concept")
        assert not (spec.design_only and not spec.design_ok), (
            f"{owner}.{name}: design_only implies design_ok")
        if spec.paired_with is not None:
            partner = fields.get(spec.paired_with)
            assert partner is not None and partner.shape == "float[]", (
                f"{owner}.{name}: paired_with must name a same-catalog "
                f"float[] field")
            assert spec.shape == "float[]", (
                f"{owner}.{name}: only float[] fields pair")
        if spec.shape == "float[]" and name.endswith("_waveform"):
            assert f"{name}_dt_s" in fields, (
                f"{owner}.{name}: a waveform array needs its _dt_s companion")


def _validate_catalog() -> None:
    """Import-time invariants: a broken catalog must never load."""
    for kind, spec in MODES.items():
        _validate_fields(f"modes.{kind}", spec.fields)
        for role, allows in spec.refs.items():
            assert all(a in MODES for a in allows), (
                f"modes.{kind}.{role}: unknown mode kind in {allows}")
    for kind, spec in COMPOSITES.items():
        _validate_fields(f"composites.{kind}", spec.fields)
        assert spec.roles, f"composites.{kind}: a composite needs member roles"
        for role, rs in spec.roles.items():
            assert all(a in MODES or a in COMPOSITES for a in rs.allows), (
                f"composites.{kind}.{role}: unknown kind in {rs.allows}")
    for kind, spec in CHANNELS.items():
        _validate_fields(f"channels.{kind}", spec.fields)
        assert spec.via_ok is (kind == "readout"), (
            f"channels.{kind}: via is a readout-only structural key")
        assert not (spec.any_target and spec.rider_suffix is not None), (
            f"channels.{kind}: any-target channels are explicit-only")
    _validate_fields("op_knobs", OP_KNOBS)

    # Facts never carry cross-setup semantics beyond their per-context store;
    # design_source is a channel-knob seeding concept.
    for owner, fields in (
            [(f"modes.{k}", s.fields) for k, s in MODES.items()]
            + [(f"composites.{k}", s.fields) for k, s in COMPOSITES.items()]):
        for name, spec in fields.items():
            assert spec.design_source is None, (
                f"{owner}.{name}: design_source belongs to channel knobs")

    # The addressing invariant behind `scqo set q1.pi_amp`: no field name in
    # two channel-kind catalogs, and none shared between mode and channel
    # catalogs (a mode fact and a channel field with one name would make the
    # qubit-closure resolution ambiguous).
    seen: dict[str, str] = {}
    for kind, spec in CHANNELS.items():
        for name in spec.fields:
            assert name not in seen, (
                f"field {name!r} appears in channel kinds {seen[name]} and "
                f"{kind} — default addressing needs catalog-wide uniqueness")
            seen[name] = kind
    mode_fields = {n for s in MODES.values() for n in s.fields}
    overlap = mode_fields & set(seen)
    assert not overlap, (
        f"fields {sorted(overlap)} exist on both a mode and a channel kind")

    # Every DERIVATION row must reference declared kinds; pump has no rows.
    for (ch, mk) in DERIVATION:
        assert ch in CHANNELS and not CHANNELS[ch].any_target, (
            f"DERIVATION row for non-table channel kind {ch!r}")
        assert mk in MODES, f"DERIVATION row for unknown mode kind {mk!r}"
    assert FLUX_TUNABLE == tuple(
        mk for mk in MODES if ("flux", mk) in DERIVATION), (
        "FLUX_TUNABLE must mirror the flux rows of DERIVATION")


_validate_catalog()
