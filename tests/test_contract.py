"""Canonical-contract conformance: every method's (simulated) probe output conforms,
run() enforces the contract, and validate() rejects non-conforming data.

These are the SCQO-side conformance checks; a real driver runs the same
``Experiment.Contract.validate(probe_output)`` against its own probe.
"""

from __future__ import annotations

import pytest

from scqo import ContractError, DatasetContract
from scqo.experiments import (
    BroadbandQubitSpectroscopy,
    BroadbandResonatorSpectroscopy,
    PairSwapChevron,
    PairSwapFluxMap,
    QubitPowerRabi,
    QubitRamsey,
    QubitT1Ade,
    ResonatorSpectroscopy,
)
from scqo.testing import InMemoryDevice, SimulatedBackend, demo_device


def _device() -> InMemoryDevice:
    """The demo device's vendor tree (fixed-frequency q0/q1) — the contract
    checks only need a backend whose simulate() runs, so the seeded demo
    vendor replaces the hand-built qubit dict."""
    _roster, _design, vendor = demo_device()
    return vendor


# Concrete (unregistered) subclasses: SimulatedBackend never calls probe().
class _Res(ResonatorSpectroscopy):
    def probe(self):
        return None


class _Broadband(BroadbandResonatorSpectroscopy):
    def probe(self):
        return None


class _BroadbandQubit(BroadbandQubitSpectroscopy):
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
    (_Broadband, "frequency_hz"),
    (_BroadbandQubit, "frequency_hz"),
    (_Ram, "idle_time_ns"),
    (_PR, "amp_prefactor"),
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


def test_alt_variables_accepts_population_only():
    """A contract with alt_variables=(("population",),) accepts a discriminated
    probe's averaged dataset — and still accepts the primary (I, Q) form."""
    _, ds = _acquire(_Ram)
    contract = _Ram.Contract
    assert contract.alt_variables == (("population",),)

    contract.validate(ds)  # primary (I, Q) form conforms
    pop_ds = ds.drop_vars("Q").rename({"I": "population"})
    contract.validate(pop_ds)  # alt (population,) form conforms
    assert contract.conforms(pop_ds)


def test_alt_variables_enforce_dims_rigor():
    """Alternative sets are held to the SAME rigor as the primary set: a
    'population' variable that does not span exactly (target, *sweeps) is
    rejected."""
    _, ds = _acquire(_Ram)
    pop_ds = ds.drop_vars("Q").rename({"I": "population"})
    squashed = pop_ds.isel(idle_time_ns=0, drop=True)  # loses the sweep dim
    with pytest.raises(ContractError):
        _Ram.Contract.validate(squashed)


def test_alt_variables_failure_lists_all_accepted_sets():
    """When nothing conforms, the error names every accepted variable set so a
    probe author sees the full menu."""
    _, ds = _acquire(_Ram)
    bogus = ds.drop_vars(["I", "Q"])
    with pytest.raises(ContractError) as err:
        _Ram.Contract.validate(bogus)
    msg = str(err.value)
    assert "('I', 'Q')" in msg and "('population',)" in msg


class _Chev(PairSwapChevron):
    def probe(self):
        return None


class _Map(PairSwapFluxMap):
    def probe(self):
        return None


@pytest.mark.parametrize("cls, sweeps", [
    (_Chev, ("flux_amp_v", "swap_time_ns")),
    (_Map, ("qubit_flux_v", "coupler_flux_v")),
])
def test_joint_population_contract(cls, sweeps):
    """The swap maps store the readout schema's joint form: ONE variable
    (``joint_population``) carrying an extra READOUT dim (``joint_state``) on
    top of the physics sweeps — and that extra dim is held to the same rigor
    as the sweeps (dim + coord, exact spans)."""
    backend = SimulatedBackend(demo_device(tunable=True)[2])
    exp = cls(backend, cls.Parameters(targets=["q0_q1"]))
    exp.sweep_axes = exp.define_sweep()
    ds = backend.acquire(exp)

    assert cls.Contract.sweeps == sweeps
    assert cls.Contract.variables == ("joint_population",)
    assert cls.Contract.readout_dims == ("joint_state",)
    cls.Contract.validate(ds)  # does not raise
    assert [str(v) for v in ds["joint_state"].values] == ["00", "01", "10", "11"]

    with pytest.raises(ContractError):
        cls.Contract.validate(ds.rename({"joint_population": "pops"}))
    with pytest.raises(ContractError):
        # collapsing the readout dim breaks the joint form
        cls.Contract.validate(ds.isel(joint_state=0, drop=True))
    with pytest.raises(ContractError):
        cls.Contract.validate(ds.isel({sweeps[0]: 0}, drop=True))  # dims rigor


def test_shot_form_is_an_accepted_alternative():
    """qc_n_swap_amp's contract accepts BOTH readout forms: the averaged
    joint_population AND the per-shot per-member ``state`` (readout dims
    member + shot_idx) — the readout_mode param selects which one the probe
    emits, and each form is validated with full rigor."""
    from scqo.experiments import QcNSwapAmp

    class _NSwap(QcNSwapAmp):
        def probe(self):
            return None

    backend = SimulatedBackend(demo_device(tunable=True)[2])
    for mode in ("average", "shot"):
        exp = _NSwap(backend, _NSwap.Parameters(targets=["q0_q1"], num_averages=50,
                                                readout_mode=mode))
        exp.sweep_axes = exp.define_sweep()
        ds = backend.acquire(exp)
        _NSwap.Contract.validate(ds)
        if mode == "shot":
            assert "state" in ds.data_vars
            assert ds["state"].dims == ("target", "member", "flux_amp_v",
                                        "swap_count", "shot_idx")
            with pytest.raises(ContractError):
                # a shot-form dataset without its shot axis conforms to NEITHER form
                _NSwap.Contract.validate(ds.isel(shot_idx=0, drop=True))
        else:
            assert "joint_population" in ds.data_vars


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


class _Ade(QubitT1Ade):
    def probe(self):
        return None


def test_extras_ride_alongside_the_primary_form():
    """The T1 trackers' contract names only the per-block streams; the
    per-shot arrays, timestamps and delay labels ride as EXTRA vars/dims,
    which validate() must tolerate (extra variables/coords are allowed) —
    while a missing PRIMARY var still refuses."""
    exp, ds = _acquire(_Ade)
    assert QubitT1Ade.Contract.sweeps == ("block_idx",)
    exp.Contract.validate(ds)  # extras present: state, block_time_s
    assert "state" in ds.data_vars and "block_time_s" in ds.data_vars
    assert set(ds["state"].dims) == {"target", "block_idx", "delay_idx", "shot_idx"}
    with pytest.raises(ContractError):
        exp.Contract.validate(ds.drop_vars("estimated_gamma"))
