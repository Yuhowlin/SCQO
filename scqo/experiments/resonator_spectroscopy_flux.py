"""Resonator spectroscopy vs flux — the dispersive flux map, greenfield.

Port of :mod:`scqo.experiments.resonator_spectroscopy_flux`. The physics
half is byte-for-byte; what moved is the device surface and the field
spellings: the flux-transfer facts land on the target's FLUX CHANNEL
under their new names (``v_offset_v`` -> ``flux_offset``,
``v_per_phi0_v`` -> ``flux_per_phi0``), the sweet-spot idle point is
two channel knobs (``idle_flux`` on the flux channel, ``readout_freq_hz``
on the readout channel), and the dispersive physics (``f_bare_hz``,
``g_hz``) goes on the attached RESONATOR mode.

FRAME (no ``_pulse`` in the name, plain ``FluxSweepParameters``): the probe
SETS the line's DC offset to each swept value, so the window is ABSOLUTE DAC
volts and its fitted ``flux_offset`` is already an absolute set-point — the
plane the ``_pulse`` experiments re-reference themselves onto. See
:mod:`._capabilities.flux`.
"""

from __future__ import annotations

import warnings
from typing import ClassVar, Literal

import numpy as np
from pydantic import Field

from .._scqat import per_qubit_results
from ..contract import DatasetContract
from ..experiment import FACT_DESIGN, FACT_MEASURED
from ._transmon_estimate import (
    DEFAULT_GAP_DELTA_HZ,
    f_q_max_hz_from_parked,
    f_q_max_hz_from_resistance,
    g_coeff_from_g,
    g_hz_from_coeff,
)
from ._capabilities.detuning import (
    ReadoutDetuningSweepParameters,
    readout_detuning_sweep,
)
from ._capabilities.flux import (
    NUM_FLUX_DESC,
    FluxSweepParameters,
    flux_anchor_v,
    flux_sweep,
    foreign_flux_source,
    standing_flux_v,
)
from ._sim import stable_seed
from ..parameters import AveragingParameters, TargetSelection
from ..result import Outcome, Result
from ..experiment import Experiment
from . import register
from ._flux_component import FluxComponentParameters


class ResonatorSpectroscopyFluxParameters(
    TargetSelection, AveragingParameters, FluxSweepParameters,
    ReadoutDetuningSweepParameters, FluxComponentParameters
):
    """Inputs for the resonator-vs-flux map.

    The frequency window is the readout_detuning capability's
    ``[start_readout_detuning_hz, end_readout_detuning_hz]`` pair, relative to
    the target's current ``readout_freq_hz``. An ASYMMETRIC one is the natural
    shape here: ``readout_freq_hz`` is normally parked at the sweet spot, and
    detuning the qubit only ever shrinks the pull ``g^2/(f_r0 - f_q)``, so the
    arch walks DOWNWARD out of a centred window.
    """

    # capability default narrowed: the dispersive fit needs >= 5 good slices
    num_flux_points: int = Field(21, gt=4, description=NUM_FLUX_DESC + " (the dispersive fit needs >= 5 good slices).")
    analysis_method: Literal["dispersive", "sine"] = Field(
        "dispersive",
        description=(
            "Flux-model fit: 'dispersive' = full flux-tunable-transmon model "
            "f_r = f_r0 + g^2/(f_r0 - f_q(flux)) — yields bare f_r0 and (conditional) "
            "coupling g on top of the sweet-spot flux + period. 'sine' = a bare "
            "cosine of the flux — model-light and robust when only ~one arch is "
            "visible or the trace is noisy, but yields no f_r0/g (so f_bare_hz/g_hz are "
            "never proposed)."
        ),
    )
    edge_margin_frac: float = Field(
        0.06, ge=0, lt=0.5,
        description=(
            "Reject per-flux dip centres pinned within this fraction of the swept "
            "detuning window of either edge before the flux-model fit. Edge-pinned "
            "centres from low-SNR slices otherwise capture the fit seed and pull the "
            "sweet spot to the wrong flux. 0 disables."
        ),
    )
    dip_method: Literal["lorentzian", "circle"] = Field(
        "lorentzian",
        description=(
            "Per-slice dip fit: 'lorentzian' = joint Lorentzian + background fit of "
            "|IQ|^2 (fast, magnitude-only). 'circle' = Probst notch-model fit of the "
            "complex S21 — handles Fano-asymmetric dips, but needs meaningful phase "
            "data (on the simulated backend, whose Q quadrature is noise, slices fall "
            "back to the coarse argmin centre)."
        ),
    )


