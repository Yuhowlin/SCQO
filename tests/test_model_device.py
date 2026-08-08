"""Device-layer contracts (scqo.device): recording semantics over a
fake vendor — pull/push seeding, push-first writes, coupled echoes, monitors
never touching the vendor, composite knob surfaces."""

import pytest

from scqo import (
    CompositeView,
    DeviceModel,
    RecordingDevice,
    parse_components,
    state_store,
)
from tests.test_model_roster import EXAMPLE


class _FakeComposite(CompositeView):
    def __init__(self, vendor, name):
        self.name = name
        self.kind = "qubit_pair"
        self._vendor, self._name = vendor, name

    def read_knob(self, field):
        return self._vendor.data[self._name].get(field)

    def write_knob(self, field, value):
        self._vendor.data[self._name][field] = value


class _FakeChannel:
    """Duck-typed channel view over the vendor dict (real drivers subclass
    make_view_base; the recorder only needs getattr/setattr)."""

    def __init__(self, vendor, name):
        object.__setattr__(self, "_vendor", vendor)
        object.__setattr__(self, "_name", name)

    def __getattr__(self, field):
        return self._vendor.data[self._name].get(field)

    def __setattr__(self, field, value):
        if field == "pi_amp" and abs(value) > 1.0:
            raise ValueError("amplitude out of range")  # instrument refusal
        self._vendor.data[self._name][field] = float(value)
        if field == "readout_power_dbm":
            # the chain solve: absolute power moves the digital amplitude
            self._vendor.data[self._name]["readout_amp"] = 10 ** (
                (value + 30.0) / 20.0) / 10.0


class FakeVendor(DeviceModel):
    def __init__(self, data):
        self.data = data
        self.saved = 0

    def component(self, name):
        if name not in self.data:
            raise KeyError(name)
        if name == "q1_q2":
            return _FakeComposite(self, name)
        return _FakeChannel(self, name)

    def save(self):
        self.saved += 1

    def snapshot(self):
        return {n: dict(f) for n, f in self.data.items()}


@pytest.fixture()
def roster():
    return parse_components(EXAMPLE)


@pytest.fixture()
def vendor():
    return FakeVendor({
        "q1_xy": {"drive_freq_hz": 5.136e9, "pi_amp": 0.209},
        "q1_ro": {"readout_freq_hz": 5.934e9, "readout_amp": 0.112,
                  "readout_power_dbm": -30.0},
        "q1_z": {"idle_flux": 0.118},
        "q1_q2": {"iswap_coupler_flux": 0.0},
    })


@pytest.fixture()
def device(tmp_path, roster, vendor):
    return RecordingDevice(vendor, roster,
                           state_store(tmp_path, roster, setup="qm_a"))


# ------------------------------------------------------------ pull seeding

def test_pull_seeds_from_the_vendor_without_history(device):
    assert device.component("q1_xy").drive_freq_hz == 5.136e9
    assert device.history() == ()          # seeding is not a change


def test_unseeded_knob_reads_strict_with_anchor_pointer(device):
    with pytest.raises(KeyError, match="anchor order"):
        device.component("q2_xy").drive_freq_hz


def test_monitors_read_none_until_measured(device):
    assert device.component("q1_ro").fidelity_g is None


# ------------------------------------------------------------------ writes

def test_write_pushes_vendor_first_then_records(device, vendor):
    device.set_context("qubit_power_rabi", run_id="r7")
    device.component("q1_xy").pi_amp = 0.222
    assert vendor.data["q1_xy"]["pi_amp"] == 0.222
    row = device.history()[0]
    assert (row.entity, row.field, row.new) == ("q1_xy", "pi_amp", 0.222)
    assert row.experiment == "qubit_power_rabi" and row.run_id == "r7"
    assert row.kind == "drive"


def test_vendor_rejection_leaves_no_false_history(device, vendor):
    with pytest.raises(ValueError, match="out of range"):
        device.component("q1_xy").pi_amp = 7.0
    assert device.history() == ()
    assert vendor.data["q1_xy"]["pi_amp"] == 0.209


def test_coupled_echo_is_recorded_with_attribution(device, vendor):
    device.component("q1_ro").readout_power_dbm = -20.0
    rows = {(r.field): r for r in device.history()}
    assert rows["readout_power_dbm"].coupled_to is None
    assert rows["readout_amp"].coupled_to == "readout_power_dbm"
    assert device.component("q1_ro").readout_amp == pytest.approx(
        vendor.data["q1_ro"]["readout_amp"])


