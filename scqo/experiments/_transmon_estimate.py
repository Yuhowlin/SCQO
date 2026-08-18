"""Predict a transmon's frequency from fabrication numbers — pure math.

WHY THIS EXISTS: a chip's DESIGNED frequency is not where the qubits land.
Fabrication scatter moves them, sometimes by hundreds of MHz, which is enough to
make a bring-up sweep miss the qubit entirely. But the fab measures each
junction's normal-state resistance at the probe station, and that resistance
predicts the Josephson energy — so the as-FABRICATED frequency is knowable before
the qubit has ever answered.

The chain, both steps pure:

    E_JSigma = Delta / (8 e^2 R_n)                    (Ambegaokar-Baratoff)
    f_q_max  = sqrt(8 E_C E_JSigma) - E_C             (transmon, E_J >> E_C)

``R_n`` is the SQUID's measured resistance. Its two junctions sit in PARALLEL at
the probe station, so the resistance yields the TOTAL ``E_JSigma`` directly — the
same quantity ``ej_sum_hz`` names, and exactly what the flux arch needs.

ON ``Delta``: it is the superconducting gap, but treat it as an EFFECTIVE
calibration constant. The room-temperature probe resistance differs from the cold
``R_n`` by 10-30%, and that correction enters the formula identically to Delta
itself (both are a scale on ``Delta / R_n``) — they are exactly degenerate, so ONE
calibrated number covers both. Al is ~180 ueV = 43.5 GHz; fit it per chip once a
few qubits have been measured, and the remaining predictions sharpen from ~400 MHz
to ~50 MHz accurate.

SENSITIVITY: ``f_q ~ sqrt(E_J) ~ 1/sqrt(R_n)``, so ``df/f = -dR/(2R)`` — a 2%
resistance spread is 50 MHz at 5 GHz. That is why this beats a design frequency,
and also why Delta must be calibratable rather than hardcoded.
"""

from __future__ import annotations

import math

#: Effective superconducting gap Delta/h (Hz) when no chip value is known:
#: Al at ~180 ueV. See the module docstring on why this is "effective".
DEFAULT_GAP_DELTA_HZ = 180e-6 * 1.602176634e-19 / 6.62607015e-34  # ~43.5 GHz

#: Elementary charge (C) and Planck constant (J s) — the Ambegaokar-Baratoff
#: prefactor is written out rather than imported so this module stays dependency
#: free (it is imported by experiments that must not drag in scipy).
_E = 1.602176634e-19
_H = 6.62607015e-34


def ej_sum_hz_from_resistance(r_ohm: float, gap_delta_hz: float) -> float:
    """Total Josephson energy E_JSigma/h (Hz) from the normal-state resistance.

    Ambegaokar-Baratoff at T=0: ``I_c = pi Delta / (2 e R_n)`` and
    ``E_J = Phi_0 I_c / (2 pi)``, which reduces to ``E_J/h = Delta/(8 e^2 R_n)``
    with Delta in joules. ``gap_delta_hz`` is Delta/h, so
    ``Delta[J] = h * gap_delta_hz``.

    A SQUID's junctions measure in parallel, so this is the SUM (``ej_sum_hz``),
    not a per-junction value.
    """
    r = float(r_ohm)
    delta_hz = float(gap_delta_hz)
    if not (math.isfinite(r) and r > 0.0):
        raise ValueError(f"junction resistance must be positive and finite, got {r_ohm!r}")
    if not (math.isfinite(delta_hz) and delta_hz > 0.0):
        raise ValueError(f"gap Delta/h must be positive and finite, got {gap_delta_hz!r}")
    return (_H * delta_hz) / (8.0 * _E * _E * r)


def f_q_max_hz_from_ej(ej_sum_hz: float, ec_hz: float) -> float:
    """Sweet-spot qubit frequency (Hz) from E_JSigma and the charging energy.

    The transmon limit ``f_01 = sqrt(8 E_C E_J) - E_C``, evaluated at the arch top
    where the full ``E_JSigma`` is in play. Inverse of the ``ej_sum`` relation the
    flux arch uses, so a value derived here feeds that model self-consistently.
    """
    ej = float(ej_sum_hz)
    ec = float(ec_hz)
    if not (math.isfinite(ej) and ej > 0.0):
        raise ValueError(f"E_JSigma must be positive and finite, got {ej_sum_hz!r}")
    if not (math.isfinite(ec) and ec > 0.0):
        raise ValueError(f"E_C must be positive and finite, got {ec_hz!r}")
    return math.sqrt(8.0 * ec * ej) - ec


def f_q_max_hz_from_resistance(r_ohm: float, ec_hz: float,
                               gap_delta_hz: float = DEFAULT_GAP_DELTA_HZ) -> float:
    """The whole chain: fab resistance -> E_JSigma -> sweet-spot frequency (Hz)."""
    return f_q_max_hz_from_ej(ej_sum_hz_from_resistance(r_ohm, gap_delta_hz), ec_hz)