#: where the dispersive fit's f_q_max came from, recorded per target in ``fit``.
#: Only a MEASURED origin makes the fitted f_r0/g physics — see :func:`_f_q_max_hz`.
F_Q_MAX_DRIVE = "drive_freq"   # the target's standing drive_freq_hz (arch-top proxy)
F_Q_MAX_DRIVE_ARCH = "drive_freq_arch"  # ...corrected to the top through a STORED arch
F_Q_MAX_RESISTANCE = "junction_resistance"  # predicted from the fab's R_n
F_Q_MAX_DESIGN = "design"      # the datasheet's f_q_max_hz
F_Q_MAX_ASSUMED = "assumed"    # nothing known — the estimator's placeholder heuristic

#: where the fit's BARE resonator frequency came from, recorded per target in
#: ``fit``. Only a MEASURED one is PINNED — see :func:`_f_bare_hz`.
F_BARE_MEASURED = "measured"   # stored fact -> pinned as a fit constant
F_BARE_DESIGN = "design"       # datasheet -> seed only, f_r0 stays free
F_BARE_FREE = "free"           # nothing known -> seeded from the trace, free

#: code-default terminals of the fact -> design -> default precedence for the
#: fixed/seed inputs of the dispersive arch (the estimator carries the same
#: numbers as its own backstop; kept equal on purpose).
_DEFAULT_EC_HZ = 0.2e9         # typical transmon charging energy E_C
_DEFAULT_G_INIT_HZ = 50e6      # physical seed for the fitted coupling g

_F_BARE_WARNING = (
    "{target}: the dispersive fit's bare resonator frequency came from design.toml "
    "({value:.6g} Hz), not a measurement, so it was used only as a SEED and f_r0 was "
    "fitted free. The trace constrains only the pull g^2/(f_r0 - f_q), so f_r0 and g "
    "trade off: the reported g is CONDITIONAL and is not proposed as a fact. Measure "
    "the bare resonator (high-power punchout, where the qubit saturates) and store it "
    "as {entity}.f_bare_hz to pin it."
)


def _actual_resonator_hz(experiment, res_entity: str,
                         target: str) -> float | None:
    """The chip's resonator frequency for a coupling calculation, or None.

    MEASURED ``f_bare_hz`` first (the uncoupled mode the coupling formula wants),
    then measured ``f_dress0_hz``, then the standing ``readout_freq_hz``. The
    three differ by the Lamb shift, ~0.1% under the square root that consumes
    this, so the ordering is a preference and not a correctness requirement.
    """
    if experiment.physical is not None:
        for field in ("f_bare_hz", "f_dress0_hz"):
            stored = experiment.physical.get(res_entity, field)
            if stored is not None:
                return float(stored)
    try:
        return float(experiment.anchor(target, "readout_freq_hz"))
    except (ValueError, KeyError, AttributeError):
        return None


def _arch_top_from_parked(experiment, target: str,
                          f_q_parked_hz: float) -> tuple[float, str]:
    """Project a parked qubit frequency to the arch TOP through a STORED arch.

    ``drive_freq_hz`` is the qubit at whatever flux the line is parked at; only at
    the sweet spot is it ``f_q_max``. When a previous map stored ``flux_offset``
    and ``flux_per_phi0`` on the flux channel, the standing ``idle_flux`` says how
    far off the sweet spot we are and the arch inverts exactly
    (:func:`._transmon_estimate.f_q_max_hz_from_parked`).

    Degrades to the uncorrected value (tag ``drive_freq``) whenever the arch is
    not stored, the idle bias is unreadable, or the projection refuses (parked far
    down the arch, where inverting amplifies every error) — the parked-at-the-
    sweet-spot assumption is then explicit in the tag rather than silent.
    """
    offset = experiment.fact(target, "flux_offset", None)
    period = experiment.fact(target, "flux_per_phi0", None)
    # standing_flux_v, NOT flux_anchor_v: this probe sweeps in the ABSOLUTE frame,
    # where flux_anchor_v is the window ORIGIN and returns a hardcoded 0.0 — which
    # would read as "parked at 0 V" and invent a huge bogus correction.
    idle = standing_flux_v(experiment, target)
    if offset is None or period is None or idle is None:
        return f_q_parked_hz, F_Q_MAX_DRIVE
    ec = experiment.fact(target, "ec_hz", _DEFAULT_EC_HZ)
    try:
        corrected = f_q_max_hz_from_parked(
            f_q_parked_hz, ec, float(idle), float(offset), float(period))
    except ValueError:
        return f_q_parked_hz, F_Q_MAX_DRIVE
    if not (np.isfinite(corrected) and corrected > 0.0):
        return f_q_parked_hz, F_Q_MAX_DRIVE
    return float(corrected), F_Q_MAX_DRIVE_ARCH


