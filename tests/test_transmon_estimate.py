"""The pure transmon/coupling math in ``scqo.experiments._transmon_estimate``.

No device, no session — these are closed-form relations, so they are tested
against their own inverses and against the flux model's forward arch.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from scqo.experiments._transmon_estimate import (
    DEFAULT_GAP_DELTA_HZ,
    ej_sum_hz_from_resistance,
    f_q_max_hz_from_ej,
    f_q_max_hz_from_parked,
    f_q_max_hz_from_resistance,
    g_coeff_from_g,
    g_hz_from_coeff,
    g_hz_from_pull,
)


class TestFabricationChain:
    def test_resistance_chain_round_trips_through_ej(self):
        ej = ej_sum_hz_from_resistance(9.0e3, DEFAULT_GAP_DELTA_HZ)
        assert f_q_max_hz_from_resistance(9.0e3, 0.2e9) == pytest.approx(
            f_q_max_hz_from_ej(ej, 0.2e9))

    def test_frequency_scales_as_inverse_sqrt_resistance(self):
        """df/f = -dR/(2R): the sensitivity the module docstring claims."""
        f_lo = f_q_max_hz_from_resistance(9.0e3, 0.2e9)
        f_hi = f_q_max_hz_from_resistance(9.0e3 * 1.02, 0.2e9)
        # +2% resistance -> ~-1% frequency (E_C offset makes it approximate)
        assert -0.012 < (f_hi - f_lo) / f_lo < -0.008

    @pytest.mark.parametrize("bad", [0.0, -1.0, float("nan"), float("inf")])
    def test_nonsense_resistance_refused(self, bad):
        with pytest.raises(ValueError):
            ej_sum_hz_from_resistance(bad, DEFAULT_GAP_DELTA_HZ)


class TestCouplingRelations:
    def test_pull_route_recovers_a_planted_coupling(self):
        """g = sqrt(pull * (f_bare - f_q)) inverts the dispersive pull exactly."""
        g, f_bare, f_q = 90e6, 6.0e9, 4.8e9
        pull = g ** 2 / (f_bare - f_q)
        assert g_hz_from_pull(pull, f_bare, f_q) == pytest.approx(g)

    def test_qubit_above_its_resonator_still_gives_a_real_coupling(self):
        """Both the pull and the detuning flip sign, so the product stays
        positive — the formula needs no special case for that geometry."""
        g, f_bare, f_q = 90e6, 5.0e9, 6.2e9
        pull = g ** 2 / (f_bare - f_q)          # negative: the dip moves DOWN
        assert pull < 0
        assert g_hz_from_pull(pull, f_bare, f_q) == pytest.approx(g)

    def test_pull_disagreeing_with_the_detuning_sign_is_refused(self):
        """A dip that moved the WRONG way for which side the qubit is on is not
        a dispersive pull, so g is undefined rather than imaginary-clamped."""
        with pytest.raises(ValueError, match="OPPOSITE signs"):
            g_hz_from_pull(+8e6, 6.0e9, 6.5e9)   # qubit above, pull up
        with pytest.raises(ValueError, match="OPPOSITE signs"):
            g_hz_from_pull(-8e6, 6.0e9, 4.8e9)   # qubit below, pull down

    def test_coefficient_round_trips_and_is_dimensionless(self):
        g, f_q, f_r = 106.76e6, 5.142e9, 5.9217e9
        c = g_coeff_from_g(g, f_q, f_r)
        assert c == pytest.approx(0.01935, abs=1e-5)   # the real 5Q4C q1 value
        assert g_hz_from_coeff(c, f_q, f_r) == pytest.approx(g)

    def test_the_coefficient_is_what_survives_a_retune(self):
        """THE POINT of storing it: hold the geometry, move the qubit, and the
        predicted g follows sqrt(f_q) while the coefficient does not budge."""
        f_r = 6.0e9
        g_old = g_hz_from_coeff(0.0182, 4.8e9, f_r)
        g_new = g_hz_from_coeff(0.0182, 5.3e9, f_r)
        assert g_new / g_old == pytest.approx(math.sqrt(5.3 / 4.8), rel=1e-9)
        assert g_coeff_from_g(g_new, 5.3e9, f_r) == pytest.approx(0.0182)

    @pytest.mark.parametrize("f_q,f_r", [(0.0, 6e9), (-5e9, 6e9), (5e9, 0.0)])
    def test_nonsense_frequencies_refused(self, f_q, f_r):
        with pytest.raises(ValueError):
            g_coeff_from_g(90e6, f_q, f_r)
        with pytest.raises(ValueError):
            g_hz_from_coeff(0.018, f_q, f_r)


def _forward_arch(f_q_max, ec, idle, offset, period):
    """The arch the scqat dispersive model fits, forward — the inverse target."""
    return (f_q_max + ec) * np.sqrt(
        np.abs(np.cos(np.pi * (idle - offset) / period))) - ec


class TestArchInversion:
    def test_inverts_the_flux_models_own_arch(self):
        f_q_max, ec, offset, period = 5.5e9, 0.2e9, 0.1027, 0.6168
        for idle in (0.1027, 0.09, 0.05, 0.0, 0.2):
            parked = _forward_arch(f_q_max, ec, idle, offset, period)
            assert f_q_max_hz_from_parked(
                parked, ec, idle, offset, period) == pytest.approx(f_q_max)

    def test_at_the_sweet_spot_the_correction_is_exactly_nothing(self):
        f = f_q_max_hz_from_parked(5.142e9, 0.2e9, 0.1027, 0.1027, 0.6168)
        assert f == pytest.approx(5.142e9)

    def test_real_5q4c_park_is_a_sub_mhz_correction(self):
        """q1 parks 0.45 mV off its fitted sweet spot — the guard exists for a
        DELIBERATELY detuned park, not to fix the everyday case."""
        corrected = f_q_max_hz_from_parked(
            5.141980830920033e9, 0.2e9, 0.10220817535401316,
            0.10266298347641434, 0.6167745706920251)
        assert abs(corrected - 5.141980830920033e9) < 1e6

    def test_far_down_the_arch_is_refused_not_extrapolated(self):
        """Near the arch bottom the division amplifies every error without
        bound, so the inversion refuses instead of returning a huge number."""
        with pytest.raises(ValueError, match="too far down the arch"):
            f_q_max_hz_from_parked(1.0e9, 0.2e9, 0.5, 0.0, 1.0)  # half a quantum

    @pytest.mark.parametrize("kwargs", [
        {"f_q_parked_hz": -1.0}, {"ec_hz": 0.0}, {"flux_per_phi0": 0.0},
    ])
    def test_nonsense_inputs_refused(self, kwargs):
        args = dict(f_q_parked_hz=5.0e9, ec_hz=0.2e9, idle_flux=0.1,
                    flux_offset=0.1, flux_per_phi0=0.6)
        args.update(kwargs)
        with pytest.raises(ValueError):
            f_q_max_hz_from_parked(**args)
