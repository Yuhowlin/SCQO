"""Device-layer contracts (scqo.device): recording semantics over a
fake vendor — pull/push seeding, push-first writes, coupled echoes (on one
channel and across a shared wire), monitors never touching the vendor, the
line / channel / operation surfaces, and borrowed routes that exist only once
the vendor adopts them."""

import pytest

from scqo import (
    DeviceModel,
    OperationView,
    RecordingDevice,
    RosterError,
    parse_components,
    state_store,
)
from tests.test_model_roster import EXAMPLE


class _FakeOperation(OperationView):
    def __init__(self, vendor, name):
        self.name = name
        self.kind = "operation"
        self._vendor, self._name = vendor, name

    def read_knob(self, field):
        return self._vendor.data[self._name].get(field)

    def write_knob(self, field, value):
        self._vendor.data[self._name][field] = value


class _FakeChannel:
    """Duck-typed channel/line view over the vendor dict (real drivers
    subclass make_view_base / make_line_view_base; the recorder only needs
    getattr/setattr)."""

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
    #: the entities served through the generic operation surface
    OPERATIONS = frozenset({"q1_q2.iswap"})

    def __init__(self, data):
        self.data = data
        self.saved = 0

    def component(self, name):
        if name not in self.data:
            raise KeyError(name)
        if name in self.OPERATIONS:
            return _FakeOperation(self, name)
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
        "xy1.q1": {"drive_freq_hz": 5.136e9, "pi_amp": 0.209},
        "fl1.q1": {"readout_freq_hz": 5.934e9, "readout_amp": 0.112,
                   "readout_power_dbm": -30.0},
        "z1": {"idle_flux": 0.118},
        "q1_q2.iswap": {"coupler_flux": 0.0},
    })


@pytest.fixture()
def device(tmp_path, roster, vendor):
    return RecordingDevice(vendor, roster,
                           state_store(tmp_path, roster, setup="qm_a"))


# ------------------------------------------------------------ pull seeding

def test_pull_seeds_from_the_vendor_without_history(device):
    assert device.component("xy1.q1").drive_freq_hz == 5.136e9
    assert device.line("z1").idle_flux == 0.118
    assert device.history() == ()          # seeding is not a change


def test_unseeded_knob_reads_strict_with_anchor_pointer(device):
    with pytest.raises(KeyError, match="anchor order"):
        device.component("xyz2.q2").drive_freq_hz


def test_monitors_read_none_until_measured(device):
    assert device.component("fl1.q1").fidelity_g is None


# ------------------------------------------------------------------ writes

def test_write_pushes_vendor_first_then_records(device, vendor):
    device.set_context("qubit_power_rabi", run_id="r7")
    device.component("xy1.q1").pi_amp = 0.222
    assert vendor.data["xy1.q1"]["pi_amp"] == 0.222
    row = device.history()[0]
    assert (row.entity, row.field, row.new) == ("xy1.q1", "pi_amp", 0.222)
    assert row.experiment == "qubit_power_rabi" and row.run_id == "r7"
    assert row.kind == "drive"


def test_vendor_rejection_leaves_no_false_history(device, vendor):
    with pytest.raises(ValueError, match="out of range"):
        device.component("xy1.q1").pi_amp = 7.0
    assert device.history() == ()
    assert vendor.data["xy1.q1"]["pi_amp"] == 0.209


def test_coupled_echo_is_recorded_with_attribution(device, vendor):
    device.component("fl1.q1").readout_power_dbm = -20.0
    rows = {(r.field): r for r in device.history()}
    assert rows["readout_power_dbm"].coupled_to is None
    assert rows["readout_amp"].coupled_to == "readout_power_dbm"
    assert device.component("fl1.q1").readout_amp == pytest.approx(
        vendor.data["fl1.q1"]["readout_amp"])


class _SharedPortChannel(_FakeChannel):
    """A readout channel on a port whose full scale every channel of the
    feedline shares: re-solving one channel's power rescales the others."""

    def __setattr__(self, field, value):
        super().__setattr__(field, value)
        if field == "readout_power_dbm":
            for name, knobs in self._vendor.data.items():
                if name != self._name and name.startswith("fl1."):
                    knobs["readout_amp"] = knobs["readout_amp"] / 2


