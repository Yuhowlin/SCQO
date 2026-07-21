"""Canonical-contract conformance: every method's (simulated) probe output conforms,
run() enforces the contract, and validate() rejects non-conforming data.

These are the SCQO-side conformance checks; a real driver runs the same
``Experiment.Contract.validate(probe_output)`` against its own probe.
"""

from __future__ import annotations

import pytest

from scqo import ContractError, DatasetContract
from scqo.experiments import QubitPowerRabi, QubitRamsey, ResonatorSpectroscopy
from scqo.testing import InMemoryDevice, SimulatedBackend


def _device() -> InMemoryDevice:
    return InMemoryDevice(
        {
            "q0": {"readout_freq": 5.95e9, "drive_freq": 3.87e9, "pi_amp": 0.2, "readout_amp": 0.25},
            "q1": {"readout_freq": 6.05e9, "drive_freq": 4.01e9, "pi_amp": 0.18, "readout_amp": 0.22},
        }
    )


# Concrete (unregistered) subclasses: SimulatedBackend never calls probe().
class _Res(ResonatorSpectroscopy):
    def probe(self):
        return None


class _Ram(QubitRamsey):
    def probe(self):
        return None


class _PR(QubitPowerRabi):
    def probe(self):
        return None


CASES = [
    (_Res, "detuning_hz"),
    (_Ram, "idle_time_ns"),
    (_PR, "amp_factor"),
]


def _acquire(cls):
    backend = SimulatedBackend(_device())
    exp = cls(backend, cls.Parameters(targets=["q0", "q1"]))
    exp.sweep_axes = exp.define_sweep()
    return exp, backend.acquire(exp)


@pytest.mark.parametrize("cls, sweep", CASES)
def test_simulated_probe_output_conforms(cls, sweep):
    exp, ds = _acquire(cls)
    # the declared contract's sweep axis matches define_sweep, and the dataset conforms
    assert cls.Contract.sweeps == (sweep,)
    assert cls.Contract.dims == ("target", sweep)
    exp.Contract.validate(ds)  # does not raise
    assert exp.Contract.conforms(ds)


def test_run_enforces_contract():
    """A backend that drops a required variable makes run() raise ContractError
    (before estimate() ever sees the data)."""

    class DropIBackend(SimulatedBackend):
        def acquire(self, experiment):
            return super().acquire(experiment).drop_vars("I")

    exp = _Ram(DropIBackend(_device()), _Ram.Parameters(targets=["q0"]))
    with pytest.raises(ContractError):
        exp.run()


def test_validate_rejects_nonconforming():
    contract = DatasetContract(sweeps=("idle_time_ns",), sweep_units=("ns",), variables=("I", "Q"))
    _, ds = _acquire(_Ram)

    contract.validate(ds)  # baseline: conforms

    with pytest.raises(ContractError):
        contract.validate(ds.drop_vars("Q"))  # missing required variable

    with pytest.raises(ContractError):
        contract.validate(ds.rename({"idle_time_ns": "t"}))  # missing sweep dim/coord


def test_two_axis_contract():
    """A 2D-sweep contract validates (qubit, axis1, axis2) datasets and rejects 1D."""
    import numpy as np
    import xarray as xr

    contract = DatasetContract(
        sweeps=("detuning_hz", "power_dbm"), sweep_units=("Hz", "dBm"), variables=("I", "Q")
    )
    assert contract.dims == ("target", "detuning_hz", "power_dbm")

    det, pwr = np.linspace(-1e6, 1e6, 5), np.linspace(-50, -20, 3)
    data = np.zeros((2, det.size, pwr.size))
    ds2 = xr.Dataset(
        {"I": (("target", "detuning_hz", "power_dbm"), data),
         "Q": (("target", "detuning_hz", "power_dbm"), data)},
        coords={"target": ["q0", "q1"], "detuning_hz": det, "power_dbm": pwr},
    )
    contract.validate(ds2)  # conforms

    with pytest.raises(ContractError):
        contract.validate(ds2.isel(power_dbm=0, drop=True))  # lost an axis -> reject
