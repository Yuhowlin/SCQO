"""The Stark-tone timing of ``qubit_resonator_stark``: THE one point, both backends.

The sequence is one sentence on every backend:

    A TONE ON THE READOUT CHANNEL STARTS ``ring_up_ns`` BEFORE THE SATURATION
    DRIVE AND ENDS WITH IT; THE STANDARD READOUT STARTS ``depletion_ns`` LATER.

    readout ch : [==== Stark tone: amp_prefactor x readout_amp ====]              [## readout ##]
    drive ch   :               [======= saturation drive ===========]
                 |<- ring_up ->|<----------- drive_len_ns --------->|<-depletion->|

The tone fills the readout resonator with photons that AC-Stark shift the
qubit; the drive probes the shifted line only once the photon number is steady,
and the readout comes after the photons have gone, so every amplitude row is
read with the same readout. That is the point of a separate tone: sweeping the
readout pulse itself would change the measurement along with the shift.

RING-UP AND DEPLETION ARE ONE NUMBER — the resolved depletion wait
(:func:`._depletion.depletion_wait_ns`: the per-run ``readout_depletion_ns``
override, else the readout channel's standing ``readout_depletion_s`` knob).
Filling and emptying are both set by the resonator's photon lifetime
``1 / (2 pi kappa_tot)``, and the knob IS ``depletion_factor`` of those: at the
default factor 10 the emptied resonator keeps e^-10 of its photons, and the
filling one reaches (1 - e^-5)^2 ~ 98.7% of its steady state before the drive
starts. One number, one precedence point, no second field that could disagree.

NEVER CALIBRATED IS REFUSED BY NAME. The photons must be gone before the
readout, and a wait nobody governed is a guess: a knob that reads None or NaN,
or one the vendor never seeded at all (``depletion_wait_ns`` raises
``KeyError`` then), refuses the run, naming the targets and the remedy. The QM
factory default (QUAM's 16 ns, which is never None) is refused by the QM probe —
the vendor-specific half of the same rule.

ONE SET OF TIMES PER RUN. Both backends play every target the same timing, so
the wait is the LONGEST of the targets' (waiting longer only costs time), ROUNDED
UP to the 4 ns grid, and a non-zero wait is at least :data:`MIN_WAIT_NS` — QM's
``wait()`` takes at least 4 clock cycles and the finer Qblox grid accepts any
multiple of 4 ns. Rounding here, ONCE, is what keeps the two backends from each
rounding their own way (the reason :mod:`._overlap` refuses off-grid inputs).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from ._depletion import depletion_wait_ns
from ._overlap import GRID_NS

#: the shortest non-zero wait both backends can play (QM: 4 clock cycles).
MIN_WAIT_NS = 16.0

_MISSING_DEPLETION = (
    "{experiment}: no governed depletion wait for {targets}. The Stark tone's "
    "photons must leave the resonator before the readout (and the same wait is "
    "the tone's ring-up lead), so it has to be a governed value, not a guess. Run "
    "resonator_spectroscopy on {targets} and accept its readout_depletion_s "
    "proposal, or pass readout_depletion_ns= for this run (0 is legal and means "
    "no wait)."
)


@dataclass(frozen=True)
class StarkWindows:
    """The run's resolved timing, all in ns. Probes play these numbers directly
    and derive nothing themselves."""

    #: how long the Stark tone runs before the saturation drive starts
    ring_up_ns: float
    #: saturation-drive length, straight from the params
    drive_len_ns: float
    #: total Stark-tone length = ring_up_ns + drive_len_ns (the two end together)
    tone_len_ns: float
    #: from the end of the tone (and the drive) to the start of the readout
    depletion_ns: float


def playable_wait_ns(value_ns: float) -> float:
    """``value_ns`` rounded UP to the grid; a non-zero wait shorter than
    :data:`MIN_WAIT_NS` becomes :data:`MIN_WAIT_NS`, and zero stays zero."""
    if value_ns <= 0:
        return 0.0
    # the tolerance keeps float noise on an on-grid value from rounding it up a step
    snapped = math.ceil(value_ns / GRID_NS - 1e-6) * GRID_NS
    return float(max(MIN_WAIT_NS, snapped))


def stark_windows(experiment) -> StarkWindows:
    """Resolve the run's Stark-tone timing, refusing an ungoverned depletion.

    THE one precedence point: ``QubitResonatorStark.define_sweep`` calls it, so a
    refusal lands before the drive-power boundary and before any instrument time,
    and both driver probes play what it returned.
    """
    name = getattr(type(experiment), "name", type(experiment).__name__)
    waits: dict[str, float] = {}
    missing: list[str] = []
    for target in experiment.params.targets:
        try:
            wait = depletion_wait_ns(experiment, target)
        except KeyError:  # the vendor never seeded the knob — same remedy as NaN
            wait = None
        if wait is None or not math.isfinite(wait) or wait < 0:
            missing.append(target)
        else:
            waits[target] = float(wait)
    if missing:
        raise ValueError(_MISSING_DEPLETION.format(
            experiment=name, targets=", ".join(missing)))
    wait_ns = playable_wait_ns(max(waits.values()))
    drive_ns = float(experiment.params.drive_len_ns)
    return StarkWindows(
        ring_up_ns=wait_ns,
        drive_len_ns=drive_ns,
        tone_len_ns=wait_ns + drive_ns,
        depletion_ns=wait_ns,
    )
