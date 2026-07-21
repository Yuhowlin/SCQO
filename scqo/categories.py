"""The category catalog — WHAT kinds of components exist and WHICH fields each owns.

A device is a set of named COMPONENTS (declared per device in ``components.toml``,
loaded by :mod:`scqo.roster`). Each name binds at most one PHYSICAL category
("what it is" — fields land in physical.json) and at most one INSTRUMENT category
("how we drive it" — fields land in scqo_state.json). A field's store is the SIDE
of the category that declares it; routing is by ``Roster.resolve(name, field)``.

Categories replace the old global FIELDS/PHYSICAL_FIELDS tables. The unique
field-name invariant is per-NAME now (the two categories bound to one name must
not declare the same field), enforced at roster load — the same field name may
legitimately exist on different categories.

Phase 1 is the single-qubit world: transmons, resonators, and the control-line
interaction terms. Pair categories (Coupling / TransmonPair / CouplerTransmon)
arrive with the first two-qubit chip.
"""

from __future__ import annotations

from dataclasses import dataclass, field as _field
from typing import Literal

Side = Literal["physical", "instrument"]
Kind = Literal["bare", "interaction", "single", "composite"]


@dataclass(frozen=True)
class FieldSpec:
    """Descriptor of one neutral field a category owns."""

    unit: str
    doc: str
    #: True = calibration knob, pushed to the vendor instrument on write/load.
    #: False = measured value — recorded into state + history, NEVER pushed
    #: (the instrument has no such knob; drivers need no code for these).
    push: bool
    #: False = only meaningful within ONE backend's current output-chain
    #: configuration (the dimensionless amplitudes) — never carry it to the other
    #: backend. Every portable=False field must either NAME a portable twin
    #: (readout_amp -> readout_power_dbm, the absolute quantity that wins on
    #: conflict) or have its untracked chain scale CATALOGUED in the driver's
    #: VENDOR_ONLY (pi_amp -> the drive-port scale knobs; no twin exists).
    #: Metadata for `scqo state --fields` and the AI loop; no code path enforces
    #: it (the era guard already blocks cross-setup accepts at apply time).
    portable: bool = True
    #: Instrument-side fields only: physical categories the component's PHYSICAL
    #: slot must match for this field to exist on it (e.g. idle_flux_v requires
    #: FluxTunableTransmon). Empty = present on every realization.
    requires_physical: tuple[str, ...] = ()
    #: Instrument-side fields only: where the bring-up DESIGN fallback comes from
    #: — (category to reach via Roster.one(), physical field), None category =
    #: the component's own physical slot. Used when the standing value is unset.
    design_source: tuple[str | None, str] | None = None


@dataclass(frozen=True)
class CategorySpec:
    """Schema of one component category."""

    side: Side
    #: "bare" = standalone physical part; "interaction" = physical term binding
    #: members by role; "single" = instrument-side operational unit;
    #: "composite" = instrument-side multi-component unit (TransmonPair). A
    #: composite carries NO member_roles of its own — topology lives once, on
    #: the name's PHYSICAL interaction term (q1_q2's members belong to Coupling).
    kind: Kind
    doc: str
    fields: dict[str, FieldSpec]
    #: interaction terms only: role -> allowed categories of the referenced
    #: component, checked against the member's PHYSICAL slot at roster load
    #: (interaction terms are physical-side, so they constrain the member's
    #: physical slot).
    member_roles: dict[str, tuple[str, ...]] = _field(default_factory=dict)
    #: roles in member_roles a roster entry MAY omit (e.g. Coupling's "coupler"
    #: — declared only when the chip's coupler is tracked as a satellite).
    optional_roles: tuple[str, ...] = ()
    #: instrument side only: the operation vocabulary components of this
    #: category may declare in the roster (validated there; experiments'
    #: required_operations check against the component's declared set).
    operations: tuple[str, ...] = ()


