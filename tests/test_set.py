"""Runless recorded manual writes: Session.set_values + the `scqo set` command.

The suggest flow deliberately requires a run_id (credit the run whose data
justifies the value); set_values is the governed path for EXPERIENCE values with
no run behind them — validated like suggest, applied immediately through the
normal stores, recorded as `(manual)`.
"""

from __future__ import annotations

import json
import math
import sys
from types import SimpleNamespace

import pytest

from scqo.session import Session
from scqo.testing import InMemoryDevice, SimulatedBackend, demo_roster

QUBITS = {
    "q0": {"readout_freq": 5.9e9, "drive_freq": 4.8e9, "pi_amp": 0.2,
           "readout_amp": 0.1, "readout_power_dbm": -30.0},
    "q1": {"readout_freq": 6.1e9, "drive_freq": 4.9e9, "pi_amp": 0.25,
           "readout_amp": 0.12, "readout_power_dbm": -32.0},
}


def _session(tmp_path=None) -> tuple[Session, InMemoryDevice]:
    inner = InMemoryDevice(QUBITS)
    state = str(tmp_path / "scqo" / "scqo_state.json") if tmp_path is not None else None
    return Session(SimulatedBackend(inner), demo_roster(), state_path=state), inner


# ------------------------------------------------------------------ Session API

def test_set_instrument_field_pushes_records_and_persists(tmp_path):
    """A pushed field write reaches the vendor, records a runless manual
    ChangeRecord, shows `(manual)` in live sources, and lands in the state file."""
    sess, inner = _session(tmp_path)
    summary = sess.set_values({"q0.readout_freq": 4.9e9})

    assert summary["errors"] == []
    assert summary["applied"] == [{"component": "q0", "field": "readout_freq",
                                   "store": "instrument", "before": 5.9e9, "after": 4.9e9}]
    assert inner.snapshot()["q0"]["readout_freq"] == 4.9e9  # vendor carries it

    row = [r for r in sess.history() if r["field"] == "readout_freq"][-1]
    assert (row["old"], row["new"]) == (5.9e9, 4.9e9)
    assert row["experiment"] is None and row["run_id"] is None
    assert row["operator"]  # OS login stamped

    assert sess.live_sources()["instrument"]["q0"]["readout_freq"]["status"] == "manual"

    saved = json.loads((tmp_path / "scqo" / "scqo_state.json").read_text(encoding="utf-8"))
    assert saved["config"]["q0"]["readout_freq"] == 4.9e9


def test_set_physical_field_records_to_sample_ledger(tmp_path):
    sess, _ = _session(tmp_path)
    summary = sess.set_values({"q0.t1_s": 2.5e-05})

    assert summary["applied"][0]["store"] == "physical"
    assert summary["applied"][0]["before"] is None  # unmeasured until now
    assert sess.physical_state()["q0"]["t1_s"] == 2.5e-05
    assert sess.live_sources()["physical"]["q0"]["t1_s"]["status"] == "manual"

    saved = json.loads((tmp_path / "scqo" / "physical.json").read_text(encoding="utf-8"))
    assert saved["values"]["q0"]["t1_s"] == 2.5e-05


def test_set_satellite_component_field_routes_by_category(tmp_path):
    """A satellite name (q0_res) takes its own category's fields — the roster
    routes them to the physical ledger under that component's name."""
    sess, _ = _session(tmp_path)
    summary = sess.set_values({"q0_res.f_r_hz": 5.91e9})
    assert summary["applied"] == [{"component": "q0_res", "field": "f_r_hz",
                                   "store": "physical", "before": None, "after": 5.91e9}]
    assert sess.physical_state()["q0_res"]["f_r_hz"] == 5.91e9