def _f_q_max_hz(experiment, target: str) -> tuple[float | None, str]:
    """The arch-top qubit frequency to hold FIXED in the dispersive fit, and where
    it came from.

    WHY THIS MATTERS MORE THAN IT LOOKS: ``f_q_max`` is not fitted (the trace fixes
    only the PRODUCT ``g^2 * f_q_max`` — see the estimator's degeneracy note), so an
    assumed value cannot be corrected by the data. The error lands in ``g``
    (``g^2 ~ max_pull * detuning``, so an overstated detuning inflates it) and in
    ``f_r0``, which the fit must push further below the trace to keep the model's
    top:bottom pull ratio; that is what makes the fitted curve dive under the data at
    the LOW sweet spot. Sourcing a measured value fixes both at once.

    Precedence, best first:

    1. the target's standing ``drive_freq_hz`` — the qubit frequency AT THE PARKED
       FLUX, which is the arch top exactly while the qubit sits at its sweet spot;
       the same proxy, and the same reason, ``qubit_spectroscopy_cryoscope`` uses
       for its arch curvature. When a PREVIOUS map already stored the arch
       (``flux_offset`` + ``flux_per_phi0``), :func:`_arch_top_from_parked` removes
       that assumption by projecting the parked frequency back to the top and the
       tag becomes ``drive_freq_arch``; at the sweet spot the correction is exactly
       nothing (the 5Q4C qubits park within 0.5 mV of theirs, so it is a guard for
       a deliberately detuned park rather than a numeric fix);
    2. the fab's ``junction_resistance_ohm`` through Ambegaokar-Baratoff
       (:mod:`._transmon_estimate`) — as-FABRICATED, so it beats the datasheet
       whenever the qubit has not answered yet;
    3. the datasheet's ``f_q_max_hz``;
    4. ``None`` — the estimator applies its own placeholder, and f_r0/g are withheld.

    A FOREIGN flux source is excluded from 1: the arch being swept is then not the
    target's own, so the target's drive frequency says nothing about it. (2 and 3
    still apply — they describe the target's own junction either way.)

    The auto-source is also declined for a qubit sitting ABOVE its resonator: the
    model pulls the resonator UP (``g^2 / (f_r0 - f_q)`` with ``f_r0 > f_q``, a bound
    the estimator enforces), so such a device is outside the dispersive method
    altogether and 'assumed' — which withholds f_r0/g rather than reporting a
    clamped fit as physics — is the honest answer.
    """
    value, source = None, F_Q_MAX_ASSUMED
    if not foreign_flux_source(experiment.params):
        try:
            candidate = float(experiment.anchor(target, "drive_freq_hz"))
        except (ValueError, KeyError, AttributeError):
            candidate = float("nan")
        if np.isfinite(candidate) and candidate > 0.0:
            value, source = _arch_top_from_parked(experiment, target, candidate)
    if value is None:
        value, source = _f_q_max_from_fabrication(experiment, target)
    if value is None:
        return None, F_Q_MAX_ASSUMED
    try:  # best-effort sanity gate; a missing readout frequency does not block
        resonator = float(experiment.anchor(target, "readout_freq_hz"))
    except (ValueError, KeyError, AttributeError):
        return value, source
    if np.isfinite(resonator) and value >= resonator:
        return None, F_Q_MAX_ASSUMED
    return value, source