_TRANSMON_FIELDS: dict[str, FieldSpec] = {
    "f_01_hz": FieldSpec(
        "Hz", "Qubit 0->1 frequency at the operating point — the measured FACT "
              "(the drive_freq knob is its instrument twin; one fit writes both).",
        push=False),
    "anharmonicity_hz": FieldSpec(
        "Hz", "Transmon anharmonicity (f_12 - f_01). No estimator yet: carries "
              "the DESIGN value today; measured when an EF-spectroscopy "
              "experiment lands.",
        push=False),
    "t1_s": FieldSpec("s", "Energy-relaxation time T1.", push=False),
    "t2_star_s": FieldSpec("s", "Ramsey dephasing time T2*.", push=False),
    "t2_echo_s": FieldSpec("s", "Hahn-echo coherence time T2_echo.", push=False),
}

#: The Phase-1 catalog. Category names are CamelCase (settled); field names keep
#: the established short spellings; all values SI with unit-suffix names.
CATEGORIES: dict[str, CategorySpec] = {
    # ------------------------------------------------------------ physical/bare
    "FixedTransmon": CategorySpec(
        side="physical", kind="bare",
        doc="Fixed-frequency transmon: coherence + spectrum facts of the qubit itself.",
        fields=dict(_TRANSMON_FIELDS),
    ),
    "FluxTunableTransmon": CategorySpec(
        side="physical", kind="bare",
        doc="Flux-tunable transmon: the fixed set plus the flux arch. The "
            "volts-to-flux transfer function is NOT here — it belongs to the "
            "ZControl interaction term (wiring-dependent).",
        fields={
            **_TRANSMON_FIELDS,
            "ej_sum_hz": FieldSpec(
                "Hz", "Total Josephson energy EJ1+EJ2 (transmon arch fit). "
                      "Renamed from ej_sum_ghz at the component cutover.",
                push=False),
            "f_q_max_hz": FieldSpec(
                "Hz", "Qubit 0-1 frequency at the sweet spot (arch top).", push=False),
        },
    ),
    "Resonator": CategorySpec(
        side="physical", kind="bare",
        doc="Readout resonator: its own spectrum facts (previously misfiled on "
            "the qubit's row).",
        fields={
            "f_r_hz": FieldSpec(
                "Hz", "Dressed resonator frequency: the spectroscopy dip position.",
                push=False),
            "f_r0_hz": FieldSpec("Hz", "Bare resonator frequency (dispersive fit).",
                                 push=False),
            "kappa_tot_hz": FieldSpec(
                "Hz", "TOTAL resonator linewidth (power-Lorentzian FWHM) — what the "
                      "magnitude fit yields. The coupling/intrinsic split waits for "
                      "a complex-S21 circle-fit estimator. Renamed from kappa_hz.",
                push=False),
        },
    ),
    # ----------------------------------------------------- physical/interaction
    "ReadoutLine": CategorySpec(
        side="physical", kind="interaction",
        doc="Transmon-resonator readout coupling (the dispersive-fit g).",
        fields={
            "g_hz": FieldSpec(
                "Hz", "Qubit-resonator coupling from the dispersive fit "
                      "(f_r arch vs qubit detuning).",
                push=False),
        },
        member_roles={"transmon": ("FixedTransmon", "FluxTunableTransmon"),
                      "resonator": ("Resonator",)},
    ),
    "XYControl": CategorySpec(
        side="physical", kind="interaction",
        doc="Which XY line drives which transmon — TOPOLOGY ONLY. No lab "
            "experiment measures the absolute line-qubit coupling (pi_amp is "
            "what gets calibrated); a field lands here when an experiment that "
            "measures it is named.",
        fields={},
        member_roles={"transmon": ("FixedTransmon", "FluxTunableTransmon")},
    ),
    "ZControl": CategorySpec(
        side="physical", kind="interaction",
        doc="The DAC-volts -> flux transfer function of one flux line onto one "
            "transmon: flux/Phi0 = (V - v_offset_v) / v_per_phi0_v. Volt-valued, "
            "wiring- and cooldown-dependent — exactly why physical.json is per "
            "(cooldown, setup). Re-homed from the old per-qubit "
            "sweet_spot_flux_v / dv_phi0_v.",
        fields={
            "v_offset_v": FieldSpec(
                "V", "Sweet-spot voltage offset of the transfer function "
                     "(was sweet_spot_flux_v).",
                push=False),
            "v_per_phi0_v": FieldSpec(
                "V", "Volts per flux quantum on this line (was dv_phi0_v).",
                push=False),
        },
        member_roles={"transmon": ("FluxTunableTransmon",)},
    ),
    "Coupling": CategorySpec(
        side="physical", kind="interaction",
        doc="Two-transmon coupling facts. Members are the HIGH/LOW idle-frequency "
            "qubits (roles are NEVER control/target/moving — which qubit moves in "
            "a gate is a per-operation vendor fact, not roster topology). The "
            "optional 'coupler' member is a physical-only FluxTunableTransmon "
            "satellite (the q1_res pattern): its facts use the standard transmon "
            "vocabulary and are measured THROUGH the pair's operations.",
        fields={
            "zz_hz": FieldSpec(
                "Hz", "Signed residual ZZ at the standing coupler operating "
                      "point (pair_zz_coupler fit).",
                push=False),
            "j_hz": FieldSpec(
                "Hz", "Exchange coupling J. No estimator yet (needs a chevron "
                      "fit in scqat): carries the DESIGN value today.",
                push=False),
        },
        member_roles={"high": ("FluxTunableTransmon", "FixedTransmon"),
                      "low": ("FluxTunableTransmon", "FixedTransmon"),
                      "coupler": ("FluxTunableTransmon",)},
        optional_roles=("coupler",),
    ),
    # -------------------------------------------------------- instrument/single
    "ReadableTransmon": CategorySpec(
        side="instrument", kind="single",
        doc="A transmon we can drive and read out. The five established pushed "
            "knobs plus the fidelity monitor; idle_flux_v exists only on "
            "flux-tunable realizations.",
        fields={
            "readout_freq": FieldSpec(
                "Hz", "Resonator readout frequency (the operating CHOICE; the "
                      "measured fact is Resonator.f_r_hz).",
                push=True, design_source=("Resonator", "f_r_hz")),
            "drive_freq": FieldSpec(
                "Hz", "Qubit 0->1 drive frequency (the operating CHOICE; the "
                      "measured fact is f_01_hz).",
                push=True, design_source=(None, "f_01_hz")),
            "pi_amp": FieldSpec(
                "", "Amplitude of the calibrated pi (x180) pulse.",
                push=True, portable=False),
            "readout_amp": FieldSpec(
                "", "Amplitude of the readout pulse (dimensionless, within the "
                    "backend's current output-power configuration).",
                push=True, portable=False),
            # Keep readout_power_dbm AFTER readout_amp: pushes go in declaration
            # order and the absolute power must win (readout_amp is the chain
            # solve's residual).
            "readout_power_dbm": FieldSpec(
                "dBm",
                "Absolute readout pulse power at the instrument output port. "
                "Setting it re-solves the output chain (QM full_scale_power_dbm "
                "/ Qblox output_att) keeping the digital amplitude <= 0.5 full "
                "scale; readout_amp changes as a COUPLED side effect.",
                push=True),
            # Keep readout_duration_s BEFORE readout_integration_s: pushes go in
            # declaration order, so growing both in one command lengthens the
            # pulse before the window that must fit inside it.
            "readout_duration_s": FieldSpec(
                "s", "Readout pulse length. Positive multiple of 4 ns (the "
                     "portable grid — QM's pulse/weights resolution; drivers "
                     "REFUSE off-grid values, no silent rounding). Shrinking "
                     "the pulse clamps readout_integration_s down with it "
                     "(recorded as a COUPLED change).",
                push=True),
            "readout_integration_s": FieldSpec(
                "s", "Acquisition integration window; contract: <= "
                     "readout_duration_s (QM realizes the window as constant "
                     "integration weights zero-padded to the pulse length, so "
                     "it cannot outlive the pulse; Qblox measure.integration_"
                     "time could, but drivers refuse it for portability). "
                     "Positive multiple of 4 ns.",
                push=True),
            "readout_fidelity": FieldSpec(
                "", "Single-shot assignment fidelity (0.5..1) — measured monitor, "
                    "never pushed.",
                push=False),
            "idle_flux_v": FieldSpec(
                "V", "Standing flux-bias voltage at idle. Volts (what is pushed); "
                     "the portable Phi0 representation is derived via the "
                     "ZControl transfer function, never stored as a second knob.",
                push=True, portable=False,
                requires_physical=("FluxTunableTransmon",)),
        },
        operations=("rx", "readout", "flux_bias"),
    ),
    # ----------------------------------------------------- instrument/composite
    "TransmonPair": CategorySpec(
        side="instrument", kind="composite",
        doc="An operable two-qubit pair (QCQ tunable-coupler architecture). "
            "Carries the coupler's standing flux knobs; gate-pulse/macro "
            "parameters stay vendor-only until scqo experiments calibrate them. "
            "Topology (high/low/coupler members) lives on the name's physical "
            "Coupling term, not here.",
        fields={
            "coupler_decouple_v": FieldSpec(
                "V", "Coupler standing bias where the qubit-qubit interaction is "
                     "OFF (the ZZ-off point — pair_zz_coupler's product). Volts "
                     "on this pair's coupler flux line, wiring-dependent.",
                push=True, portable=False),
            "coupler_interaction_v": FieldSpec(
                "V", "Coupler standing bias where the interaction is ON (gate "
                     "operating point). Volts on this pair's coupler flux line.",
                push=True, portable=False),
        },
        operations=("coupler_bias", "iswap"),
    ),
}

