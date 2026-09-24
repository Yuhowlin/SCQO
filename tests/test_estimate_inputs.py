"""The frozen estimate surface and the snapshot dataset.nc carries.

``estimate()`` never reads the live device: it reads the values the data was
ACQUIRED with, rebuilt from the dataset's own attrs. Two properties carry the
whole feature and are pinned here:

* the frozen surface answers reads exactly as :class:`RecordingDevice` does
  (it hands out the same generated views — this test is what keeps that true
  if anyone reimplements them), and refuses every write by name;
* what is embedded at acquisition time is what the fit reads back.

The live half — that routing all 45 experiments through the frozen surface did
not change a single number — is pinned by the rest of the suite, which now runs
every offline experiment through it.
"""

from __future__ import annotations

import ast
import json
import math
from pathlib import Path

import numpy as np
import pytest
import xarray as xr

from scqo import RecordingDevice, parse_components, state_store
from scqo.design import Design
from scqo.estimate_inputs import (
    ATTR_DEVICE,
    ATTR_PHYSICAL,
    ATTR_SCHEMA,
    SCHEMA,
    FrozenDevice,
    FrozenWriteError,
    MissingEmbeddedInputs,
    acquisition_note,
    embed,
    is_embedded,
    load_frozen,
    note_acquisition,
    strip_attrs,
)
from tests.test_model_device import FakeVendor
from tests.test_model_roster import EXAMPLE

EXPERIMENTS = Path(__file__).resolve().parent.parent / "scqo" / "experiments"


@pytest.fixture()
def roster():
    return parse_components(EXAMPLE)


@pytest.fixture()
def live(tmp_path, roster):
    """A RecordingDevice with knobs seeded, one monitor measured, one entity
    left entirely unseeded (q2_xy) — the three read outcomes in one device."""
    vendor = FakeVendor({
        "q1_xy": {"drive_freq_hz": 5.136e9, "pi_amp": 0.209},
        "q1_ro": {"readout_freq_hz": 5.934e9, "readout_amp": 0.112,
                  "readout_power_dbm": -30.0},
        "q1_z": {"idle_flux": 0.118},
        "q1_q2": {"iswap_coupler_flux": 0.031},
    })
    device = RecordingDevice(vendor, roster,
                            state_store(tmp_path, roster, setup="qm_a"))
    device.component("q1_ro").fidelity_g = 0.96  # a measured monitor
    return device


@pytest.fixture()
def frozen(live, roster):
    return FrozenDevice(roster, live.snapshot())


def _read(device, entity, field):
    """One read, as its value or the name of the exception it raised."""
    try:
        view = device.component(entity)
    except Exception as err:  # noqa: BLE001 - the outcome IS what we compare
        return f"component:{type(err).__name__}"
    try:
        if hasattr(view, "read_knob"):
            return view.read_knob(field)
        return getattr(view, field)
    except Exception as err:  # noqa: BLE001
        return f"read:{type(err).__name__}"


# -------------------------------------------------- reads: live == frozen

def test_every_read_matches_the_recording_device(live, frozen, roster):
    """Field by field, over every entity in the roster: value for value,
    exception for exception. A frozen surface that read differently would
    change what live runs fit, silently."""
    compared = 0
    for entity in roster.entities:
        for field in roster.fields_of(entity):
            expected = _read(live, entity, field)
            assert _read(frozen, entity, field) == expected, (entity, field)
            compared += 1
    assert compared > 20  # the roster really was walked


def test_unseeded_knob_raises_keyerror_so_anchor_still_falls_back(frozen):
    # anchor() catches (KeyError, AttributeError) and drops to the design
    # seed; any other exception type would break that fallback.
    with pytest.raises(KeyError, match="acquired"):
        frozen.component("q2_xy").drive_freq_hz


def test_monitor_reads_none_when_it_was_never_measured(frozen):
    assert frozen.component("q1_ro").fidelity_e is None


def test_measured_monitor_comes_back(frozen):
    assert frozen.component("q1_ro").fidelity_g == 0.96


def test_composite_knob_reads_through_read_knob(frozen):
    assert frozen.component("q1_q2").read_knob("iswap_coupler_flux") == 0.031


def test_fact_reads_still_point_at_the_physical_store(frozen):
    with pytest.raises(KeyError, match="physical.json"):
        frozen.component("q1_q2").read_knob("zz_hz")


def test_channel_and_resonator_addressing_match(live, frozen):
    assert frozen.channel("q1", "readout").readout_freq_hz == 5.934e9
    assert frozen.resonator_of("q1") == live.resonator_of("q1")


# ------------------------------------------------------------ writes refused

def test_knob_write_is_refused_by_name(frozen):
    with pytest.raises(FrozenWriteError, match="q1_xy.pi_amp"):
        frozen.component("q1_xy").pi_amp = 0.3


