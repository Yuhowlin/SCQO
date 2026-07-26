"""Qubit echo vs flux — T2_echo spectrum over a swept z bias, greenfield.

Port of :mod:`scqo.experiments.qubit_echo_flux`. The physics half is
byte-for-byte; this is a record-only diagnostic (no ``update()`` — the
per-flux T2_echo fits live in the run record), so what moved is only the
import surface (capability helpers from their current locations) and the
kind-based gating inherited from the greenfield base.
"""

from __future__ import annotations

from typing import ClassVar

import numpy as np
from pydantic import Field

from ..contract import DatasetContract
from ._capabilities.flux import (
    MAX_FLUX_DESC,
    MIN_FLUX_DESC,
    FluxSweepParameters,
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
from ..parameters import AveragingParameters, TargetSelection
from ..result import Outcome, Result
from ..experiment import Experiment
from . import register


class QubitEchoFluxParameters(
    TargetSelection, AveragingParameters, StateReadoutParameters, FluxSweepParameters, QubitResetParameters
):
    """Parameters for T2 echo vs flux spectroscopy."""

    min_wait_ns: float = Field(32.0, ge=0.0, description="Minimum idle delay (total tau).")
    max_wait_ns: float = Field(40000.0, gt=0.0, description="Maximum idle delay (total tau).")
    num_wait_points: int = Field(51, gt=1, description="Number of wait time points.")
    # capability defaults narrowed to the coherence window (canonical text constants)
    min_flux_v: float = Field(-0.08, ge=-0.5, description=MIN_FLUX_DESC)
    max_flux_v: float = Field(0.08, le=0.5, description=MAX_FLUX_DESC)


class QubitEchoFluxResult(Result):
    """Fitted T2 echo spectrum results."""


@register
class QubitEchoFlux(Experiment):
    """Measure qubit Hahn echo coherence time T2_echo vs Z flux pulse amplitude."""

    name: ClassVar[str] = "qubit_echo_flux"
    description: ClassVar[str] = (
        "Sweep a Z pulse amplitude and total wait delay in a Hahn echo sequence, "
        "fitting T2_echo decay at each flux point to map out the T2_echo spectrum."
    )
    Parameters: ClassVar[type] = QubitEchoFluxParameters
    Result: ClassVar[type] = QubitEchoFluxResult
    Contract: ClassVar[DatasetContract] = DatasetContract(
        sweeps=("flux_bias_v", "wait_time_ns"),
        sweep_units=("V", "ns"),
        variables=("I", "Q"),
        alt_variables=STATE_ALT,
    )

    params: QubitEchoFluxParameters

    required_operations: ClassVar[tuple[str, ...]] = ("rx", "readout", "flux_bias")

    def define_sweep(self) -> dict[str, np.ndarray]:
        wait_time = np.linspace(
            self.params.min_wait_ns,
            self.params.max_wait_ns,
            self.params.num_wait_points,
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
        rng = np.random.default_rng(stable_seed("qubit_echo_flux", *qubits))
        for k in range(n_qubits):
            for f_idx, f_amp in enumerate(flux):
                # T2_echo: Sweet spot is at f_amp=0 (T2_echo ~ 50 us)
                # Introduce a TLS defect dip at f_amp = 0.03 V
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

    def estimate(self) -> QubitEchoFluxResult:
        assert self.dataset is not None, "run() populates self.dataset before estimate()"
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

        result = QubitEchoFluxResult()
        for qubit in self.params.targets:
            r = results[qubit]
            result.fit[qubit] = {
                "flux_bias_v": [float(x) for x in r["flux_bias"]],
                "t2_echo": [float(x) for x in r["t2_echo"]],
                "t2_echo_stderr": [float(x) for x in r["t2_echo_stderr"]],
                "amplitude": [float(x) for x in r["amplitude"]],
                "offset": [float(x) for x in r["offset"]],
            }
            result.outcomes[qubit] = Outcome.SUCCESSFUL if r.get("success", False) else Outcome.FAILED
        return result

    def probe(self):  # pragma: no cover - driver half
        raise NotImplementedError("a driver backend supplies probe()")
