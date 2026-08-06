"""The resonator-depletion wait: THE precedence point, and its one canonical text.

The readout twin of ``_capabilities/qubit_reset.py``, deliberately built to the
same shape because it is the same logic one level over:

===================  ==============================  ==============================
                     thermal reset                   resonator depletion
===================  ==============================  ==============================
measured FACT        ``t1_s`` (qubit mode)           ``kappa_tot_hz`` (resonator)
measured by          ``qubit_relaxation``            ``resonator_spectroscopy``
per-run FACTOR       ``thermalization_factor`` 10    ``depletion_factor`` 10
proposed KNOB        ``thermalization_time_s``       ``readout_depletion_s``
   ... on            the DRIVE channel (q1_xy)       the READOUT channel (q1_ro)
per-run override     ``thermalization_time_ns``      ``readout_depletion_ns``
precedence helper    ``reset_wait_ns``               :func:`depletion_wait_ns`
===================  ==============================  ==============================

WHY A KNOB AND NOT A PARAMETER: the wait is a property of the resonator and the
readout condition, identical for every experiment that measures it, and it is
realized by a real vendor field on both backends (QM
``q.resonator.depletion_time``, Qblox ``element.depletion.duration``). That is
placement-rule step (4), the same step that makes the thermal wait a knob while
``t1_s`` stays a fact. It was previously TWO per-run Parameters fields that could
disagree with each other and with the vendor.

THE FACTOR IS A CHOICE, THE LINEWIDTH IS A FACT. ``kappa_tot_hz`` is the
power-Lorentzian FWHM in Hz, so the photon-number decay time is
``1 / (2 pi x kappa_tot_hz)`` and the wait is ``depletion_factor`` of those.
10 leaves e^-10 ~ 0.005% of the photons. At kappa/2pi = 1 MHz that is 1592 ns.

WHY THIS RETURNS None RATHER THAN RAISING, unlike ``reset_wait_ns``: its two
callers have legitimately different policies for "never calibrated". Active reset
REFUSES (a missing settle silently biases the next pulse, which shows up as a
fitted-frequency error nobody attributes to the reset). The punchout probe falls
back to its own built-in idle, because it worked that way before this knob
existed and a bring-up sweep must not require a calibration it is being run to
inform. Making the caller state its policy keeps both visible.
"""

from __future__ import annotations

import math

#: canonical field text for the per-run override (the knob's own text lives in
#: catalog.py). Re-declared by every experiment that offers it, so the catalog
#: description cannot drift per-experiment.
READOUT_DEPLETION_NS_DESC = (
    "Per-run override of the resonator-depletion wait, ns. None = use the "
    "standing readout_depletion_s knob on the target's readout channel (the "
    "normal case), which resonator_spectroscopy proposes from the measured "
    "linewidth; set it to try a different wait for THIS run only, without "
    "disturbing device state."
)

#: canonical field text for the factor that turns the measured linewidth into
#: the proposed knob — the depletion twin of thermalization_factor.
DEPLETION_FACTOR_DESC = (
    "Multiple of the resonator's photon lifetime 1 / (2 pi x kappa_tot_hz) "
    "proposed as each target's readout_depletion_s knob (the wait every other "
    "experiment then leaves between a readout and the next pulse). 10 leaves "
    "~0.005% of the photons. This run's OWN readouts still use the standing "
    "knob — the proposal takes effect once accepted."
)


def depletion_time_s(kappa_tot_hz: float, factor: float) -> float:
    """The proposed wait, seconds, from the measured linewidth.

    ``kappa_tot_hz`` is the power-Lorentzian FWHM (catalog.py), so the
    photon-number decay time is its inverse angular linewidth.
    """
    return float(factor) / (2.0 * math.pi * float(kappa_tot_hz))


def depletion_wait_ns(experiment, target: str) -> float | None:
    """The resolved depletion wait for one target in ns, or None if there is no
    governed value yet.

    THE one precedence point, called by every driver probe that needs the wait:
    the per-run ``readout_depletion_ns`` override when set, else the standing
    ``readout_depletion_s`` knob on the target's readout channel. ``None`` means
    neither exists — see the module docstring for why that is returned rather
    than raised.
    """
    override = getattr(experiment.params, "readout_depletion_ns", None)
    if override is not None:
        return float(override)
    standing = experiment.device.channel(target, "readout").readout_depletion_s
    if standing is None or math.isnan(standing):
        return None
    return float(standing) * 1e9