def test_a_port_level_echo_on_a_wire_mate_is_recorded(tmp_path, roster):
    """One vendor setting can be port-level (an MW-FEM full scale shared by
    every readout on the feedline): re-solving q1's readout power moves q2's
    amplitude too. The recorder re-reads every owner on the written entity's
    LINE, so that change is recorded on q2's channel, attributed to the write
    that caused it — never silent."""
    class PortVendor(FakeVendor):
        def component(self, name):
            if name.startswith("fl1.") and name in self.data:
                return _SharedPortChannel(self, name)
            return super().component(name)

    vendor = PortVendor({
        "fl1.q1": {"readout_amp": 0.112, "readout_power_dbm": -30.0},
        "fl1.q2": {"readout_amp": 0.1},
        "xy1.q1": {"pi_amp": 0.209},
    })
    device = RecordingDevice(vendor, roster, state_store(tmp_path, roster))
    device.channel("q1", "readout").readout_power_dbm = -20.0
    rows = {(r.entity, r.field): r for r in device.history()}
    assert rows[("fl1.q1", "readout_amp")].coupled_to == "readout_power_dbm"
    mate = rows[("fl1.q2", "readout_amp")]
    assert mate.new == 0.05
    assert mate.coupled_to == "fl1.q1.readout_power_dbm"  # entity.field
    assert device.component("fl1.q2").readout_amp == 0.05
    # another line is another wire: never re-read, never recorded
    assert {r.entity for r in device.history()} == {"fl1.q1", "fl1.q2"}


def test_monitor_write_records_but_never_touches_the_vendor(device, vendor):
    device.component("fl1.q1").fidelity_g = 0.96
    assert "fidelity_g" not in vendor.data["fl1.q1"]
    assert device.component("fl1.q1").fidelity_g == 0.96


def test_unknown_field_write_fails_loudly(device):
    with pytest.raises(AttributeError, match="no field"):
        device.component("xy1.q1").readout_freq_hz = 5.9e9


# -------------------------------------------------------------- flux lines

def test_driver_view_bases_split_line_knobs_from_channel_knobs():
    """A driver implements one view per channel kind (that channel's knobs
    only) and one per kind of LINE knobs (a flux line's bias and delay)."""
    from scqo import make_line_view_base, make_view_base
    assert make_line_view_base("flux").__abstractmethods__ == {
        "idle_flux", "flux_delay_s"}
    assert not make_view_base("flux").__abstractmethods__   # facts only
    drive = make_view_base("drive").__abstractmethods__
    assert {"drive_freq_hz", "pi_amp"} <= drive
    assert not {"idle_flux", "flux_delay_s"} & drive


def test_the_flux_bias_is_the_lines_knob(device, vendor):
    """idle_flux is one DC offset per WIRE: the line z1 owns it (flux_line
    reaches it from q1), while the channel z1.q1 keeps only q1's transfer
    function — facts, which never ride the device."""
    line = device.flux_line("q1")
    assert line.idle_flux == 0.118
    line.idle_flux = 0.12
    assert vendor.data["z1"]["idle_flux"] == 0.12
    row = device.history()[-1]
    assert (row.entity, row.field, row.kind) == ("z1", "idle_flux", "line")
    assert device.line("z1").idle_flux == 0.12
    with pytest.raises(AttributeError, match="no field 'idle_flux'"):
        device.component("z1.q1").idle_flux = 0.1
    # q2's combined drive+flux wire is its flux line
    assert device.flux_line("q2").name == "xyz2"


# -------------------------------------------------------------- operations

def test_operation_knobs_via_the_generic_surface(device, vendor):
    view = device.operation("q1_q2", "iswap")
    view.write_knob("coupler_flux", 0.081)
    assert vendor.data["q1_q2.iswap"]["coupler_flux"] == 0.081
    assert view.read_knob("coupler_flux") == 0.081
    row = device.history()[0]
    assert (row.entity, row.field, row.kind) == (
        "q1_q2.iswap", "coupler_flux", "operation")


def test_a_composite_has_no_view_its_knobs_live_on_its_operations(device):
    with pytest.raises(KeyError, match=r"q1_q2\.iswap"):
        device.component("q1_q2")


def test_undeclared_operation_is_exact_cause(device):
    with pytest.raises(KeyError, match="operation 'cz' is not declared"):
        device.operation("q1_q2", "cz")


# ------------------------------------------------------------ entity kinds

def test_modes_carry_no_knobs_and_the_error_names_channels(device):
    with pytest.raises(KeyError, match=r"xy1\.q1"):
        device.component("q1")
    with pytest.raises(KeyError, match="unknown entity"):
        device.component("ghost")


# -------------------------------------------------------- borrowed routes