def test_monitor_write_records_but_never_touches_the_vendor(device, vendor):
    device.component("q1_ro").fidelity_g = 0.96
    assert "fidelity_g" not in vendor.data["q1_ro"]
    assert device.component("q1_ro").fidelity_g == 0.96


def test_unknown_field_write_fails_loudly(device):
    with pytest.raises(AttributeError, match="no field"):
        device.component("q1_xy").readout_freq_hz = 5.9e9


# -------------------------------------------------------------- composites

def test_composite_knobs_via_the_generic_surface(device, vendor):
    view = device.component("q1_q2")
    view.write_knob("iswap_coupler_flux", 0.081)
    assert vendor.data["q1_q2"]["iswap_coupler_flux"] == 0.081
    assert view.read_knob("iswap_coupler_flux") == 0.081
    assert device.history()[0].kind == "qubit_pair"


def test_composite_undeclared_operation_is_exact_cause(device):
    with pytest.raises(Exception, match="operation 'cz' is not declared"):
        device.component("q1_q2").write_knob("cz_coupler_flux", 0.1)


# ------------------------------------------------------------ entity kinds

def test_modes_carry_no_knobs_and_the_error_names_channels(device):
    with pytest.raises(KeyError, match="q1_xy"):
        device.component("q1")
    with pytest.raises(KeyError, match="unknown entity"):
        device.component("ghost")


# --------------------------------------------------------------- push mode

def test_push_mode_restores_saved_knobs_into_the_vendor(tmp_path, roster,
                                                        vendor):
    seed = state_store(tmp_path, roster, setup="qm_a")
    seed.record("q1_xy", "pi_amp", 0.333)
    seed.save()
    device = RecordingDevice(
        vendor, roster, state_store(tmp_path, roster, setup="qm_a"),
        on_load="push")
    assert vendor.data["q1_xy"]["pi_amp"] == 0.333    # SCQO won
    # store-silent knobs backfill from the vendor snapshot:
    assert device.component("q1_xy").drive_freq_hz == 5.136e9


def test_push_mode_tolerates_unrealized_knobs(tmp_path, roster):
    """A knob the backend declares Unrealized raises NotImplementedError —
    capability, not failure: seeding skips it and keeps going."""
    class PartialVendor(FakeVendor):
        def component(self, name):
            view = super().component(name)
            if name == "q1_xy":
                original = view.__setattr__

                class _Refusing:
                    name = "q1_xy"

                    def __setattr__(self, field, value):
                        if field == "drag_beta":
                            raise NotImplementedError("no DRAG on this box")
                        original(field, value)

                    def __getattr__(self, field):
                        return getattr(view, field)
                return _Refusing()
            return view

    seed = state_store(tmp_path, roster, setup="qm_a")
    seed.record("q1_xy", "drag_beta", -1.0)     # the unrealized one
    seed.record("q1_xy", "pi_amp", 0.44)        # must still land
    seed.save()
    vendor = PartialVendor({"q1_xy": {"pi_amp": 0.1}})
    device = RecordingDevice(vendor, roster,
                             state_store(tmp_path, roster, setup="qm_a"),
                             on_load="push")      # must not raise
    assert vendor.data["q1_xy"]["pi_amp"] == 0.44
    assert device.component("q1_xy").pi_amp == 0.44


def test_push_mode_tolerates_unrealized_entities(tmp_path, roster, vendor):
    seed = state_store(tmp_path, roster, setup="qm_a")
    seed.record("q3_xy", "pi_amp", 0.5)               # vendor has no q3_xy
    seed.save()
    device = RecordingDevice(
        vendor, roster, state_store(tmp_path, roster, setup="qm_a"),
        on_load="push")                               # must not raise
    assert device.component("q1_xy").pi_amp == 0.209


def test_save_persists_vendor_and_store(device, vendor, tmp_path):
    device.component("q1_xy").pi_amp = 0.25
    device.save()
    assert vendor.saved == 1
    assert (tmp_path / "scqo_state.json").is_file()
    again = state_store(tmp_path, roster=device.roster)
    assert again.get("q1_xy", "pi_amp") == 0.25


# ----------------------------------------------- review-added regressions