def test_monitor_write_is_refused_by_name(frozen):
    with pytest.raises(FrozenWriteError, match="fidelity_g"):
        frozen.component("q1_ro").fidelity_g = 0.99


def test_composite_write_is_refused_by_name(frozen):
    with pytest.raises(FrozenWriteError, match="iswap_coupler_flux"):
        frozen.component("q1_q2").write_knob("iswap_coupler_flux", 0.2)


def test_save_is_refused(frozen):
    with pytest.raises(FrozenWriteError):
        frozen.save()


# ------------------------------------------------------------ embed / load

def _dataset():
    return xr.Dataset({"I": ("target", np.array([0.1, 0.2]))},
                      coords={"target": ["q1", "q2"]})


class _Params:
    def model_dump(self, mode="json"):
        return {"targets": ["q1"], "num_averages": 100}


def test_round_trip_rebuilds_all_three_surfaces(live, roster):
    ds = _dataset()
    embed(ds, experiment="resonator_spectroscopy", params=_Params(),
          device=live.snapshot(), physical={"q1_res": {"kappa_tot_hz": 1.2e6}},
          design=Design({"q1_res": {"f_bare_hz": 5.9e9}}))
    inputs = load_frozen(ds, roster)
    assert inputs.experiment == "resonator_spectroscopy"
    assert inputs.parameters["num_averages"] == 100
    assert inputs.device.component("q1_xy").drive_freq_hz == 5.136e9
    assert inputs.physical.get("q1_res", "kappa_tot_hz") == 1.2e6
    assert inputs.design.get("q1_res", "f_bare_hz") == 5.9e9
    assert ("device", "q1_xy", "drive_freq_hz") in inputs.reads()
    assert ("physical", "q1_res", "kappa_tot_hz") in inputs.reads()
    assert ("design", "q1_res", "f_bare_hz") in inputs.reads()


def test_absent_facts_stay_absent_rather_than_becoming_empty(live, roster):
    """``physical is None`` is the standalone case, and ``fact_sourced`` skips
    the measured tier for it. An empty store instead would answer every fact
    "nothing measured" — the same answer for "no store" and "not calibrated"."""
    ds = _dataset()
    embed(ds, experiment="x", params=_Params(), device=live.snapshot(),
          physical=None, design=Design({}))
    assert load_frozen(ds, roster).physical is None


def test_a_dataset_without_the_snapshot_is_refused_by_name(roster):
    with pytest.raises(MissingEmbeddedInputs, match="run_estimate"):
        load_frozen(_dataset(), roster)


def test_an_unknown_schema_is_refused_rather_than_guessed(live, roster):
    ds = _dataset()
    embed(ds, experiment="x", params=_Params(), device=live.snapshot(),
          physical={}, design=Design({}))
    ds.attrs[ATTR_SCHEMA] = SCHEMA + 1
    with pytest.raises(MissingEmbeddedInputs, match="immutable"):
        load_frozen(ds, roster)


def test_a_non_finite_value_never_costs_the_measurement(live, roster, capsys):
    """Provenance degrades; the run survives. (The store rejects NaN, so this
    is the path a hand-built Parameters or a driver monitor could take.)"""
    ds = _dataset()
    snapshot = live.snapshot()
    snapshot["q1_xy"]["pi_amp"] = float("nan")
    embed(ds, experiment="x", params=_Params(), device=snapshot,
          physical={}, design=Design({}))
    assert "could not embed" in capsys.readouterr().err
    inputs = load_frozen(ds, roster)
    with pytest.raises(KeyError):  # scrubbed to None == "had no value"
        inputs.device.component("q1_xy").pi_amp
    assert inputs.device.component("q1_xy").drive_freq_hz == 5.136e9


def test_is_embedded_is_the_witness_session_run_checks(live):
    ds = _dataset()
    assert not is_embedded(ds)
    embed(ds, experiment="x", params=_Params(), device=live.snapshot(),
          physical={}, design=Design({}))
    assert is_embedded(ds)


# ------------------------------------------------------- acquisition notes

def test_acquisition_notes_round_trip_through_the_dataset():
    ds = _dataset()
    assert acquisition_note(ds, "t1_prior_s", {}) == {}
    note_acquisition(ds, "t1_prior_s", {"q1": 2.5e-5})
    note_acquisition(ds, "top_amps", {"q1": 0.1})
    assert acquisition_note(ds, "t1_prior_s") == {"q1": 2.5e-5}
    assert acquisition_note(ds, "top_amps") == {"q1": 0.1}


# ------------------------------------------------------------- scqat edge

