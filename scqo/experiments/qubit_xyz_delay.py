"""Qubit XY-Z delay — align the Z (flux) line to the XY drive line, greenfield.

Migrated from the QM reference node ``16a_xyz_delay`` (``qua-libs``). The XY x180
drive and a same-length Z (flux) pulse are slid past each other at 1 ns
resolution for two preparations (|e> via x180, |g> via idle); the ``|e> - |g>``
readout contrast versus relative shift is a TRIANGLE (the overlap of two
equal-length rectangles), peaked where the two lines are aligned. The peak
position is the delay to add to the flux line so a Z pulse and the XY drive it
accompanies arrive together.

FRAME: the Z pulse rides on the standing bias (``z_pulse_amp_v`` is an absolute
excursion played on top of ``idle_flux``), and the extracted delay is written
ABSOLUTELY — the vendored node incremented ``q.z.opx_output.delay`` by the fit,
so ``estimate()`` reads the channel's current ``flux_delay_s`` and reports
``old + fitted`` (the same re-referencing invariant flux facts use).
"""

from __future__ import annotations

from typing import ClassVar

import numpy as np
from pydantic import Field

from .._scqat import per_qubit_results
from ..contract import DatasetContract
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


class QubitXyzDelayParameters(
    TargetSelection, AveragingParameters, StateReadoutParameters, QubitResetParameters
):
    """Inputs for an XY-Z delay calibration."""

    half_scan_ns: int = Field(
        60, gt=1,
        description="Half the relative-timing scan: the shift runs over "
                    "[-half_scan_ns, half_scan_ns) ns at 1 ns resolution "
                    "(2*half_scan_ns points, and as many baked segments on QM). "
                    "Must exceed the pi pulse length so the triangle peak clears "
                    "the scan edges.",
    )
    z_pulse_amp_v: float = Field(
        0.1,
        description="Z (flux) pulse amplitude in volts, played on top of the "
                    "standing idle bias, detuning the qubit during the XY drive.",
    )


class QubitXyzDelayResult(Result):
    """Fitted XY-Z delay.

    ``fit[target]``: ``flux_delay_s`` (new ABSOLUTE line delay = old +
    fitted peak), ``delay_shift_s`` (the fitted peak alone), ``delay_std_s``,
    ``snr`` and ``old_flux_delay_s``.
    """