def test_set_record_only_field_never_touches_vendor():
    sess, inner = _session()
    summary = sess.set_values({"q0.readout_fidelity": 0.93})
    assert summary["applied"][0]["store"] == "instrument"
    assert sess.device_state()["q0"]["readout_fidelity"] == 0.93
    assert "readout_fidelity" not in inner.snapshot()["q0"]  # no vendor knob


@pytest.mark.parametrize("assignments, match", [
    ({"q0.nope": 1.0}, "has no field"),
    ({"q9.pi_amp": 0.1}, "unknown component"),
    ({"badkey": 1.0}, "must be 'component.field'"),
    ({"q0.pi_amp": float("nan")}, "non-finite"),
    ({"q0.pi_amp": True}, "must be a number"),
    ({"q0.pi_amp": "0.2"}, "must be a number"),
    ({}, "no assignments"),
])
def test_set_validates_all_before_writing(assignments, match):
    """One bad assignment applies NOTHING — even valid siblings in the same call."""
    sess, inner = _session()
    combined = {"q0.readout_freq": 5.0e9, **assignments}
    with pytest.raises(ValueError, match=match):
        sess.set_values(combined if assignments else {})
    assert inner.snapshot()["q0"]["readout_freq"] == 5.9e9  # untouched
    assert sess.history() == []


def test_set_wrong_component_error_names_the_carrier():
    """Category-aware routing error: a field that lives on ANOTHER component's
    category names the component that carries it."""
    sess, _ = _session()
    with pytest.raises(ValueError) as err:
        sess.set_values({"q0.f_r_hz": 1.0})
    msg = str(err.value)
    assert "Resonator field" in msg and "q0_res.f_r_hz" in msg


def test_set_dry_run_reports_and_writes_nothing():
    sess, inner = _session()
    plan = sess.set_values({"q0.readout_freq": 4.9e9, "q0.t1_s": 2.5e-05}, dry_run=True)
    assert plan["items"] == [
        {"component": "q0", "field": "readout_freq", "store": "instrument", "unit": "Hz",
         "current": 5.9e9, "after": 4.9e9},
        {"component": "q0", "field": "t1_s", "store": "physical", "unit": "s",
         "current": None, "after": 2.5e-05},
    ]
    assert inner.snapshot()["q0"]["readout_freq"] == 5.9e9
    assert sess.history() == [] and sess.physical_state() == {}


class _RejectingDevice(InMemoryDevice):
    """Vendor device whose drive_freq knob refuses writes (an out-of-range rejection)."""

    def component(self, name: str):
        view = super().component(name)

        class _V:
            def __getattr__(self, attr):
                return getattr(view, attr)

            def __setattr__(self, attr, value):
                if attr == "drive_freq":
                    raise ValueError("vendor rejects this value")
                setattr(view, attr, value)

        return _V()


def test_set_per_component_atomicity_on_vendor_rejection():
    """A vendor rejection skips that component's REMAINING items; other components proceed."""
    sess = Session(SimulatedBackend(_RejectingDevice(QUBITS)), demo_roster())
    summary = sess.set_values({
        "q0.drive_freq": 1.0e9,   # vendor raises
        "q0.pi_amp": 0.3,         # same component: skipped
        "q1.pi_amp": 0.4,         # other component: proceeds
    })
    assert [f"{a['component']}.{a['field']}" for a in summary["applied"]] == ["q1.pi_amp"]
    assert len(summary["errors"]) == 1 and "q0.drive_freq" in summary["errors"][0]
    assert sess.device_state()["q0"]["pi_amp"] == 0.2  # untouched
    assert sess.device_state()["q1"]["pi_amp"] == 0.4


class _VendorAwareBackend(SimulatedBackend):
    """Simulated backend with a vendor-only catalog, for the vendor-aware error."""

    def vendor_only(self):
        from scqo.fieldmap import VendorOnly

        return {
            "lo_knob": VendorOnly(path="hw.lo_freq", unit="Hz", kind="vendor",
                                  doc="port-shared LO - edit offline, no live session"),
            "att_knob": VendorOnly(path="hw.att", unit="dB", kind="realizer",
                                   doc="realizes readout_power_dbm - change it with "
                                       "scqo set QUBIT.readout_power_dbm=..."),
        }