def test_scqat_sees_a_dataset_without_the_embedding(live):
    ds = _dataset()
    ds.attrs["units"] = "V"  # someone else's attr survives
    embed(ds, experiment="x", params=_Params(), device=live.snapshot(),
          physical={}, design=Design({}))
    note_acquisition(ds, "top_amps", {"q1": 0.1})
    stripped = strip_attrs(ds)
    assert not [k for k in stripped.attrs if k.startswith("scqo_")]
    assert stripped.attrs["units"] == "V"
    assert list(stripped.data_vars) == list(ds.data_vars)
    assert is_embedded(ds)  # the original is untouched


# ------------------------------------------------------------- enforcement

def test_no_experiment_calls_estimate_directly():
    """``run_estimate()`` is the only caller of ``estimate()``.

    A ``run()`` override that calls ``estimate()`` itself gets the live device
    and writes no snapshot — the dataset silently stops being self-contained.
    (Session.run also reports it at runtime, for forks this test cannot see.)
    """
    offenders = []
    for path in sorted(EXPERIMENTS.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if (isinstance(func, ast.Attribute) and func.attr == "estimate"
                    and isinstance(func.value, ast.Name)
                    and func.value.id == "self"):
                offenders.append(f"{path.stem}:{node.lineno}")
    assert not offenders, (
        f"experiment(s) calling self.estimate() directly: {offenders}. Call "
        f"self.run_estimate() — it embeds the acquisition-time snapshot and "
        f"runs estimate() against it."
    )


# ------------------------------------------------- the real end-to-end shape

@pytest.fixture()
def session(tmp_path):
    from scqo import Session
    from scqo.testing import (
        InMemoryDevice,
        SimulatedBackend,
        demo_components,
        demo_design,
        demo_vendor_state,
    )

    roster = demo_components()
    design = demo_design(roster)
    vendor = InMemoryDevice(roster, demo_vendor_state(roster, design))
    return Session(
        SimulatedBackend(vendor), roster, design=design,
        scqo_dir=tmp_path / "scqo", data_root=tmp_path / "data",
        device_name="chipT", backend_label="simulated",
        setup_name="sim", cooldown_id="cd1")


def test_a_persisted_run_is_self_contained(session):
    out = session.run("resonator_spectroscopy",
                      {"targets": ["q0", "q1"], "num_readout_freq_points": 41})
    assert out.get("error") is None and "dataset_warning" not in out

    ds = session.datastore.open_dataset(out["run_id"])
    assert ds.attrs[ATTR_SCHEMA] == SCHEMA
    device = json.loads(ds.attrs[ATTR_DEVICE])
    assert device["q0_ro"]["readout_freq_hz"] > 0
    run = json.loads(ds.attrs["scqo_run"])
    assert run["run_id"] == out["run_id"] and run["cooldown"] == "cd1"
    assert run["versions"]["scqo"] and run["versions"]["scqat"]

    record = session.load_run(out["run_id"])["record"]
    assert record["versions"] == run["versions"]


def test_the_embedded_snapshot_is_the_pre_update_one(session):
    """The snapshot is taken before ``update()`` moves anything — otherwise a
    re-fit would read back the value this very run proposed."""
    out = session.run("resonator_spectroscopy", {"targets": ["q0"]},
                      update="apply")
    ds = session.datastore.open_dataset(out["run_id"])
    embedded = json.loads(ds.attrs[ATTR_DEVICE])["q0_ro"]["readout_freq_hz"]
    applied = session.device_state()["q0_ro"]["readout_freq_hz"]
    assert embedded != applied
    assert json.loads(ds.attrs[ATTR_PHYSICAL]) == {}  # facts were empty at acquisition


def test_the_embedding_stays_far_below_the_hdf5_attribute_limit(session):
    """HDF5 keeps an attribute compact only up to 64 KB. The demo device sits
    around 10 KB; a change that pushed it past this would need the per-entity
    layout instead (and a SCHEMA bump)."""
    out = session.run("resonator_spectroscopy", {"targets": ["q0", "q1"]})
    ds = session.datastore.open_dataset(out["run_id"])
    biggest = max(len(str(v)) for k, v in ds.attrs.items()
                  if k.startswith("scqo_"))
    assert biggest < 32 * 1024


def test_estimate_runs_frozen_and_hands_the_live_surface_back(session):
    """The swap is a loan: update() must find the REAL device afterwards."""
    from scqo import experiments as registry

    cls = registry.get("resonator_spectroscopy")
    exp = cls(session.backend, cls.Parameters(targets=["q0"]))
    exp.device, exp.design = session.device, session.design
    exp.physical = session.physical
    result = exp.run()

    assert result.error is None
    # the detuning -> absolute-frequency anchor is the read this whole feature
    # exists for, and it came off the frozen surface
    assert ("device", "q0_ro", "readout_freq_hz") in exp.inputs_used
    assert exp.device is session.device
    assert exp.physical is session.physical
    assert exp.design is session.design