def _f_q_max_from_fabrication(experiment, target: str) -> tuple[float | None, str]:
    """f_q_max from the fab's junction resistance, else the datasheet.

    The resistance route outranks the datasheet because it is as-FABRICATED: it
    already contains the scatter that moved the qubit off its design frequency.
    """
    ec = experiment.fact(target, "ec_hz", _DEFAULT_EC_HZ)
    r_n = experiment.fact(target, "junction_resistance_ohm", None)
    if r_n is not None:
        gap = experiment.fact(target, "gap_delta_hz", DEFAULT_GAP_DELTA_HZ)
        try:
            return f_q_max_hz_from_resistance(r_n, ec, gap), F_Q_MAX_RESISTANCE
        except ValueError:
            pass  # a nonsensical stored resistance must not kill the run
    designed = experiment.fact(target, "f_q_max_hz", None)
    if designed is not None and np.isfinite(designed) and designed > 0.0:
        return float(designed), F_Q_MAX_DESIGN
    return None, F_Q_MAX_ASSUMED


def _g_init_hz(experiment, target: str, f_q_max_hz: float | None) -> float:
    """Seed for the fitted coupling g: fact -> design-RESCALED -> code default.

    Physically ``g ∝ sqrt(f_q · f_r)`` — the proportionality coefficient is the
    geometry constant (capacitance ratios), the frequencies are wherever the
    modes actually sit. A ``design.toml`` g is therefore only valid at the
    DESIGN frequencies: when the fabricated chip landed elsewhere (junction
    scatter moves f_q_max by hundreds of MHz), the design value is rescaled by
    ``sqrt((f_q · f_r) / (f_q_design · f_r_design))`` before seeding. A
    MEASURED g needs no rescale (it is the chip's own coupling at its own
    frequencies), and any missing number degrades to the unscaled design value
    — this seeds a FITTED parameter, so degrading quietly is correct here.

    ``f_q_max_hz`` is the arch top ``estimate()`` already resolved through
    :func:`_f_q_max_hz` (None when nothing was known). The actual resonator
    frequency prefers the measured ``f_bare_hz``, then measured ``f_dress0_hz``,
    then the standing ``readout_freq_hz``; the design side prefers ``f_bare_hz``
    then ``f_dress0_hz`` — the difference between the two resonator choices is
    the Lamb shift, 0.1% under the square root.
    """
    res_entity = experiment.device.resonator_of(target)
    # A stored/design g_coeff is the DIRECT route: it is frequency-independent by
    # construction, so it needs no rescale — just evaluate it here. Preferred over
    # reconstructing the same thing from a design g_hz plus design frequencies.
    coeff, _coeff_tier = experiment.fact_sourced(target, "g_coeff", None)
    if coeff is not None and np.isfinite(coeff) and coeff > 0.0:
        f_r_now = _actual_resonator_hz(experiment, res_entity, target)
        if (f_q_max_hz is not None and f_r_now is not None
                and np.isfinite(f_q_max_hz) and f_q_max_hz > 0.0):
            try:
                return g_hz_from_coeff(float(coeff), f_q_max_hz, f_r_now)
            except ValueError:
                pass  # fall through to the g_hz tiers

    value, tier = experiment.fact_sourced(target, "g_hz", None)
    if value is None:
        return _DEFAULT_G_INIT_HZ
    if tier != FACT_DESIGN:
        return float(value)

    f_q_design = experiment.design.get(target, "f_q_max_hz")
    f_r_design = None
    for field in ("f_bare_hz", "f_dress0_hz"):
        f_r_design = experiment.design.get(res_entity, field)
        if f_r_design is not None:
            break
    f_r_actual = _actual_resonator_hz(experiment, res_entity, target)

    inputs = (f_q_max_hz, f_r_actual, f_q_design, f_r_design)
    if any(v is None or not np.isfinite(v) or v <= 0.0 for v in inputs):
        return float(value)
    return float(value) * float(np.sqrt(
        (f_q_max_hz * f_r_actual) / (float(f_q_design) * float(f_r_design))))