def test_set_unknown_field_is_vendor_aware():
    """A vendor-only name gets THAT catalog entry as the error (path + doc +
    kind), not the bare known-field dump — the rule at the moment of failure."""
    sess = Session(_VendorAwareBackend(InMemoryDevice(QUBITS)), demo_roster())
    with pytest.raises(ValueError) as err:
        sess.set_values({"q0.lo_knob": 5e9})
    msg = str(err.value)
    assert "vendor parameter" in msg and "(kind: vendor)" in msg
    assert "hw.lo_freq [Hz]" in msg and "port-shared LO" in msg
    assert "scqo state --fields" in msg and "scqo state --rule" in msg

    with pytest.raises(ValueError) as err:  # realizer: doc names the governed route
        sess.set_values({"q0.att_knob": 10})
    assert "scqo set QUBIT.readout_power_dbm" in str(err.value)

    with pytest.raises(ValueError, match="has no field"):  # non-catalog fallback
        sess.set_values({"q0.totally_bogus": 1.0})


# ------------------------------------------------------------------------- CLI

def test_cli_set_non_tty_refuses_without_yes(capsys):
    from scqo.cli import set as set_cli

    assert set_cli.main(["q0.readout_freq=5.91e9"]) == 1
    captured = capsys.readouterr()
    assert "not a terminal - nothing written" in captured.err
    assert "will write:" in captured.err  # the preview still shows
    assert "applied" not in captured.out  # no JSON summary on stdout


def test_cli_set_yes_applies_and_prints_json(capsys):
    from scqo.cli import set as set_cli

    assert set_cli.main(["q0.readout_freq=5.91e9", "--yes"]) == 0
    captured = capsys.readouterr()
    summary = json.loads(captured.out)
    assert summary["applied"][0]["after"] == 5.91e9 and summary["errors"] == []
    assert "built-in demo device" in captured.err  # target named before writing
    assert "applied  q0.readout_freq" in captured.err


class _TTYWrapper:
    """Delegate writes to the captured stream but claim to be a terminal."""

    def __init__(self, wrapped):
        self._wrapped = wrapped

    def write(self, *args, **kwargs):
        return self._wrapped.write(*args, **kwargs)

    def flush(self):
        return self._wrapped.flush()

    def isatty(self) -> bool:
        return True


@pytest.mark.parametrize("answer, expect_written", [("y", True), ("", False)])
def test_cli_set_tty_confirmation_gate(capsys, monkeypatch, answer, expect_written):
    """At a terminal: y applies, plain Enter (the default) writes nothing."""
    from scqo.cli import _review
    from scqo.cli import set as set_cli

    monkeypatch.setattr(sys, "stdin", _TTYWrapper(sys.stdin))
    monkeypatch.setattr(sys, "stderr", _TTYWrapper(sys.stderr))
    monkeypatch.setattr(_review, "_ask", lambda prompt: answer)

    assert set_cli.main(["q0.readout_freq=5.91e9"]) == 0
    captured = capsys.readouterr()
    if expect_written:
        assert json.loads(captured.out)["applied"]
    else:
        assert "nothing written" in captured.err
        assert captured.out == ""


def test_cli_set_bad_input_exits_cleanly():
    from scqo.cli import set as set_cli

    with pytest.raises(SystemExit, match="bad assignment"):
        set_cli.main(["q0.readout_freq"])  # no '=VALUE'
    with pytest.raises(SystemExit, match="has no field"):
        set_cli.main(["q0.bogus=1", "--yes"])


def test_cli_set_registered_in_dispatcher():
    from scqo.cli.__main__ import _COMMANDS

    assert "set" in _COMMANDS and _COMMANDS["set"][0] == "set"
