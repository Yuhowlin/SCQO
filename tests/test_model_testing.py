"""Demo device + in-memory vendor + simulated backend (scqo.testing):
the offline end-to-end substrate every later cutover test builds on."""

import numpy as np
import pytest

from scqo import RecordingDevice, state_store
from scqo.testing import (
    InMemoryDevice,
    SimulatedBackend,
    demo_components,
    demo_design,
    demo_vendor_state,
)


@pytest.fixture(scope="module")
def roster():
    return demo_components()


@pytest.fixture(scope="module")
def design(roster):
    return demo_design(roster)


def test_demo_components_shape(roster):
    assert set(roster.modes()) == {"q0", "q1", "q0_res", "q1_res"}
    assert set(roster.composites()) == {"q0_q1"}
    assert roster.entities["q0_q1"].roles["high"] == ("q1",)
    assert set(roster.channels()) == {"fl.q0", "fl.q1", "xy_q0.q0",
                                      "xy_q1.q1"}
    # every drive line lends itself to the other qubit, undeclared
    assert set(roster.borrowed_channels()) == {"xy_q0.q1", "xy_q1.q0"}
    assert set(roster.operation_entities()) == {"q0_q1.iswap"}
    # multiplexed readout: both readout channels ride the one feedline
    assert {roster.entities[c].line for c in ("fl.q0", "fl.q1")} == {"fl"}
    assert set(roster.operations("q0")) == {"rx", "readout"}


def test_tunable_demo_puts_the_bias_on_the_lines():
    """The flux variant: a z line per qubit and one per coupler; the vendor
    tree carries the LINES' knobs, and nothing for the flux channels (their
    transfer function is facts) or the unadopted borrowed routes."""
    r = demo_components(tunable=True)
    assert {"z_q0", "z_q1", "zc_q0_q1"} <= set(r.lines())
    assert {"z_q0.q0", "z_q1.q1", "zc_q0_q1.q0_q1_c"} <= set(r.channels())
    state = demo_vendor_state(r, demo_design(r))
    for line in ("z_q0", "z_q1", "zc_q0_q1"):
        assert state[line] == {"idle_flux": 0.0, "flux_delay_s": 0.0}
    assert "z_q0.q0" not in state
    assert "xy_q0.q0_q1_c" not in state
    assert state["q0_q1.iswap"] == {}


def test_demo_design_binds_roles_and_seeds(roster, design):
    assert design.get("q1", "f_01_hz") > design.get("q0", "f_01_hz")
    from scqo import seed_value
    assert seed_value(roster, design, "xy_q0.q0", "drive_freq_hz") == 3.8e9
    assert seed_value(roster, design, "fl.q1", "readout_freq_hz") == 6.05e9


def test_pull_seeded_recording_device_end_to_end(tmp_path, roster, design):
    vendor = InMemoryDevice(roster, demo_vendor_state(roster, design))
    device = RecordingDevice(vendor, roster, state_store(tmp_path, roster))
    assert device.component("xy_q0.q0").drive_freq_hz == 3.8e9
    assert device.component("fl.q0").readout_duration_s == 8.0e-7
    device.component("xy_q0.q0").pi_amp = 0.22
    assert vendor.snapshot()["xy_q0.q0"]["pi_amp"] == 0.22
    assert device.history()[0].field == "pi_amp"


def test_operation_knobs_on_the_in_memory_vendor(tmp_path, roster, design):
    vendor = InMemoryDevice(roster, demo_vendor_state(roster, design))
    device = RecordingDevice(vendor, roster, state_store(tmp_path, roster))
    view = device.operation("q0_q1", "iswap")
    view.write_knob("waveform_dt_s", 5e-10)
    view.write_knob("waveform", [0.0, 0.3, 0.0])
    assert vendor.snapshot()["q0_q1.iswap"]["waveform"] == [0.0, 0.3, 0.0]
    assert view.read_knob("waveform") == [0.0, 0.3, 0.0]


def test_unrealized_entities_raise_keyerror(roster, design):
    state = demo_vendor_state(roster, design)
    del state["xy_q1.q1"]
    vendor = InMemoryDevice(roster, state)
    with pytest.raises(KeyError):
        vendor.component("xy_q1.q1")
    # a borrowed channel is unrealized until the vendor adopts the route
    assert "xy_q0.q1" not in state
    with pytest.raises(KeyError):
        vendor.component("xy_q0.q1")
    vendor.adopt("xy_q0.q1", {"pi_amp": 0.3})
    assert vendor.component("xy_q0.q1").pi_amp == 0.3


def test_simulated_backend_acquire_contract():
    class _Params:
        targets = ("q0", "q1")

    class _Exp:
        params = _Params()
        sweep_axes = {"freq_hz": np.linspace(3.7e9, 3.9e9, 5)}

        def simulate(self, sweep):
            n = len(sweep["freq_hz"])
            return {"magnitude": np.ones((2, n)),
                    "extra": (("target",), np.zeros(2))}

    ds = SimulatedBackend(device=None).acquire(_Exp())
    assert list(ds["magnitude"].dims) == ["target", "freq_hz"]
    assert list(ds["extra"].dims) == ["target"]
    assert list(ds.coords["target"].values) == ["q0", "q1"]
