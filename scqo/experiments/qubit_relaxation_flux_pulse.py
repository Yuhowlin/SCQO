"""Qubit relaxation vs flux PULSE — T1 spectrum over a swept z excursion, greenfield.

Port of :mod:`scqo.experiments.qubit_relaxation_flux`. The physics half is
byte-for-byte; this is a record-only diagnostic (no ``update()``, the
per-flux fits live in ``result.fit``), so nothing lands on the device
surface — only the imports moved (capability helpers from
``scqo.experiments._capabilities.flux`` / ``.state_readout``) and the
driver stub ``probe()`` was added.

FRAME (``_pulse`` in the name, ``FluxPulseSweepParameters`` in the schema):
the z bias is a PULSE played during the idle delay and the DAC adds it to the
standing bias, so the window is measured from the channel's ``idle_flux`` and
0 means "sit at the operating point". A T1 spectrum is therefore read as
"coherence this far off the parked bias", which is the only reading that makes
the T1 minimum at 0 meaningful. See :mod:`._capabilities.flux`.
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


class QubitRelaxationFluxPulseParameters(
    TargetSelection, AveragingParameters, StateReadoutParameters, FluxPulseSweepParameters, QubitResetParameters
):
    """Parameters for T1 vs flux-pulse spectroscopy (window relative to idle_flux)."""

    min_wait_ns: float = Field(16.0, ge=0.0, description="Minimum idle delay.")
    max_wait_ns: float = Field(40000.0, gt=0.0, description="Maximum idle delay.")
    num_wait_points: int = Field(51, gt=1, description="Number of wait time points.")
    # capability defaults narrowed to the coherence window (canonical text constants)
    min_flux_v: float = Field(-0.08, ge=-0.5, description=MIN_FLUX_PULSE_DESC)
    max_flux_v: float = Field(0.08, le=0.5, description=MAX_FLUX_PULSE_DESC)
    prepare_state: int = Field(1, description="State to prepare (0 for g, 1 for e).")


class QubitRelaxationFluxPulseResult(Result):
    """Fitted T1 spectrum results.

    ``fit[target]``: per-flux lists (``flux_bias_v``, ``t1``, ``t1_stderr``,
    ``amplitude``, ``offset``) plus the scalar ``old_idle_flux`` — the standing
    bias the swept excursion rode on, so the spectrum's x axis can be read as
    absolute set-points long after the run.
    """


@register
class QubitRelaxationFluxPulse(Experiment):
    """Measure qubit relaxation time T1 vs Z flux PULSE amplitude (idle-relative)."""

    name: ClassVar[str] = "qubit_relaxation_flux_pulse"
    description: ClassVar[str] = (
        "Sweep a Z PULSE amplitude — RELATIVE to the flux channel's idle_flux, "
        "0 = stay parked — and a wait delay after excitation, fitting T1 decay "
        "at each flux point to map out the T1 spectrum. Record-only: the fits "
        "are saved, nothing is proposed."
    )
    Parameters: ClassVar[type] = QubitRelaxationFluxPulseParameters
    Result: ClassVar[type] = QubitRelaxationFluxPulseResult
    Contract: ClassVar[DatasetContract] = DatasetContract(
        sweeps=("flux_bias_v", "wait_time_ns"),
        sweep_units=("V", "ns"),
        variables=("I", "Q"),
        alt_variables=STATE_ALT,
    )

    params: QubitRelaxationFluxPulseParameters

    required_operations: ClassVar[tuple[str, ...]] = ("rx", "readout", "flux_bias")

    def define_sweep(self) -> dict[str, np.ndarray]:
        wait_time = time_axis_ns(
            self.params.min_wait_ns,
            self.params.max_wait_ns,
            self.params.num_wait_points,
        )
        return {**flux_sweep(self.params), "wait_time_ns": wait_time}

    def simulate(self, coords: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
        """Generate synthetic decay curves with a simulated TLS (Two-Level System) defect dip."""
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
        rng = np.random.default_rng(stable_seed("qubit_relaxation_flux_pulse", *qubits))
        for k in range(n_qubits):
            for f_idx, f_amp in enumerate(flux):
                # Qubit relaxation T1: best at f_amp=0 (T1 ~ 25 us), which under the
                # idle-relative frame IS the sweet spot the qubit is parked at.
                # Away from the parked bias, T1 decays.
                # TLS defect dip 30 mV OFF idle (where T1 drops sharply)
                t1_baseline = 25e-6 * (1.0 - 0.4 * (f_amp ** 2))
                tls_dip = 15e-6 * np.exp(-((f_amp - 0.03) / 0.008) ** 2)
                t1 = max(t1_baseline - tls_dip, 1e-6) # prevent non-positive T1

                # If prepare_state is 0, signal is ground state (stays near 1.0)
                # If prepare_state is 1, signal starts at 1.0 (excited state) and decays to 0.0
                if self.params.prepare_state == 1:
                    decay = np.exp(-(wait * 1e-9) / t1)
                else:
                    decay = np.ones_like(wait)

                noise = 0.02
                if use_state:
                    state[k, f_idx] = state_row(decay, rng)
                else:
                    i_data[k, f_idx] = decay + rng.normal(0, noise, n_wait)
                    q_data[k, f_idx] = rng.normal(0, noise, n_wait)

        return readout_vars(use_state, state, i_data, q_data)

    def estimate(self) -> QubitRelaxationFluxPulseResult:
        assert self.dataset is not None, "run() populates self.dataset before estimate()"
        # scqat is NOT renamed — the estimator is the same fit either frame.
        from scqat.estimators.qubit_relaxation_flux import QubitRelaxationFluxEstimator
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
            prepared, QubitRelaxationFluxEstimator(), artifact_dir=self.artifact_dir
        )

        result = QubitRelaxationFluxPulseResult()
        for qubit in self.params.targets:
            r = results[qubit]
            result.fit[qubit] = {
                "flux_bias_v": [float(x) for x in r["flux_bias"]],
                "t1": [float(x) for x in r["t1"]],
                "t1_stderr": [float(x) for x in r["t1_stderr"]],
                "amplitude": [float(x) for x in r["amplitude"]],
                "offset": [float(x) for x in r["offset"]],
                # The frame declaration: the x axis above is an EXCURSION from
                # this bias, so a reader can recover absolute set-points without
                # knowing where the qubit was parked at run time.
                "old_idle_flux": flux_anchor_v(self, qubit),
            }
            result.outcomes[qubit] = Outcome.SUCCESSFUL if r.get("success", False) else Outcome.FAILED
        return result

    def probe(self):  # pragma: no cover - driver half
        raise NotImplementedError("a driver backend supplies probe()")