def test_a_borrowed_channel_is_unrealized_until_adopted(tmp_path, roster):
    """A coupler driven through q1's line needs no declaration, but the vendor
    must carry an element for that route before its knobs exist: until then a
    read has no value and a write is the driver's refusal, with no history.
    Adopted, it is an ordinary channel — minus what belongs to the coupler
    itself — and still never a default."""
    vendor = FakeVendor({"xy1.q1": {"pi_amp": 0.209}})
    device = RecordingDevice(vendor, roster, state_store(tmp_path, roster))
    route = device.channel_on("xy1", "q1_q2_c")
    with pytest.raises(KeyError, match="no value yet"):
        route.pi_amp
    with pytest.raises(KeyError, match=r"xy1\.q1_q2_c"):
        route.pi_amp = 0.3                      # the vendor has no element
    assert device.history() == ()

    vendor.data["xy1.q1_q2_c"] = {"drive_freq_hz": 7.1e9}   # adopted
    device = RecordingDevice(vendor, roster, state_store(tmp_path, roster))
    route = device.channel_on("xy1", "q1_q2_c")
    assert route.drive_freq_hz == 7.1e9
    route.pi_amp = 0.3
    assert vendor.data["xy1.q1_q2_c"]["pi_amp"] == 0.3
    row = device.history()[-1]
    assert (row.entity, row.kind) == ("xy1.q1_q2_c", "drive")
    with pytest.raises(AttributeError, match="no field"):
        route.thermalization_time_s = 1e-4      # the coupler's own business
    with pytest.raises(RosterError, match="no unique drive channel"):
        device.channel("q1_q2_c", "drive")


# --------------------------------------------------------------- push mode

def test_push_mode_restores_saved_knobs_into_the_vendor(tmp_path, roster,
                                                        vendor):
    seed = state_store(tmp_path, roster, setup="qm_a")
    seed.record("xy1.q1", "pi_amp", 0.333)
    seed.record("z1", "idle_flux", 0.2)
    seed.save()
    device = RecordingDevice(
        vendor, roster, state_store(tmp_path, roster, setup="qm_a"),
        on_load="push")
    assert vendor.data["xy1.q1"]["pi_amp"] == 0.333   # SCQO won
    assert vendor.data["z1"]["idle_flux"] == 0.2      # lines too
    # store-silent knobs backfill from the vendor snapshot:
    assert device.component("xy1.q1").drive_freq_hz == 5.136e9


def test_push_mode_tolerates_unrealized_knobs(tmp_path, roster):
    """A knob the backend declares Unrealized raises NotImplementedError —
    capability, not failure: seeding skips it and keeps going."""
    class PartialVendor(FakeVendor):
        def component(self, name):
            view = super().component(name)
            if name == "xy1.q1":
                original = view.__setattr__

                class _Refusing:
                    name = "xy1.q1"

                    def __setattr__(self, field, value):
                        if field == "drag_beta":
                            raise NotImplementedError("no DRAG on this box")
                        original(field, value)

                    def __getattr__(self, field):
                        return getattr(view, field)
                return _Refusing()
            return view

    seed = state_store(tmp_path, roster, setup="qm_a")
    seed.record("xy1.q1", "drag_beta", -1.0)     # the unrealized one
    seed.record("xy1.q1", "pi_amp", 0.44)        # must still land
    seed.save()
    vendor = PartialVendor({"xy1.q1": {"pi_amp": 0.1}})
    device = RecordingDevice(vendor, roster,
                             state_store(tmp_path, roster, setup="qm_a"),
                             on_load="push")      # must not raise
    assert vendor.data["xy1.q1"]["pi_amp"] == 0.44
    assert device.component("xy1.q1").pi_amp == 0.44


def test_push_mode_tolerates_unrealized_entities(tmp_path, roster, vendor):
    seed = state_store(tmp_path, roster, setup="qm_a")
    seed.record("xy3.q3", "pi_amp", 0.5)              # vendor has no xy3.q3
    seed.record("xy1.q1_q2_c", "pi_amp", 0.12)        # a route never adopted
    seed.save()
    device = RecordingDevice(
        vendor, roster, state_store(tmp_path, roster, setup="qm_a"),
        on_load="push")                               # must not raise
    assert device.component("xy1.q1").pi_amp == 0.209
    assert "xy1.q1_q2_c" not in vendor.data           # nothing invented


def test_save_persists_vendor_and_store(device, vendor, tmp_path):
    device.component("xy1.q1").pi_amp = 0.25
    device.save()
    assert vendor.saved == 1
    assert (tmp_path / "scqo_state.json").is_file()
    again = state_store(tmp_path, roster=device.roster)
    assert again.get("xy1.q1", "pi_amp") == 0.25


# ----------------------------------------------- review-added regressions