@register
class QubitXyzDelay(Experiment):
    """Backend-agnostic XY-Z delay. ``probe()`` is supplied by a driver."""

    name: ClassVar[str] = "qubit_xyz_delay"
    description: ClassVar[str] = (
        "Slide a fixed X180 XY pulse and a same-length Z (flux) pulse past each "
        "other at 1 ns resolution for two preparations (|e> via x180, |g> via "
        "idle) and fit the |e> - |g> contrast to the triangle overlap of the two "
        "pulses; its peak is the flux line's delay relative to the drive line, "
        "written to the flux channel's flux_delay_s so simultaneous XY+Z gates "
        "actually coincide. Needs a calibrated x180 (its length sets the triangle "
        "width). use_state_discrimination returns the FPGA-discriminated averaged "
        "state instead of I/Q (run single_shot_readout and accept its "
        "readout_rotation_rad / readout_threshold suggestions first)."
    )
    Parameters: ClassVar[type] = QubitXyzDelayParameters
    Result: ClassVar[type] = QubitXyzDelayResult
    Contract: ClassVar[DatasetContract] = DatasetContract(
        sweeps=("prepared_state", "relative_time_ns"),
        sweep_units=("state", "ns"),
        variables=("I", "Q"),
        alt_variables=STATE_ALT,
    )
    required_operations: ClassVar[tuple[str, ...]] = ("rx", "readout", "flux_bias")
    #: stored blob centers ride the dataset -> axial axis = the measured g->e vector
    attach_readout_positions: ClassVar[bool] = True

    params: QubitXyzDelayParameters

    def define_sweep(self) -> dict[str, np.ndarray]:
        half = int(self.params.half_scan_ns)
        return {
            "prepared_state": np.array([0, 1]),
            "relative_time_ns": time_axis_ns(-half, half - 1, 2 * half),
        }

    def simulate(self, coords: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
        prepared = coords["prepared_state"]
        t_ns = coords["relative_time_ns"]
        targets = self.params.targets
        rng = np.random.default_rng(stable_seed("qubit_xyz_delay", *targets))
        use_state = self.params.use_state_discrimination

        n_t, n_ps, n_rt = len(targets), prepared.size, t_ns.size
        i_data = np.zeros((n_t, n_ps, n_rt))
        q_data = np.zeros_like(i_data)
        state = np.zeros_like(i_data)

        for k, q in enumerate(targets):
            # The triangle half-width is the pi length; centre it on the stored
            # delay so a re-run after accept sees the peak move to 0.
            xy_ns = float(self.device.channel(q, "drive").pi_duration_s) * 1e9
            t0 = float(self.device.channel(q, "flux").flux_delay_s) * 1e9
            tri = np.maximum(0.0, 1.0 - np.abs(t_ns - t0) / xy_ns)
            pops = {0: np.full(n_rt, 0.05), 1: 0.05 + 0.9 * tri}
            if use_state:
                for j, s in enumerate(prepared):
                    state[k, j] = state_row(pops[int(s)], rng)
            else:
                # both prep slices share ONE IQ geometry (rotation/offset), so
                # their difference is a clean signed triangle for the axial fit.
                theta = float(rng.uniform(-np.pi, np.pi))
                sep = float(rng.uniform(2.0, 4.0))
                pos0 = complex(float(rng.uniform(-1.0, 1.0)), float(rng.uniform(-1.0, 1.0)))
                for j, s in enumerate(prepared):
                    z = pos0 + pops[int(s)] * sep * np.exp(1j * theta)
                    z = z + rng.normal(0.0, 0.02, n_rt) + 1j * rng.normal(0.0, 0.02, n_rt)
                    i_data[k, j] = np.real(z)
                    q_data[k, j] = np.imag(z)

        return readout_vars(use_state, state, i_data, q_data)

    def estimate(self) -> QubitXyzDelayResult:
        assert self.dataset is not None, "run() populates self.dataset before estimate()"
        from scqat.estimators.xyz_delay import XyzDelayEstimator

        # scqat's contract: coords `prepared_state` + `relative_time` (seconds), and a
        # discriminated `signal` or complex I/Q. Names already match; only rename the
        # signal source and the time axis, then scale ns -> s.
        rename = signal_rename(self.dataset, {"relative_time_ns": "relative_time"})
        prepared = self.dataset.rename(rename)
        prepared = prepared.assign_coords(relative_time=prepared["relative_time"] * 1e-9)

        # the triangle half-width is the target's pi length, which can differ per qubit
        per_target = {
            qubit: {"xy_duration_s": float(self.device.channel(qubit, "drive").pi_duration_s)}
            for qubit in self.params.targets
        }
        results = per_qubit_results(
            prepared, XyzDelayEstimator(), artifact_dir=self.artifact_dir,
            per_target_kwargs=per_target,
        )

        result = QubitXyzDelayResult()
        for qubit in self.params.targets:
            r = results[qubit]
            shift = float(r["t0_s"])
            old = float(self.device.channel(qubit, "flux").flux_delay_s)
            result.fit[qubit] = {
                # the vendored node INCREMENTED the port delay by the fitted peak;
                # scqo pushes absolutes, so the new line delay is old + fitted.
                "flux_delay_s": old + shift,
                "delay_shift_s": shift,
                "delay_std_s": float(r["t0_std_s"]),
                "snr": float(r["snr"]),
                "old_flux_delay_s": old,
            }
            ok = bool(r["success"]) and np.isfinite(shift)
            result.outcomes[qubit] = Outcome.SUCCESSFUL if ok else Outcome.FAILED
        return result

    def update(self) -> None:
        if self.result is None:
            return
        for qubit, fit in self.result.fit.items():
            if self.result.outcomes[qubit] is Outcome.SUCCESSFUL:
                self.device.channel(qubit, "flux").flux_delay_s = fit["flux_delay_s"]

    def probe(self):  # pragma: no cover - driver half
        raise NotImplementedError("a driver backend supplies probe()")
