"""AC-Stark phase echo — the phase an off-resonant Stark pulse imprints on a qubit.

A Hahn echo whose SECOND free-evolution arm is filled by an off-resonant
AC-Stark tone::

    Y90 - wait(D) - X180 - [stark tone, duration D, off-resonant] - {X90 | -Y90} - readout
          arm 1 (idle)          arm 2 (stark)

Both arms have the same length ``D`` = the registered ``stark`` operation's OWN
length (arm 1's idle is derived from it, and the tone fully replaces the free
evolution in arm 2 — no dynamic-duration override, so an arbitrary stark waveform
is never zero-padded). The central X180 refocuses the qubit's static detuning
(arm-1 idle phase cancels arm-2's), so the ONLY surviving phase is the AC-Stark
shift accumulated in arm 2, ``phi = delta_stark * D`` — cleanly separated from
ordinary dephasing. To change ``D``, re-register the stark op at a new length.

The phase is read out in TWO measurement bases (``meas_basis`` axis, 2 points),
so it is recovered unambiguously (a single closing X90 sees only ``cos phi``,
blind near 0):

* ``meas_basis = 0`` — close with ``X90``  -> <Z> = sin(phi)  (the Y quadrature)
* ``meas_basis = 1`` — close with ``-Y90`` -> <Z> = cos(phi)  (the X quadrature)

so ``phi = atan2(sin, cos)``. Only the tone's AMPLITUDE is swept
(``stark_amp``); the detuning is a per-run scalar and the duration is a property
of the registered stark op. The AC-Stark phase is quadratic in drive amplitude,
``phi ~ k * amp**2``, and the scqat estimator fits that -> the Stark coefficient ``k``.

The prep is Y90 (not X90): it starts the equatorial state on +x, aligned with the
closing bases' phi=0 reference, so at amp=0 the prepared phase is exactly 0 and the
measured absolute phase is the Stark-induced phase.

RECORD-ONLY: there is no ``update()`` and nothing lands on the device surface;
the fitted coefficient and the per-amplitude phase live in ``result.fit``.
``use_state_discrimination`` returns the FPGA-discriminated averaged population
instead of I/Q (needs a calibrated discriminator).
"""

from __future__ import annotations

from typing import ClassVar

import numpy as np
from pydantic import Field

from .._scqat import per_qubit_results
from ..contract import DatasetContract
from ._capabilities.qubit_reset import QubitResetParameters
from ._capabilities.state_readout import (
    POPULATION_ALT,
    StateReadoutParameters,
    population_row,
    readout_vars,
    signal_rename,
)
from ._sim import iq_from_population, stable_seed
from ..parameters import AveragingParameters, TargetSelection
from ..result import Outcome, Result
from ..experiment import Experiment
from . import register


class QubitStarkPhaseEchoParameters(TargetSelection, AveragingParameters,
                                    StateReadoutParameters, QubitResetParameters):
    """Inputs for the AC-Stark phase echo. Amplitude is swept; detuning + duration are fixed."""

    stark_operation: str = Field(
        "stark",
        description="The named XY (RF) operation played in the second echo arm to induce "
                    "the AC-Stark shift; its baked amplitude is the reference the swept factor "
                    "multiplies. NOT x180 — a dedicated off-resonant tone (the driver refuses "
                    "by name if the operation is missing; register it via register_stark.py).")
    stark_detuning_hz: float = Field(
        50e6,
        description="FIXED off-resonant detuning (Hz) of the stark tone from the qubit drive "
                    "frequency. Must be off-resonant for a genuine Stark shift (a resonant tone "
                    "drives Rabi rotations instead); tune per chip — far enough to avoid driving "
                    "a transition, near enough for a usable shift. Set per run; not a sweep axis.")
    min_stark_amp: float = Field(
        0.0,
        description="Lowest AC-Stark drive amplitude, as a dimensionless FACTOR of the stark "
                    "operation's baked amplitude (the QUA amplitude_scale). Keep at ~0: the "
                    "estimator anchors phi=0 at the smallest amplitude (no drive => no phase).")
    max_stark_amp: float = Field(
        1.0, description="Highest AC-Stark drive amplitude factor.")
    num_amp_points: int = Field(
        21, gt=4, description="Number of stark-amplitude points.")


class QubitStarkPhaseEchoResult(Result):
    """``fit[target]``: ``stark_coeff_rad_per_amp2`` (phase per amp², the Stark
    coefficient) and ``intercept_rad``. Record-only: no ``update()``, nothing
    written to the device."""