def _f_bare_hz(experiment, target: str) -> tuple[float | None, bool, str]:
    """The BARE resonator frequency for the fit: ``(value, pin?, source)``.

    A MEASURED value is PINNED, which breaks the second degeneracy of this model
    (``f_r0`` against ``g``) and is what makes ``g`` quantitative. A DESIGN value is
    NOT pinned, only seeded: the whole flux-dependent signal is the few-MHz pull, so
    a datasheet frequency carrying the usual 10-50 MHz of resonator fab scatter would
    inject more error into ``g`` than the pin removes — measured on real data, a
    5 MHz error moves ``g`` by ~30% and a 20 MHz error makes the pull negative and
    the model undefined. With neither, the estimator keeps its historical behaviour
    (seed from the trace, fit free).

    Never refuses: ``sweet_spot_flux`` / ``flux_per_phi0`` are robust to this
    degeneracy and are the bring-up deliverable.
    """
    value, tier = experiment.fact_sourced(target, "f_bare_hz", None)
    if value is None or not np.isfinite(value) or value <= 0.0:
        return None, False, F_BARE_FREE
    if tier == FACT_MEASURED:
        return float(value), True, F_BARE_MEASURED
    return float(value), False, F_BARE_DESIGN


def _arch_anchored(experiment, target: str, fit: dict) -> bool:
    """True when this target's dispersive fit was pinned to a MEASURED arch top —
    the gate on proposing ``f_bare_hz``/``g_hz``.

    Normally reads the ``f_q_max_source`` tag ``estimate()`` recorded. A CAMPAIGN
    finalize replays ``update()`` over the aggregated fit, which keeps only numeric
    quantities, so the tag is gone there — re-derive it from the same precedence
    rather than silently withholding physics the per-repeat runs did propose. That
    re-derivation reads device state through whatever surface ``update()`` is running
    against; any failure degrades to the withholding answer (the safe direction — an
    assumed value must never enter the ledger), never a finalize crash.
    """
    source = fit.get("f_q_max_source")
    if source is None:
        try:
            source = _f_q_max_hz(experiment, target)[1]
        except Exception:  # noqa: BLE001 - never fail a finalize over provenance
            source = F_Q_MAX_ASSUMED
    return source != F_Q_MAX_ASSUMED


class ResonatorSpectroscopyFluxResult(Result):
    """``fit[qubit]``: ``flux_offset`` (upper sweet-spot flux), ``sweet_spot_res_hz``
    (resonator centre freq there), ``sweet_spot_low_flux_v``/``sweet_spot_low_res_hz``
    (the LOWER sweet spot — record-only, derivable as flux_offset ± flux_per_phi0/2),
    ``flux_per_phi0`` (flux period), plus
    ``f_bare_hz``/``g_hz``/``f_q_max_hz`` and the ``f_q_max_source`` provenance tag
    (``param``/``drive_freq``/``assumed``) for the dispersive method only —
    ``assumed`` means f_bare_hz/g_hz are conditional on a placeholder detuning and are
    NOT proposed. ``update()`` proposes the
    physical facts on the qubit's flux channel (flux_offset/flux_per_phi0) and
    resonator mode (f_bare_hz/g_hz), and two idle-point channel knobs:
    ``idle_flux`` on the flux channel (= flux_offset; park at the sweet spot) and
    ``readout_freq_hz`` on the readout channel (= sweet_spot_res_hz; read out at
    the resonator dip there)."""


