"""Transmon frequency and qubit-resonator coupling relations — pure math.

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

THE COUPLING RELATIONS (second half of this module)
--------------------------------------------------
``g = c * sqrt(f_q * f_r)``, where ``c`` is a dimensionless GEOMETRY constant
(capacitance ratios) and the frequencies are wherever the two modes actually sit.
So ``g`` itself is only valid at one operating point, while ``c`` survives
re-tuning, re-parking and cooldowns — which is why the catalog records both
(``g_hz`` and ``g_coeff`` on the resonator mode).

.. warning::
   ``sqrt(f_q * f_r)`` holds BETWEEN transmon-regime operating points. It must
   NOT be extrapolated down a flux arch: applying ``g(phi) ~ sqrt(f_q(phi))``
   inside the dispersive flux model was tested against 13 real target-runs on
   2026-08-18 and REFUTED — the measured pull at the arch bottom does not
   vanish the way the scaling predicts (as E_J collapses the device leaves the
   transmon regime and the charge matrix element stops following sqrt(f_q)).
   See ``RELEASES.d/flux-design-g-seed-rescale.toml``. Rescale a coupling
   BETWEEN sweet-spot-like operating points, never along the arch.
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


#: Smallest |cos| the arch inversion will divide by. Below this the qubit is far
#: down the arch, where dividing amplifies both the measurement error and the
#: model error without bound (and the transmon approximation is failing anyway).
_MIN_ARCH_COS = 0.05


def g_hz_from_pull(pull_hz: float, f_bare_hz: float, f_q_hz: float) -> float:
    """Coupling g (Hz) from a measured dispersive pull.

    The dispersive model gives ``pull = f_dressed - f_bare = g^2/(f_bare - f_q)``,
    so ``g = sqrt(pull * (f_bare - f_q))``. This is the PUNCHOUT route: the pull
    is the Lamb shift the punchout measured directly (``f_dress0 - f_bare``, both
    from the same run) and ``f_q`` is the qubit frequency at the flux that run
    sat at, so nothing here depends on a flux-arch fit.

    SIGN-SAFE BY CONSTRUCTION: ``pull`` and ``(f_bare - f_q)`` always share a
    sign under the model — a qubit ABOVE its resonator flips both — so the
    product is non-negative whenever the data is consistent with a dispersive
    pull. A NEGATIVE product means it is not (the dip moved the wrong way for
    which side the qubit is on), and that is a physically meaningful refusal
    rather than an edge case to clamp away.
    """
    pull = float(pull_hz)
    detuning = float(f_bare_hz) - float(f_q_hz)
    if not (math.isfinite(pull) and math.isfinite(detuning)):
        raise ValueError(
            f"pull and detuning must be finite, got pull={pull_hz!r}, "
            f"f_bare={f_bare_hz!r}, f_q={f_q_hz!r}")
    product = pull * detuning
    if product < 0.0:
        raise ValueError(
            f"pull ({pull:.6g} Hz) and (f_bare - f_q) ({detuning:.6g} Hz) have "
            f"OPPOSITE signs — the dip moved the wrong way for a qubit on that "
            f"side of its resonator, so this is not a dispersive pull and g is "
            f"undefined")
    return math.sqrt(product)


def g_coeff_from_g(g_hz: float, f_q_hz: float, f_r_hz: float) -> float:
    """The dimensionless geometry coefficient ``c = g / sqrt(f_q * f_r)``.

    ``f_r`` is the BARE resonator (the uncoupled mode) and ``f_q`` the qubit
    frequency g was measured at. Using the dressed resonator instead shifts the
    result by the Lamb shift under a square root — ~0.1% — but bare is the
    physically right choice and the one the catalog stores.
    """
    g = float(g_hz)
    product = float(f_q_hz) * float(f_r_hz)
    if not (math.isfinite(g) and g >= 0.0):
        raise ValueError(f"g must be finite and non-negative, got {g_hz!r}")
    if not (math.isfinite(product) and product > 0.0):
        raise ValueError(
            f"f_q * f_r must be positive and finite, got f_q={f_q_hz!r}, f_r={f_r_hz!r}")
    return g / math.sqrt(product)


def g_hz_from_coeff(coeff: float, f_q_hz: float, f_r_hz: float) -> float:
    """Coupling g (Hz) predicted at an operating point from the coefficient.

    The inverse of :func:`g_coeff_from_g`. Valid BETWEEN transmon-regime
    operating points — see the module warning on not extrapolating down an arch.
    """
    c = float(coeff)
    product = float(f_q_hz) * float(f_r_hz)
    if not (math.isfinite(c) and c >= 0.0):
        raise ValueError(f"coefficient must be finite and non-negative, got {coeff!r}")
    if not (math.isfinite(product) and product > 0.0):
        raise ValueError(
            f"f_q * f_r must be positive and finite, got f_q={f_q_hz!r}, f_r={f_r_hz!r}")
    return c * math.sqrt(product)


def f_q_max_hz_from_parked(f_q_parked_hz: float, ec_hz: float, idle_flux: float,
                           flux_offset: float, flux_per_phi0: float) -> float:
    """Arch TOP (Hz) from a qubit frequency measured at an arbitrary parked flux.

    Inverts the symmetric-SQUID arch the flux model uses forward,
    ``f_q = (f_q_max + E_C) sqrt(|cos(pi (phi - phi_off)/phi0)|) - E_C``::

        f_q_max = (f_q_parked + E_C) / sqrt(|cos(...)|) - E_C

    A standing ``drive_freq_hz`` is the qubit frequency AT THE PARKED FLUX, which
    equals ``f_q_max`` only when parked at the sweet spot. Whenever the arch
    (``flux_offset`` + ``flux_per_phi0``) is known this removes that assumption;
    at the sweet spot the cosine is 1 and the correction is exactly nothing.

    Refuses below ``_MIN_ARCH_COS``: far down the arch the division amplifies
    every error without bound.
    """
    f_parked = float(f_q_parked_hz)
    ec = float(ec_hz)
    period = float(flux_per_phi0)
    if not (math.isfinite(f_parked) and f_parked > 0.0):
        raise ValueError(f"parked qubit frequency must be positive, got {f_q_parked_hz!r}")
    if not (math.isfinite(ec) and ec > 0.0):
        raise ValueError(f"E_C must be positive and finite, got {ec_hz!r}")
    if not (math.isfinite(period) and period != 0.0):
        raise ValueError(f"flux_per_phi0 must be non-zero and finite, got {flux_per_phi0!r}")
    quantum = (float(idle_flux) - float(flux_offset)) / period
    cos_term = abs(math.cos(math.pi * quantum))
    if not (math.isfinite(cos_term) and cos_term > _MIN_ARCH_COS):
        raise ValueError(
            f"parked {quantum:.4g} flux quanta from the sweet spot (|cos| = "
            f"{cos_term:.4g} <= {_MIN_ARCH_COS}) — too far down the arch to "
            f"invert; the arch top cannot be recovered from here")
    return (f_parked + ec) / math.sqrt(cos_term) - ec