@register
class QubitStarkPhaseEcho(Experiment):
    """Backend-agnostic AC-Stark phase echo. ``probe()`` is supplied by a driver."""

    name: ClassVar[str] = "qubit_stark_phase_echo"
    description: ClassVar[str] = (
        "AC-Stark phase echo: a Hahn echo (X90 - wait(D) - X180 - stark(D) - close - readout) "
        "with an off-resonant Stark tone filling the second free-evolution arm (the first arm is "
        "an idle of the same duration D). The echo refocuses static dephasing, so the surviving "
        "phase is the AC-Stark shift the tone imprints. The phase is read in two bases (close with "
        "X90 -> sin(phi), -Y90 -> cos(phi)) so it is unambiguous, and only the tone's amplitude is "
        "swept; the estimator fits phi vs amp^2 to the Stark coefficient. Record-only diagnostic: "
        "the coefficient lands in result.fit and nothing is written back to the device. "
        "use_state_discrimination returns the FPGA-discriminated averaged population instead of I/Q "
        "(needs a calibrated discriminator)."
    )
    Parameters: ClassVar[type] = QubitStarkPhaseEchoParameters
    Result: ClassVar[type] = QubitStarkPhaseEchoResult
    Contract: ClassVar[DatasetContract] = DatasetContract(
        sweeps=("stark_amp", "meas_basis"), sweep_units=("", ""),
        variables=("I", "Q"), alt_variables=POPULATION_ALT,
    )
    required_operations: ClassVar[tuple[str, ...]] = ("rx", "readout")
    #: stored blob centers ride the dataset -> the estimator's pooled axial axis
    #: is the measured g->e vector (else it falls back to PCA).
    attach_readout_positions: ClassVar[bool] = True

    params: QubitStarkPhaseEchoParameters

    def define_sweep(self) -> dict[str, np.ndarray]:
        return {
            # dict order IS the contract order: stark amplitude outer, basis inner.
            "stark_amp": np.linspace(self.params.min_stark_amp,
                                     self.params.max_stark_amp,
                                     self.params.num_amp_points),
            # 0 = close with x90 (reads sin phi); 1 = close with -y90 (reads cos phi).
            "meas_basis": np.array([0, 1]),
        }

    def simulate(self, coords: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
        """A quadratic Stark phase phi(a) = k*a^2 read out in the two bases.

        Basis 0 (x90) measures sin(phi), basis 1 (-y90) measures cos(phi). The two
        bases of ONE target are placed in the SAME IQ frame (one flattened
        iq_from_population call), as a shared readout would.
        """
        amp = np.asarray(coords["stark_amp"], dtype=float)
        basis = np.asarray(coords["meas_basis"])
        na, nb = amp.size, basis.size
        targets = self.params.targets
        rng = np.random.default_rng(stable_seed("qubit_stark_phase_echo", *targets))
        use_state = self.params.use_state_discrimination
        i_data = np.empty((len(targets), na, nb))
        q_data = np.empty_like(i_data)
        pop = np.empty_like(i_data)
        for k in range(len(targets)):
            k_coeff = rng.uniform(2.0, 8.0)  # hidden phase-per-amp^2 the fit must recover
            phi = k_coeff * amp ** 2
            comp = np.empty((na, nb))
            comp[:, 0] = np.sin(phi)  # meas_basis 0: x90  -> <Z> = sin(phi)
            comp[:, 1] = np.cos(phi)  # meas_basis 1: -y90 -> <Z> = cos(phi)
            population = 0.5 * (1.0 - comp)
            if use_state:
                pop[k] = population_row(population.reshape(-1), rng).reshape(na, nb)
            else:
                i_flat, q_flat = iq_from_population(population.reshape(-1), rng)
                i_data[k] = i_flat.reshape(na, nb)
                q_data[k] = q_flat.reshape(na, nb)
        return readout_vars(use_state, pop, i_data, q_data)

    def estimate(self) -> QubitStarkPhaseEchoResult:
        assert self.dataset is not None
        from scqat.estimators.qubit_stark_phase_echo import QubitStarkPhaseEchoEstimator

        # scqat's contract: coords stark_amp + meas_basis, and either a real
        # `signal` (discriminated) or complex I/Q (reduced by the estimator).
        prepared = self.dataset.rename(signal_rename(self.dataset))
        results = per_qubit_results(prepared, QubitStarkPhaseEchoEstimator(),
                                    artifact_dir=self.artifact_dir)

        result = QubitStarkPhaseEchoResult()
        for target in self.params.targets:
            r = results[target]
            result.fit[target] = {
                "stark_coeff_rad_per_amp2": float(r["stark_coeff"]),
                "intercept_rad": float(r["intercept"]),
            }
            result.outcomes[target] = Outcome.SUCCESSFUL if bool(r["success"]) else Outcome.FAILED
        return result

    def probe(self):  # pragma: no cover - driver half
        raise NotImplementedError("a driver backend supplies probe()")