@register
class ResonatorSpectroscopyFlux(Experiment):
    """Backend-agnostic resonator flux map. ``probe()`` is supplied by a driver."""

    name: ClassVar[str] = "resonator_spectroscopy_flux"
    description: ClassVar[str] = (
        "2D resonator spectroscopy vs ABSOLUTE flux bias (the probe sets the line's "
        "DC offset per point, so the window is DAC volts, not an excursion from "
        "idle_flux): tracks the dip at every flux and fits "
        "its flux dependence with a selectable model (analysis_method='dispersive' or "
        "'sine'); proposes the sweet-spot flux (flux_offset) + flux period "
        "(flux_per_phi0) as physical facts on the qubit's flux channel, and "
        "sets the idle point at the sweet spot (the flux channel's "
        "idle_flux = flux_offset, the readout channel's readout_freq_hz = "
        "resonator dip there). This is the BRING-UP seed for idle_flux, found "
        "without needing a prior idle point; once the qubit answers, "
        "qubit_spectroscopy_flux_pulse is the authority for that knob. "
        "Plus bare f_bare_hz and coupling g_hz on the attached resonator mode "
        "when the dispersive method ran against a MEASURED arch top, auto-sourced "
        "per target from the standing drive_freq_hz, else the fab's "
        "junction_resistance_ohm (Ambegaokar-Baratoff), else the datasheet. With "
        "none of those the fit only ASSUMES f_q_max, and assumptions are not "
        "recorded as physics. The bare resonator is PINNED when f_bare_hz is a "
        "stored measurement (which is what makes g quantitative) and only seeded "
        "when it is merely designed."
    )
    Parameters: ClassVar[type] = ResonatorSpectroscopyFluxParameters
    Result: ClassVar[type] = ResonatorSpectroscopyFluxResult
    Contract: ClassVar[DatasetContract] = DatasetContract(
        sweeps=("flux_bias_v", "detuning_hz"), sweep_units=("V", "Hz"), variables=("I", "Q")
    )
    required_operations: ClassVar[tuple[str, ...]] = ("readout", "flux_bias")

    params: ResonatorSpectroscopyFluxParameters

    def define_sweep(self) -> dict[str, np.ndarray]:
        return {**flux_sweep(self.params), **readout_detuning_sweep(self.params)}

    def simulate(self, coords: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
        flux = coords["flux_bias_v"]
        detuning = coords["detuning_hz"]
        targets = self.params.targets
        rng = np.random.default_rng(stable_seed("resonator_spectroscopy_flux", *targets))
        kappa = (detuning[-1] - detuning[0]) / 40
        i_data = np.empty((len(targets), flux.size, detuning.size))
        q_data = np.empty_like(i_data)
        for k, q in enumerate(targets):
            # centers generated FROM the dispersive model the estimator fits
            readout_now = float(self.device.channel(q, "readout").readout_freq_hz)
            f_q_max = float(self.device.channel(q, "drive").drive_freq_hz)
            # E_c from the same fact -> design -> default source estimate() uses,
            # so the forward model matches the arch the estimator fits (GHz here).
            ec = self.fact(q, "ec_hz", _DEFAULT_EC_HZ) * 1e-9
            sweet = rng.uniform(0.3 * flux.min(), 0.3 * flux.max())
            period = rng.uniform(1.8, 2.6) * (flux.max() - flux.min())
            g = rng.uniform(70e6, 100e6)
            f_r0 = readout_now - 2e6  # bare resonator (dressed sits above for f_q < f_r0)
            ej_sum = ((f_q_max * 1e-9 + ec) ** 2) / (8.0 * ec)
            quan = (flux - sweet) / period
            f_q = (np.sqrt(8.0 * ec * ej_sum * np.abs(np.cos(np.pi * quan))) - ec) * 1e9
            centers = (f_r0 + g**2 / (f_r0 - f_q)) - readout_now  # as detuning
            noise = 0.01
            for j in range(flux.size):
                magnitude = 1.0 - 0.75 / (1.0 + ((detuning - centers[j]) / kappa) ** 2)
                i_data[k, j] = magnitude + rng.normal(0, noise, detuning.size)
                q_data[k, j] = rng.normal(0, noise, detuning.size)
        return {"I": i_data, "Q": q_data}

    def estimate(self) -> ResonatorSpectroscopyFluxResult:
        assert self.dataset is not None, "run() populates self.dataset before estimate()"
        from scqat.estimators.resonator_spectroscopy_flux import ResonatorSpectroscopyFluxEstimator

        targets = list(self.dataset["target"].values)
        old_freqs = {
            q: float(self.device.channel(q, "readout").readout_freq_hz) for q in targets
        }
        prepared = self.dataset.rename({"flux_bias_v": "flux_bias", "detuning_hz": "detuning"})
        prepared = prepared.transpose("target", "flux_bias", "detuning")
        detuning = prepared["detuning"].values
        full_freq = np.array([detuning + old_freqs[q] for q in targets])
        prepared = prepared.assign_coords(full_freq=(("target", "detuning"), full_freq))

        kwargs = {
            "method": self.params.analysis_method,
            "dip_method": self.params.dip_method,
            "edge_margin_frac": float(self.params.edge_margin_frac),
        }
        # f_q_max, E_c and the g seed are per TARGET (each qubit sits at its own
        # detuning), so they ride per_target_kwargs rather than the shared set.
        # E_c and the g seed follow the fact -> design -> code-default precedence
        # (Experiment.fact): a stored ec_hz/g_hz wins, else design.toml, else the
        # code default. g_hz is re-seeded from what THIS experiment last wrote;
        # a DESIGN-tier g is first rescaled to the chip's actual frequencies
        # (g ∝ sqrt(f_q·f_r) — see _g_init_hz).
        sources: dict[str, str] = {}
        bare_sources: dict[str, str] = {}
        per_target: dict[str, dict] = {}
        for qubit in targets:
            q = str(qubit)
            value, source = _f_q_max_hz(self, q)
            sources[q] = source
            ptk: dict = {
                "ec": self.fact(q, "ec_hz", _DEFAULT_EC_HZ),
                # a design-tier g is rescaled to the chip's actual frequencies
                # (g ∝ sqrt(f_q·f_r)); a measured g rides through untouched
                "g_init": _g_init_hz(self, q, value),
            }
            if value is not None:
                ptk["f_q_max"] = value
            # The bare resonator: PINNED when measured (breaks the f_r0/g
            # degeneracy), merely seeded when only designed — and then said out
            # loud, because the resulting g is conditional.
            bare, pin, bare_source = _f_bare_hz(self, q)
            bare_sources[q] = bare_source
            if bare is not None:
                ptk["f_r0"] = bare
                ptk["fit_f_r0"] = not pin
                if not pin:
                    warnings.warn(
                        _F_BARE_WARNING.format(
                            target=q, value=bare,
                            entity=self.device.resonator_of(q)),
                        stacklevel=2)
            per_target[q] = ptk
        results = per_qubit_results(
            prepared, ResonatorSpectroscopyFluxEstimator(), artifact_dir=self.artifact_dir,
            per_target_kwargs=per_target, **kwargs
        )

        result = ResonatorSpectroscopyFluxResult()
        for qubit in self.params.targets:
            disp = results[qubit]["dispersion"]
            vs = results[qubit]["vs_flux"]
            fit = {
                "flux_offset": float(disp["sweet_spot_flux"]),
                "sweet_spot_res_hz": float(disp["sweet_spot_res"]),
                "sweet_spot_low_flux_v": float(disp["sweet_spot_low_flux"]),
                "sweet_spot_low_res_hz": float(disp["sweet_spot_low_res"]),
                "flux_per_phi0": float(disp["dv_phi0"]),
                "n_good_flux": int(vs["n_good"]),
                "old_readout_freq_hz": old_freqs[qubit],
                # The frame origin, 0.0 here: this probe SETS the DC offset, so
                # the window is measured from the DAC zero and whatever the line
                # was parked at is overridden point by point. Recorded anyway so
                # `flux_offset == old_idle_flux + <fitted in frame>` is one
                # invariant across the whole flux capability, both frames.
                "old_idle_flux": flux_anchor_v(self, qubit),
            }
            # Dispersive-only physics — the sine method produces no f_r0/g/f_q_max.
            # ec_hz is the sourced FIXED input (record-only provenance, never
            # proposed — an assumed/derived input is not measured physics).
            for src, dst in (("f_r0", "f_bare_hz"), ("g", "g_hz"),
                             ("f_q_max", "f_q_max_hz"), ("ec", "ec_hz")):
                if src in disp:
                    fit[dst] = float(disp[src])
            if "f_q_max_hz" in fit:
                # provenance of the fit's FIXED inputs: only a measured origin makes
                # the fitted f_bare_hz/g_hz physics (see update()).
                fit["f_q_max_source"] = sources.get(qubit, F_Q_MAX_ASSUMED)
                fit["f_bare_source"] = bare_sources.get(qubit, F_BARE_FREE)
            # The frequency-INDEPENDENT twin of the fitted g: the geometry
            # constant g / sqrt(f_q_max · f_bare). g_hz goes stale the moment the
            # qubit is re-tuned; this does not, which is why both are stored.
            if "g_hz" in fit and "f_q_max_hz" in fit and "f_bare_hz" in fit:
                try:
                    fit["g_coeff"] = g_coeff_from_g(
                        fit["g_hz"], fit["f_q_max_hz"], fit["f_bare_hz"])
                except ValueError:
                    pass  # a degenerate fit; g_coeff is simply not reported
            result.fit[qubit] = fit
            result.outcomes[qubit] = Outcome.SUCCESSFUL if bool(disp["success"]) else Outcome.FAILED
        return result

    def update(self) -> None:
        """Propose the flux-model quantities: physical facts + idle-point knobs.

        Sweet-spot flux + flux period are always proposed (robust flux-periodicity,
        produced by every method) as ``flux_offset``/``flux_per_phi0`` on the
        qubit's flux CHANNEL (PHYSICAL facts). Two pushed channel knobs set the
        idle point at the sweet spot: the flux channel's ``idle_flux`` =
        ``flux_offset`` (park at the upper sweet spot) and the readout channel's
        ``readout_freq_hz`` = ``sweet_spot_res_hz`` (read out at the resonator dip
        there — a later readout_frequency run refines it for fidelity).
        ``f_bare_hz`` / ``g_hz`` (both on the attached resonator mode) are proposed
        only when the DISPERSIVE method ran AND its ``f_q_max`` came from a MEASURED
        origin — the target's standing ``drive_freq_hz``, the fab's junction
        resistance, or the datasheet (``fit["f_q_max_source"]``, see
        :func:`_f_q_max_hz`). The
        sine method yields no such physics, and when nothing was known the estimator
        holds f_q_max at a placeholder guess that the trace cannot correct, so g is
        conditional on that assumption — an assumed value must never enter the
        measured-physics ledger. The gate is per QUBIT: on a multi-target run one
        qubit may be anchored and another not. ``f_q_max_hz`` itself is never
        proposed here (it is an INPUT of the dispersive fit;
        qubit_spectroscopy_flux_pulse measures it).
        """
        if self.result is None:
            return
        if foreign_flux_source(self.params):
            # Crosstalk / coupler-shift data, not the target's own arch — record-only.
            return
        # f_r0/g are physical only from the dispersive model with a MEASURED f_q_max.
        dispersive = self.params.analysis_method == "dispersive"
        for qubit, fit in self.result.fit.items():
            if self.result.outcomes[qubit] is not Outcome.SUCCESSFUL:
                continue
            flux_view = self.device.channel(qubit, "flux")
            for field in ("flux_offset", "flux_per_phi0"):
                if field in fit:
                    setattr(flux_view, field, fit[field])
            # Set up the idle point at the sweet spot — two pushed channel
            # knobs: park the standing idle flux at the sweet-spot bias
            # (idle_flux = flux_offset on the flux channel), and read out at the
            # resonator dip there (readout_freq_hz = sweet_spot_res_hz on the
            # readout channel). The flux channel exists because an own-flux run
            # requires the flux_bias operation (the target is flux-tunable); a
            # later readout_frequency run refines readout_freq_hz.
            if "flux_offset" in fit:
                flux_view.idle_flux = fit["flux_offset"]
            if "sweet_spot_res_hz" in fit:
                self.device.channel(qubit, "readout").readout_freq_hz = (
                    fit["sweet_spot_res_hz"])
            if dispersive and _arch_anchored(self, qubit, fit):
                res_view = self.device.component(self.device.resonator_of(qubit))
                # f_bare_hz is proposed only when the fit MEASURED it (f_r0 free).
                # A pinned f_r0 was an INPUT — echoing it back would launder a
                # stored value into a fresh measurement of itself.
                if ("f_bare_hz" in fit
                        and fit.get("f_bare_source", F_BARE_FREE) != F_BARE_MEASURED):
                    res_view.f_bare_hz = fit["f_bare_hz"]
                if "g_hz" in fit:
                    res_view.g_hz = fit["g_hz"]
                # ...and its frequency-independent twin, under the same gate: it
                # is the same measurement expressed as the geometry constant.
                if "g_coeff" in fit and np.isfinite(fit["g_coeff"]):
                    res_view.g_coeff = fit["g_coeff"]

    def probe(self):  # pragma: no cover - driver half
        raise NotImplementedError("a driver backend supplies probe()")
