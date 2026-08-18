"""Qubit spectroscopy with the drive and the readout tone ON TOGETHER.

:mod:`.qubit_spectroscopy` separates the two in time — saturate, then measure.
This one co-starts them and lets the ADC integrate only after both have been on
for ``acq_start_ns``, so what it records is the qubit line *under measurement
conditions*, in a steady state rather than a decaying one.

Everything but the sequence is the parent's: the same 1-D ``detuning_hz`` sweep,
the same :class:`~scqo.contract.DatasetContract`, the same
``drive_power_boundary`` in ``run()``, and the same ``estimate()`` /
``update()`` — so scqat's ``QubitSpectroscopyEstimator`` is reused verbatim and
the strongest peak recalibrates ``drive_freq_hz`` exactly as it does there.

READ THIS BEFORE ACCEPTING A RESULT. A live readout tone AC-Stark shifts the
qubit, so the peak this finds sits ``chi * n_bar``-ish away from the bare f_01
and the ``drive_freq_hz`` / ``f_01_hz`` it writes back CARRIES THAT SHIFT. That
is inherent to measuring while driving, not a defect: keep ``readout_amp`` low
enough that the shift is under your tolerance, and watch ``peak_detuning_hz`` in
the fit — it is the shift, and if it moves when you change the readout power you
are reading photons, not the qubit. When you want the bare frequency, run
:mod:`.qubit_spectroscopy`.

The timing arithmetic (tone length, ADC onset, drive length, the 4 ns grid) is
:mod:`._overlap`, shared with both driver probes.
"""

from __future__ import annotations

from typing import ClassVar

import numpy as np
from pydantic import Field

from ._overlap import OVERLAP_FIELD_DESCS
from ._sim import stable_seed
from . import register
from .qubit_spectroscopy import QubitSpectroscopy, QubitSpectroscopyParameters


class QubitSpectroscopyOverlapParameters(QubitSpectroscopyParameters):
    """Inputs for a concurrent-tone (drive + readout together) two-tone
    measurement. Everything the parent takes, plus when the ADC opens."""

    acq_start_ns: float = Field(0.0, ge=0, description=OVERLAP_FIELD_DESCS["acq_start_ns"])
    # redeclared: on this experiment the length is meaningful on BOTH backends
    # (it bounds the concurrent window), and None means "the whole readout tone"
    # rather than the parent's "the backend's configured length".
    drive_len_ns: float | None = Field(
        None, gt=0, description=OVERLAP_FIELD_DESCS["drive_len_ns"]
    )


@register
class QubitSpectroscopyOverlap(QubitSpectroscopy):
    """Backend-agnostic concurrent-tone two-tone. ``probe()`` is supplied by a
    driver; every other half is the parent's."""

    name: ClassVar[str] = "qubit_spectroscopy_overlap"
    description: ClassVar[str] = (
        "Two-tone spectroscopy with the saturation drive and the readout tone "
        "started TOGETHER, the ADC opening only after both have been on for "
        "acq_start_ns so it integrates a steady state. Recalibrates the drive "
        "channel's drive_freq_hz from the strongest peak like qubit_spectroscopy, "
        "and is faster (no drive-then-readout dead time) — but the live readout "
        "tone AC-Stark shifts the qubit, so the frequency it writes back carries "
        "that shift unless readout_amp is kept low. Use qubit_spectroscopy for "
        "the bare f_01; use this one to see the line under measurement conditions."
    )
    Parameters: ClassVar[type] = QubitSpectroscopyOverlapParameters

    params: QubitSpectroscopyOverlapParameters

    def simulate(self, coords: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
        """The parent's Lorentzian, pulled by a Stark shift and broadened.

        The shift and the extra width scale with ``acq_start_ns`` only through
        the hidden per-qubit truth, not physically — this is an offline
        placeholder, so it just has to be deterministic, distinguishable from
        the parent's data, and land inside the ``[start_detuning_hz,
        end_detuning_hz]`` window (``estimate()`` rejects a peak outside it).
        """
        detuning = coords["detuning_hz"]
        targets = self.params.targets
        rng = np.random.default_rng(stable_seed("qubit_spectroscopy_overlap", *targets))
        width = self.params.end_detuning_hz - self.params.start_detuning_hz
        window_mid = (self.params.start_detuning_hz + self.params.end_detuning_hz) / 2
        i_data = np.empty((len(targets), detuning.size))
        q_data = np.empty_like(i_data)
        for k in range(len(targets)):
            err = window_mid + rng.uniform(-0.2, 0.2) * width  # hidden truth, in-window
            # measuring while driving: the line sits low and is dephasing-broadened
            stark = -rng.uniform(0.02, 0.08) * width
            fwhm = rng.uniform(2e6, 5e6) * rng.uniform(1.5, 2.5)
            centre = err + stark
            peak = 0.5 * (fwhm / 2) ** 2 / ((detuning - centre) ** 2 + (fwhm / 2) ** 2)
            noise = 0.02
            i_data[k] = peak + rng.normal(0, noise, detuning.size)
            q_data[k] = rng.normal(0, noise, detuning.size)
        return {"I": i_data, "Q": q_data}

    def probe(self):  # pragma: no cover - driver half
        raise NotImplementedError("a driver backend supplies probe()")
