"""Qubit echo vs flux PULSE — T2_echo spectrum over a swept z excursion, greenfield.

Port of :mod:`scqo.experiments.qubit_echo_flux`. The physics half is
byte-for-byte; this is a record-only diagnostic (no ``update()`` — the
per-flux T2_echo fits live in the run record), so what moved is only the
import surface (capability helpers from their current locations) and the
kind-based gating inherited from the greenfield base.

FRAME (``_pulse`` in the name, ``FluxPulseSweepParameters`` in the schema):
the z bias is a PULSE played inside the echo and the DAC adds it to the
standing bias, so the window is measured from the channel's ``idle_flux`` and
0 means "sit at the operating point". See :mod:`._capabilities.flux`.
"""

from __future__ import annotations

from typing import ClassVar

import numpy as np
from pydantic import Field

from ..contract import DatasetContract
from ._capabilities.flux import (
    MAX_FLUX_PULSE_DESC,
    MIN_FLUX_PULSE_DESC,
    FluxPulseSweepParameters,
    flux_anchor_v,
    flux_sweep,
)
from ._capabilities.qubit_reset import QubitResetParameters
from ._capabilities.state_readout import (
    STATE_ALT,
    StateReadoutParameters,
    readout_vars,
    signal_rename,
    state_row,
)
from ._sim import stable_seed
from ._time_grid import time_axis_ns
from ..parameters import AveragingParameters, TargetSelection
from ..result import Outcome, Result
from ..experiment import Experiment
from . import register


class QubitEchoFluxPulseParameters(
    TargetSelection, AveragingParameters, StateReadoutParameters, FluxPulseSweepParameters, QubitResetParameters
):
    """Parameters for T2 echo vs flux-pulse spectroscopy (window relative to idle_flux)."""

    min_wait_ns: float = Field(32.0, ge=0.0, description="Minimum idle delay (total tau).")
    max_wait_ns: float = Field(40000.0, gt=0.0, description="Maximum idle delay (total tau).")
    num_wait_points: int = Field(51, gt=1, description="Number of wait time points.")
    # capability defaults narrowed to the coherence window (canonical text constants)
    min_flux_v: float = Field(-0.08, ge=-0.5, description=MIN_FLUX_PULSE_DESC)
    max_flux_v: float = Field(0.08, le=0.5, description=MAX_FLUX_PULSE_DESC)


class QubitEchoFluxPulseResult(Result):
    """Fitted T2 echo spectrum results.

    ``fit[target]``: per-flux lists (``flux_bias_v``, ``t2_echo``, ...) plus the
    scalar ``old_idle_flux`` — the standing bias the swept excursion rode on.
    """


@register
class QubitEchoFluxPulse(Experiment):
    """Measure qubit Hahn echo coherence T2_echo vs Z flux PULSE amplitude (idle-relative)."""

    name: ClassVar[str] = "qubit_echo_flux_pulse"
    description: ClassVar[str] = (
        "Sweep a Z PULSE amplitude — RELATIVE to the flux channel's idle_flux, "
        "0 = stay parked — and a total wait delay in a Hahn echo sequence, "
        "fitting T2_echo decay at each flux point to map out the T2_echo "
        "spectrum. Record-only: the fits are saved, nothing is proposed."
    )
    Parameters: ClassVar[type] = QubitEchoFluxPulseParameters
    Result: ClassVar[type] = QubitEchoFluxPulseResult
    Contract: ClassVar[DatasetContract] = DatasetContract(
        sweeps=("flux_bias_v", "wait_time_ns"),
        sweep_units=("V", "ns"),
        variables=("I", "Q"),
        alt_variables=STATE_ALT,
    )

    params: QubitEchoFluxPulseParameters

    required_operations: ClassVar[tuple[str, ...]] = ("rx", "readout", "flux_bias")

    def define_sweep(self) -> dict[str, np.ndarray]:
        wait_time = time_axis_ns(
            self.params.min_wait_ns,
            self.params.max_wait_ns,
            self.params.num_wait_points,
            grid_ns=2,  # total idle splits into two equal echo arms
        )
        return {**flux_sweep(self.params), "wait_time_ns": wait_time}

    def simulate(self, coords: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
        """Generate synthetic Hahn echo decay curves with a simulated TLS defect dip."""
        flux = coords["flux_bias_v"]
        wait = coords["wait_time_ns"]
        qubits = self.params.targets

        n_qubits = len(qubits)
        n_flux = len(flux)
        n_wait = len(wait)

        i_data = np.zeros((n_qubits, n_flux, n_wait))
        q_data = np.zeros((n_qubits, n_flux, n_wait))
        state = np.zeros_like(i_data)

        use_state = self.params.use_state_discrimination
        rng = np.random.default_rng(stable_seed("qubit_echo_flux_pulse", *qubits))
        for k in range(n_qubits):
            for f_idx, f_amp in enumerate(flux):
                # T2_echo: best at f_amp=0 (T2_echo ~ 50 us), which under the
                # idle-relative frame IS the sweet spot the qubit is parked at.
                # TLS defect dip 30 mV OFF idle
                t2e_baseline = 50e-6 * (1.0 - 0.4 * (f_amp ** 2))
                tls_dip = 30e-6 * np.exp(-((f_amp - 0.03) / 0.008) ** 2)
                t2e = max(t2e_baseline - tls_dip, 1e-6)

                decay = np.exp(-(wait * 1e-9) / t2e)
                noise = 0.02
                if use_state:
                    state[k, f_idx] = state_row(decay, rng)
                else:
                    i_data[k, f_idx] = decay + rng.normal(0, noise, n_wait)
                    q_data[k, f_idx] = rng.normal(0, noise, n_wait)

        return readout_vars(use_state, state, i_data, q_data)

    def estimate(self) -> QubitEchoFluxPulseResult:
        assert self.dataset is not None, "run() populates self.dataset before estimate()"
        # scqat is NOT renamed — the estimator is the same fit either frame.
        from scqat.estimators.qubit_echo_flux import QubitEchoFluxEstimator
        from .._scqat import per_qubit_results

        # scqat's contract: variable `signal` + coords `flux_bias` & `wait_time` (seconds).
        # A discriminated probe returns the averaged `state`; otherwise the raw I
        # quadrature is the pre-reduced signal.
        rename = signal_rename(
            self.dataset,
            {"wait_time_ns": "wait_time", "flux_bias_v": "flux_bias"},
            iq_fallback="I",
        )
        prepared = self.dataset.rename(rename)
        prepared = prepared.assign_coords(wait_time=prepared["wait_time"] * 1e-9)

        results = per_qubit_results(
            prepared, QubitEchoFluxEstimator(), artifact_dir=self.artifact_dir
        )

        result = QubitEchoFluxPulseResult()
        for qubit in self.params.targets:
            r = results[qubit]
            result.fit[qubit] = {
                "flux_bias_v": [float(x) for x in r["flux_bias"]],
                "t2_echo": [float(x) for x in r["t2_echo"]],
                "t2_echo_stderr": [float(x) for x in r["t2_echo_stderr"]],
                "amplitude": [float(x) for x in r["amplitude"]],
                "offset": [float(x) for x in r["offset"]],
                # The frame declaration: the x axis above is an EXCURSION from
                # this bias. See qubit_relaxation_flux_pulse for the rationale.
                "old_idle_flux": flux_anchor_v(self, qubit),
            }
            result.outcomes[qubit] = Outcome.SUCCESSFUL if r.get("success", False) else Outcome.FAILED
        return result

    def probe(self):  # pragma: no cover - driver half
        raise NotImplementedError("a driver backend supplies probe()")
