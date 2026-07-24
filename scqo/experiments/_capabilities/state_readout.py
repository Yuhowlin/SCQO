"""State-readout capability: the FPGA-discriminated averaged ``state`` acquisition mode.

An experiment HAS this capability exactly when its Parameters subclass
:class:`StateReadoutParameters`; the catalog derives the ``"state_readout"`` tag
from that subclass relation (never from a declared string). The capability owns
the Parameters field (one canonical text), the contract fragment (``STATE_ALT``),
the simulate-side draw (:func:`state_row`) and the estimate-side rename
(:func:`signal_rename`).

BOUNDARY RULE: the field name ``use_state_discrimination`` and its semantics
(True => the probe returns ONE averaged ``state`` variable; False => ``I`` + ``Q``)
cross the QM probe boundary — LCHQMDriver shells pass it verbatim to the vendor
``build_program`` — so NEITHER side is ever renamed. LCHQBDriver never reads it
(Qblox probes always emit averaged I/Q, so True is a no-op there).

``attach_readout_positions`` deliberately does NOT ride along: it is an
Experiment ClassVar owned by experiments whose scqat estimator consumes the
stored blob centers (axial IQ reduction) — orthogonal to whether the probe can
return ``state``.
"""

from __future__ import annotations

import numpy as np
import xarray as xr
from pydantic import Field

from ...parameters import Parameters

#: contract fragment: the accepted alternative variable set of a discriminated probe.
STATE_ALT: tuple[tuple[str, ...], ...] = (("state",),)


class StateReadoutParameters(Parameters):
    """Mixin: FPGA state-discriminated acquisition (averaged ``state`` instead of I/Q)."""

    use_state_discrimination: bool = Field(
        False,
        description="Discriminate each shot on the FPGA and return the averaged state "
        "(population) instead of I/Q. Requires a calibrated discriminator "
        "(run single_shot_readout, then accept its readout_rotation_rad / "
        "readout_threshold suggestions).",
    )


def state_row(
    population: np.ndarray, rng: np.random.Generator, *, noise: float = 0.02
) -> np.ndarray:
    """One target's (or one slice's) discriminated readout row: the population
    plus exactly ONE ``rng.normal(0, noise, size)`` draw, clipped to [0, 1].

    The single-draw guarantee is load-bearing: per-target draw ordering (physics
    draws first, then readout draws) is part of the offline sims' stable_seed
    contract — tests replay the stream (SCQO
    ``tests/test_end_to_end.py::test_stored_positions_resolve_power_rabi_axis``).
    """
    population = np.asarray(population, dtype=float)
    return np.clip(population + rng.normal(0.0, noise, population.size), 0.0, 1.0)


def readout_vars(use_state: bool, state, i_data, q_data) -> dict[str, np.ndarray]:
    """The simulate() return: ``{"state": ...}`` in discriminated mode, else ``{"I", "Q"}``."""
    return {"state": state} if use_state else {"I": i_data, "Q": q_data}


def signal_rename(
    ds: xr.Dataset,
    base: dict[str, str] | None = None,
    *,
    iq_fallback: str | None = None,
) -> dict[str, str]:
    """The estimate-side rename dict: ``base`` (sweep-axis renames) merged with
    the signal-source mapping — ``state -> signal`` when the dataset is
    discriminated, else ``iq_fallback -> signal`` for estimators that consume a
    pre-reduced 1-D signal (the flux coherence pair maps ``"I"``).
    ``iq_fallback=None`` leaves I/Q untouched (the coherent-drive family hands
    complex IQ to scqat's axial reduction)."""
    rename = dict(base or {})
    if "state" in ds.data_vars:
        rename["state"] = "signal"
    elif iq_fallback is not None:
        rename[iq_fallback] = "signal"
    return rename