def test_store_invalid_values_never_reach_the_vendor(device, vendor):
    view = device.operation("q1_q2", "iswap")
    view.write_knob("waveform_dt_s", 5e-10)
    with pytest.raises(Exception, match="non-finite"):
        view.write_knob("waveform", [0.1, float("nan")])
    assert "waveform" not in vendor.data["q1_q2.iswap"]   # vendor untouched
    with pytest.raises(Exception, match="expected a number"):
        view.write_knob("coupler_flux", [0.1, 0.2])
    assert vendor.data["q1_q2.iswap"]["coupler_flux"] == 0.0
    assert len(device.history()) == 1                     # only the dt row


def test_waveform_dt_pushes_before_waveform_in_push_seed(tmp_path, roster):
    order = []

    class OrderVendor(FakeVendor):
        def component(self, name):
            view = super().component(name)
            if name == "q1_q2.iswap":
                original = view.write_knob
                view.write_knob = lambda f, v: (order.append(f),
                                                original(f, v))
            return view

    seed = state_store(tmp_path, roster, setup="qm_a")
    seed.record("q1_q2.iswap", "waveform_dt_s", 5e-10)
    seed.record("q1_q2.iswap", "waveform", [0.0, 0.4])
    seed.save()
    RecordingDevice(OrderVendor({"q1_q2.iswap": {}}), roster,
                    state_store(tmp_path, roster), on_load="push")
    assert order.index("waveform_dt_s") < order.index("waveform")


def test_push_seed_reconciles_entities_without_power_anchor(tmp_path, roster):
    class EchoVendor(FakeVendor):
        def component(self, name):
            view = super().component(name)
            if name == "q1_q2.iswap":
                original = view.write_knob

                def echoing(f, v):
                    original(f, v)
                    if f == "coupler_flux":  # gate retiming echo
                        self.data[name]["duration_s"] = 4e-8
                view.write_knob = echoing
            return view

    seed = state_store(tmp_path, roster, setup="qm_a")
    seed.record("q1_q2.iswap", "coupler_flux", 0.08)   # ONLY the flux point
    seed.save()
    store = state_store(tmp_path, roster)
    device = RecordingDevice(EchoVendor({"q1_q2.iswap": {}}), roster, store,
                             on_load="push")
    echo = [r for r in device.history() if r.coupled_to]
    assert echo and echo[0].field == "duration_s"
    assert echo[0].coupled_to == "coupler_flux"
    assert device.operation("q1_q2", "iswap").read_knob("duration_s") == 4e-8


def test_snapshot_includes_monitor_values(device):
    device.component("fl1.q1").fidelity_g = 0.96
    snap = device.snapshot()
    assert snap["fl1.q1"]["fidelity_g"] == 0.96
    assert snap["xy1.q1"]["pi_amp"] == 0.209
    # flat, keyed by owner: lines and operations beside channels
    assert snap["z1"]["idle_flux"] == 0.118
    assert snap["q1_q2.iswap"]["coupler_flux"] == 0.0


def test_operation_reads_are_symmetric_with_channels(device):
    view = device.operation("q1_q2", "iswap")
    with pytest.raises(KeyError, match="anchor order"):
        view.read_knob("duration_s")      # unseeded knob: strict
    with pytest.raises(RosterError, match="unknown field"):
        view.read_knob("zz_hz")           # the pair's fact: refused, not None


def test_operation_attribute_writes_fail_loudly(device):
    with pytest.raises(AttributeError, match="write_knob"):
        device.operation("q1_q2", "iswap").coupler_flux = 0.1


def test_line_error_names_the_channels_riding_it(device):
    with pytest.raises(KeyError, match=r"fl1\.q1"):
        device.component("fl1")


def test_loud_error_lists_only_fields_not_kind(device):
    try:
        device.component("xy1.q1").bogus = 1
    except AttributeError as err:
        assert "kind" not in str(err).split("fields:")[1]


def test_set_context_campaign_id_reaches_coupled_echoes(device, vendor):
    """The campaign stamp rides every row a write produces — including the
    chain-reconcile echo — and the plain two-arg reset clears it."""
    device.set_context("single_shot_readout", campaign_id="camp-1")
    device.component("fl1.q1").readout_power_dbm = -20.0
    rows = {r.field: r for r in device.history()}
    assert rows["readout_power_dbm"].campaign_id == "camp-1"
    assert rows["readout_power_dbm"].run_id is None
    assert rows["readout_amp"].coupled_to == "readout_power_dbm"
    assert rows["readout_amp"].campaign_id == "camp-1"  # the echo inherits

    device.set_context(None, None)  # the existing reset clears every stamp
    device.component("xy1.q1").pi_amp = 0.222
    assert {r.field: r for r in device.history()}["pi_amp"].campaign_id is None
