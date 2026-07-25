"""Demo device + in-memory vendor + simulated backend (scqo.model.testing):
the offline end-to-end substrate every later cutover test builds on."""

import numpy as np
import pytest

from scqo.model import RecordingDevice, state_store
from scqo.model.testing import (
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


def test_demo_roster_shape(roster):
    assert set(roster.modes()) == {"q0", "q1", "q0_res", "q1_res"}
    assert set(roster.composites()) == {"q0_q1"}
    assert roster.entities["q0_q1"].roles["high"] == ("q1",)
    assert set(roster.channels()) == {"q0_ro", "q1_ro", "q0_xy", "q1_xy"}
    # multiplexed readout: both readout channels ride the one feedline
    assert {roster.entities[c].line for c in ("q0_ro", "q1_ro")} == {"fl"}
    assert set(roster.operations("q0")) == {"rx", "readout"}


def test_demo_design_binds_roles_and_seeds(roster, design):
    assert design.get("q1", "f_01_hz") > design.get("q0", "f_01_hz")
    from scqo.model import seed_value
    assert seed_value(roster, design, "q0_xy", "drive_freq_hz") == 3.8e9
    assert seed_value(roster, design, "q1_ro", "readout_freq_hz") == 6.05e9


def test_pull_seeded_recording_device_end_to_end(tmp_path, roster, design):
    vendor = InMemoryDevice(roster, demo_vendor_state(roster, design))
    device = RecordingDevice(vendor, roster, state_store(tmp_path, roster))
    assert device.component("q0_xy").drive_freq_hz == 3.8e9
    assert device.component("q0_ro").readout_duration_s == 8.0e-7
    device.component("q0_xy").pi_amp = 0.22
    assert vendor.snapshot()["q0_xy"]["pi_amp"] == 0.22
    assert device.history()[0].field == "pi_amp"


def test_composite_knobs_on_the_in_memory_vendor(tmp_path, roster, design):
    vendor = InMemoryDevice(roster, demo_vendor_state(roster, design))
    device = RecordingDevice(vendor, roster, state_store(tmp_path, roster))
    view = device.component("q0_q1")
    view.write_knob("iswap_waveform_dt_s", 5e-10)
    view.write_knob("iswap_waveform", [0.0, 0.3, 0.0])
    assert vendor.snapshot()["q0_q1"]["iswap_waveform"] == [0.0, 0.3, 0.0]
    assert view.read_knob("iswap_waveform") == [0.0, 0.3, 0.0]


def test_unrealized_entities_raise_keyerror(roster, design):
    state = demo_vendor_state(roster, design)
    del state["q1_xy"]
    vendor = InMemoryDevice(roster, state)
    with pytest.raises(KeyError):
        vendor.component("q1_xy")


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