def test_store_invalid_values_never_reach_the_vendor(device, vendor):
    view = device.component("q1_q2")
    view.write_knob("iswap_waveform_dt_s", 5e-10)
    with pytest.raises(Exception, match="non-finite"):
        view.write_knob("iswap_waveform", [0.1, float("nan")])
    assert "iswap_waveform" not in vendor.data["q1_q2"]   # vendor untouched
    with pytest.raises(Exception, match="expected a number"):
        view.write_knob("iswap_coupler_flux", [0.1, 0.2])
    assert vendor.data["q1_q2"]["iswap_coupler_flux"] == 0.0
    assert len(device.history()) == 1                     # only the dt row


def test_waveform_dt_pushes_before_waveform_in_push_seed(tmp_path, roster):
    order = []

    class OrderVendor(FakeVendor):
        def component(self, name):
            view = super().component(name)
            if name == "q1_q2":
                original = view.write_knob
                view.write_knob = lambda f, v: (order.append(f),
                                                original(f, v))
            return view

    seed = state_store(tmp_path, roster, setup="qm_a")
    seed.record("q1_q2", "iswap_waveform_dt_s", 5e-10)
    seed.record("q1_q2", "iswap_waveform", [0.0, 0.4])
    seed.save()
    RecordingDevice(OrderVendor({"q1_q2": {}}), roster,
                    state_store(tmp_path, roster), on_load="push")
    assert order.index("iswap_waveform_dt_s") < order.index("iswap_waveform")


def test_push_seed_reconciles_entities_without_power_anchor(tmp_path, roster):
    class EchoVendor(FakeVendor):
        def component(self, name):
            view = super().component(name)
            if name == "q1_q2":
                original = view.write_knob

                def echoing(f, v):
                    original(f, v)
                    if f == "iswap_coupler_flux":  # gate retiming echo
                        self.data[name]["iswap_duration_s"] = 4e-8
                return_view = view
                return_view.write_knob = echoing
            return view

    seed = state_store(tmp_path, roster, setup="qm_a")
    seed.record("q1_q2", "iswap_coupler_flux", 0.08)   # ONLY the flux point
    seed.save()
    store = state_store(tmp_path, roster)
    device = RecordingDevice(EchoVendor({"q1_q2": {}}), roster, store,
                             on_load="push")
    echo = [r for r in device.history() if r.coupled_to]
    assert echo and echo[0].field == "iswap_duration_s"
    assert echo[0].coupled_to == "iswap_coupler_flux"
    assert device.component("q1_q2").read_knob("iswap_duration_s") == 4e-8


def test_snapshot_includes_monitor_values(device):
    device.component("q1_ro").fidelity_g = 0.96
    snap = device.snapshot()
    assert snap["q1_ro"]["fidelity_g"] == 0.96
    assert snap["q1_xy"]["pi_amp"] == 0.209


def test_composite_reads_are_symmetric_with_channels(device):
    view = device.component("q1_q2")
    with pytest.raises(KeyError, match="anchor order"):
        view.read_knob("iswap_duration_s")      # unseeded knob: strict
    with pytest.raises(KeyError, match="physical.json"):
        view.read_knob("zz_hz")                 # fact: pointer, not None


def test_composite_attribute_writes_fail_loudly(device):
    with pytest.raises(AttributeError, match="write_knob"):
        device.component("q1_q2").iswap_coupler_flux = 0.1


def test_line_error_names_the_channels_riding_it(device):
    with pytest.raises(KeyError, match="q1_ro"):
        device.component("fl1")


def test_loud_error_lists_only_fields_not_kind(device):
    try:
        device.component("q1_xy").bogus = 1
    except AttributeError as err:
        assert "kind" not in str(err).split("fields:")[1]


def test_set_context_campaign_id_reaches_coupled_echoes(device, vendor):
    """The campaign stamp rides every row a write produces — including the
    chain-reconcile echo — and the plain two-arg reset clears it."""
    device.set_context("single_shot_readout", campaign_id="camp-1")
    device.component("q1_ro").readout_power_dbm = -20.0
    rows = {r.field: r for r in device.history()}
    assert rows["readout_power_dbm"].campaign_id == "camp-1"
    assert rows["readout_power_dbm"].run_id is None
    assert rows["readout_amp"].coupled_to == "readout_power_dbm"
    assert rows["readout_amp"].campaign_id == "camp-1"  # the echo inherits

    device.set_context(None, None)  # the existing reset clears every stamp
    device.component("q1_xy").pi_amp = 0.222
    assert {r.field: r for r in device.history()}["pi_amp"].campaign_id is None
