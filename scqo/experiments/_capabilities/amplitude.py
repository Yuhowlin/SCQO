"""Amplitude-sweep capability: the absolute amplitude behind a swept RATIO.

Five experiments sweep a drive or readout-channel amplitude as a dimensionless
FACTOR of whatever the target currently stores — ``qubit_power_rabi`` and
``qubit_pi_pulse_error`` against the drive channel's ``pi_amp``,
``qubit_deterministic_benchmarking`` against ``pi_amp`` or ``pi_amp_x90``, and
``readout_power`` (the readout pulse) and ``qubit_resonator_stark`` (its Stark
tone) against the readout channel's ``readout_amp``.

THE RATIO IS THE INPUT, ON PURPOSE. One factor array serves every target in a
multiplexed run: q1 with ``pi_amp`` 0.15 and q2 with 0.35 are each driven around
their OWN pi pulse from the same sweep. A single shared ABSOLUTE window cannot do
that, and the narrow-window carriers (``readout_power`` 0.4-1.8x, the two
+/-10% error-amplification scans) would be unusable on any non-uniform chip. So
the parameter stays a ratio, and the absolute amplitude is DERIVED PROVENANCE —
never an input, never something a caller sets.

What this module adds is that provenance: :func:`attach_absolute_amp` writes the
``digital_amp`` coordinate onto the acquired dataset, so a run folder answers
"what amplitude actually played?" on its own. Without it the absolute value
exists only in the fit dict, as ``old_<knob> * factor``, and a saved
``dataset.nc`` cannot be read without separately recovering the device snapshot
from the moment of the run.

``digital_amp`` IS DIMENSIONLESS — the 0-1 normalized amplitude the ``pi_amp`` /
``pi_amp_x90`` / ``readout_amp`` knobs carry (``unit=""`` in ``catalog.py``): a
fraction of full scale on both backends (QM's MW-FEM amplitude is a fraction of
the port's ``full_scale_power_dbm``; Qblox's ``amp180``/``pulse_amp`` are
fractions of DAC full scale). It is NOT volts and NOT dBm — absolute POWER is
``drive_power_dbm`` / ``readout_power_dbm`` and belongs to the punchout
experiments. The name is deliberately the one
``resonator_spectroscopy_power_amp`` already uses for this same quantity.

The coordinate name is shared with the punchout family, but the ATTACH POINT is
not: this one runs in ``Experiment.run()``, so the values reach ``dataset.nc``;
the punchout attaches inside ``estimate()`` onto its local prepared dataset, so
they reach only the figure.

An experiment HAS this capability exactly when its Parameters subclass
:class:`AmplitudeSweepParameters`; the catalog derives the ``"amplitude"``
capability from that subclass relation (never from a declared string). The capability owns
the window Parameters (canonical names + texts), the canonical sweep-axis name
(:data:`AMP_AXIS` — the probe boundary both drivers emit and read), and the
absolute-axis attach.

A carrier declares ONE thing and inherits the rest::

    def amp_reference_field(self) -> str: ...       # the knob the ratio multiplies

``amp_reference_field`` returns a BARE FIELD NAME, never a (channel kind, field)
pair: :meth:`Experiment.anchor` resolves it through ``Roster.resolve_field``
(qubit-closure addressing), and ``catalog.py`` asserts that no field name appears
in two channel-kind catalogs — so ``"pi_amp"`` already means q1's designed
drive channel (``xy1.q1``) and ``"readout_amp"`` its readout channel
(``feedline.q1``). Passing the kind as well would be a
second, unchecked source of truth. It is a METHOD rather than a ClassVar because
``qubit_deterministic_benchmarking`` picks its knob from ``target_gate``.

The axis is ``amp_prefactor`` and not ``amp_factor`` because that is already the
name in scqat (all three amplitude estimators), in the QM probes and in the QM
qualibrate nodes — choosing it DELETES boundary renames instead of adding them,
including a positional-fallback hop in the QM backend's ``_to_canonical``.

THE WINDOW IS A TRAVERSAL ORDER: ``start_amp_factor`` -> ``end_amp_factor``, in
either direction, and the dataset keeps the order the probe walked (decided
2026-09-26) — a strong readout or Stark tone leaves the next point heated, so
which way the amplitude went must be the caller's choice and visible in the data.
scqat's estimators cannot tell the direction, so it never changes a fitted number.
Only a zero-width window is refused (``.._window``), and the bounds hold on BOTH
edges, since either one may be the larger. A carrier that overrides
:meth:`AmplitudeSweepParameters.amp_values` must keep that order too.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

import numpy as np
from pydantic import Field, model_validator

from ...parameters import Parameters
from .._window import refuse_zero_width

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ...experiment import Experiment

#: the canonical swept RATIO axis every amplitude probe emits (dimensionless
#: multiplier of the target's stored amplitude).
AMP_AXIS = "amp_prefactor"

#: the per-target ABSOLUTE amplitude behind each swept ratio point, attached to
#: the acquired dataset as a coordinate over ``(target, AMP_AXIS)``.
ABS_AMP_COORD = "digital_amp"

#: what scqat draws the companion axis under (its ``twin_label``). The knob's own
#: name would be wrong on ``qubit_deterministic_benchmarking``, which calibrates
#: ``pi_amp`` or ``pi_amp_x90`` depending on the benchmarked gate.
ABS_AMP_LABEL = "absolute amplitude (normalized)"

#: canonical field texts — a subclass overriding a DEFAULT re-declares the Field
#: with these constants, so the catalog text can never drift (test-enforced).
START_AMP_FACTOR_DESC = (
    "First drive/readout amplitude of the sweep, as a FACTOR of the target's "
    "currently stored amplitude (1.0 = leave it as calibrated). The probe walks "
    "start_amp_factor -> end_amp_factor IN THAT ORDER, either direction, and the "
    "data keeps it; the fitted result does not depend on it."
)
END_AMP_FACTOR_DESC = (
    "Last amplitude factor of the sweep. May be above or below "
    "start_amp_factor; only a zero-width window (both edges equal) is refused."
)
NUM_AMP_POINTS_DESC = "Number of amplitude points."
#: the ONE carrier that legitimately allows a single point — a degenerate
#: "measure at the current amplitude" mode, not a sweep.
NUM_AMP_POINTS_OPTIONAL_DESC = (
    "Number of amplitude points; 1 measures at the current amplitude only "
    "(no sweep, so no amplitude is fitted)."
)


class AmplitudeSweepParameters(Parameters):
    """Mixin: the swept amplitude window as a FACTOR of the standing amplitude.

    The factor frame is deliberate — see the module docstring. The bounds are
    ``ge=0.0`` and ``lt=2.0`` on BOTH edges (the order is free, so either may be
    the larger): ``2.0`` is the widest window ANY current backend can express
    (QUA's dynamic ``amplitude_scale`` is fixed-point on ``(-2, 2)``); the real,
    device-state-dependent limit is ``factor x stored_amplitude <= 1`` and belongs
    to the drivers, which refuse it BY NAME rather than clipping. A carrier
    re-declaring the edges keeps a bound on both — tighter where its physics
    needs it.
    """

    start_amp_factor: float = Field(0.9, ge=0.0, lt=2.0, description=START_AMP_FACTOR_DESC)
    end_amp_factor: float = Field(1.1, ge=0.0, lt=2.0, description=END_AMP_FACTOR_DESC)
    num_amp_points: int = Field(41, gt=1, description=NUM_AMP_POINTS_DESC)

    @model_validator(mode="after")
    def _amp_window_spans(self) -> "AmplitudeSweepParameters":
        refuse_zero_width(
            self.start_amp_factor, self.end_amp_factor,
            start_name="start_amp_factor", end_name="end_amp_factor",
            points_name="num_amp_points", quantity="amplitude")
        return self

    def amp_values(self) -> np.ndarray:
        """The swept factors, ``start`` -> ``end`` in THAT order. Overridden by a
        carrier with a different rule, which must keep the order it is given."""
        return np.linspace(self.start_amp_factor, self.end_amp_factor,
                           self.num_amp_points)


def amp_sweep(params: AmplitudeSweepParameters) -> dict[str, np.ndarray]:
    """The define_sweep fragment: ``{AMP_AXIS: <the carrier's factors>}``."""
    return {AMP_AXIS: np.asarray(params.amp_values(), dtype=float)}


def amp_anchor(experiment: "Experiment", target: str) -> float:
    """The standing absolute amplitude this target's ratio multiplies.

    Through :meth:`Experiment.anchor`, which already owns "the sweep rides on a
    standing knob": it falls back to ``design.toml`` (tagging the run
    ``seeded:<entity>.<field>``) and otherwise raises a named bring-up
    instruction rather than fitting around garbage.

    Every carrier's ``estimate()`` reads ``old_<knob>`` through this same call,
    so the attached axis and the reported reference can never come from two
    different reads.

    A DRIVE-channel knob resolves through :func:`drive_line.drive_owner`: a
    carrier that names a drive line reads the knob on ``<line>.<target>`` (a
    coupler's borrowed ``xy2.q1_q2_c``), every other read stays the qubit
    shorthand it always was. A readout knob never follows the drive line.
    """
    from ...catalog import CHANNELS
    from .drive_line import drive_owner

    field = experiment.amp_reference_field()
    owner = (drive_owner(experiment, target) if field in CHANNELS["drive"].fields
             else target)
    return experiment.anchor(owner, field)


def absolute_amps(experiment: "Experiment") -> Optional[np.ndarray]:
    """``(n_target, n_amp)`` absolute amplitudes, or ``None`` when none resolved.

    A target whose reference knob cannot be read leaves a NaN row: this is
    provenance, and a measurement that already reached the instrument must never
    die over a decoration. (``estimate()`` does NOT swallow — a fit reported
    against an unknown reference is worse than a failed run.)

    Deliberately NOT the all-or-nothing rule ``_attach_reference_positions``
    uses: that one guards g/e blob-centre PAIRS, where a half-known pair is
    meaningless. A scalar amplitude is independent per target.
    """
    dataset = experiment.dataset
    if dataset is None or AMP_AXIS not in dataset.coords:
        return None
    ratios = np.asarray(dataset.coords[AMP_AXIS].values, dtype=float)
    targets = [str(t) for t in dataset["target"].values]

    values = np.full((len(targets), ratios.size), np.nan)
    for row, target in enumerate(targets):
        try:
            values[row] = ratios * amp_anchor(experiment, target)
        except Exception:  # noqa: BLE001 - provenance only, never fail a run
            continue
    return values if np.isfinite(values).any() else None


def attach_absolute_amp(experiment: "Experiment") -> None:
    """Attach :data:`ABS_AMP_COORD` to the acquired dataset (the ``run()`` hook).

    A COORDINATE, not a data variable — it labels an axis, and ``DatasetContract``
    explicitly permits extra coordinates, so no carrier's contract changes.
    """
    values = absolute_amps(experiment)
    if values is None:
        return
    coord = ("target", AMP_AXIS), values
    experiment.dataset = experiment.dataset.assign_coords({ABS_AMP_COORD: coord})
    experiment.dataset[ABS_AMP_COORD].attrs.update(
        long_name="absolute pulse amplitude",
        units="",  # dimensionless DAC/full-scale fraction — see the module docstring
        reference_field=experiment.amp_reference_field(),
    )