#: Canonical operation vocabulary (catalog gating + roster validation).
OPERATIONS = ("rx", "readout", "flux_bias", "coupler_bias", "iswap")


def pushed_fields(category: str) -> tuple[str, ...]:
    """The vendor-realized fields of an instrument category, in declaration
    (push) order. Physical categories have none by construction."""
    return tuple(f for f, s in CATEGORIES[category].fields.items() if s.push)


def field_categories() -> dict[str, tuple[str, ...]]:
    """Reverse index field -> categories declaring it (error-hint surface)."""
    out: dict[str, list[str]] = {}
    for cat, spec in CATEGORIES.items():
        for f in spec.fields:
            out.setdefault(f, []).append(cat)
    return {f: tuple(cats) for f, cats in out.items()}


def _validate_catalog() -> None:
    """Import-time invariants: a broken catalog must never load."""
    for name, spec in CATEGORIES.items():
        if spec.side == "physical":
            assert all(not s.push for s in spec.fields.values()), (
                f"{name}: physical fields are never pushed (no vendor knob for "
                f"sample physics)")
            assert all(s.design_source is None and not s.requires_physical
                       for s in spec.fields.values()), (
                f"{name}: design_source/requires_physical are instrument-side "
                f"concepts")
        if spec.kind == "interaction":
            assert spec.member_roles, f"{name}: interaction terms need member roles"
            assert spec.side == "physical", f"{name}: interaction terms are physical-side"
        else:
            assert not spec.member_roles, f"{name}: only interaction terms take members"
        if spec.kind == "composite":
            assert spec.side == "instrument", (
                f"{name}: composites are instrument-side (their topology lives "
                f"on the physical interaction term)")
        assert not spec.operations or spec.side == "instrument", (
            f"{name}: operations belong to instrument categories")
        assert set(spec.optional_roles) <= set(spec.member_roles), (
            f"{name}: optional_roles must name declared member roles")
        for role, allowed in spec.member_roles.items():
            for cat in allowed:
                assert cat in CATEGORIES, f"{name}.{role}: unknown member category {cat!r}"
                assert CATEGORIES[cat].side == spec.side, (
                    f"{name}.{role}: member categories must be same-side")
        for f, s in spec.fields.items():
            if s.design_source is not None:
                src_cat, src_field = s.design_source
                if src_cat is not None:
                    assert src_cat in CATEGORIES and CATEGORIES[src_cat].side == "physical", (
                        f"{name}.{f}: design_source category must be physical")
                    assert src_field in CATEGORIES[src_cat].fields, (
                        f"{name}.{f}: design_source field {src_field!r} not in {src_cat}")


_validate_catalog()
