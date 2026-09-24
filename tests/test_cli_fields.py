"""`scqo state --fields` - the backend's vendor surface: bindings, the vendor-only
inventory, and the operator commands `scqo -h` cannot show.

In-process against the pure line builders and a real simulated Session - the one
subprocess end-to-end pass lives in test_cli_run.py (each spawn costs seconds;
the formatting logic does not need one). This file is also the FIRST coverage
this view has ever had: until the operator-command inventory landed beside them,
``_fields_payload`` called three backend hooks by direct attribute access, and
``SimulatedBackend`` is not a ``Backend`` subclass - so the view raised
``AttributeError`` on every ``backend = "simulated"`` setup.
"""

from __future__ import annotations

import json
import types
from dataclasses import FrozenInstanceError
from dataclasses import fields as dataclass_fields

import pytest

from scqo.cli import state as state_cli
from scqo.fieldmap import OperatorCommand, VendorOnly
from scqo.session import Session
from scqo.testing import SimulatedBackend, demo_device

#: _print_context reads only the device name off the lab config.
CFG = types.SimpleNamespace(device=None)


def _session(backend_cls=SimulatedBackend):
    roster, _design, device = demo_device()
    return Session(backend_cls(device), roster)


class _Rich(SimulatedBackend):
    """A driver declaring both halves of the vendor surface, both tiers."""

    def vendor_only(self):
        return {
            "readout_band": VendorOnly(
                path="q.resonator.opx_output.band", unit="", kind="vendor",
                doc="which MW-FEM band the readout output port runs in",
                coupled=("readout_upconverter_frequency",),
                edit="state.json ports.mw_outputs.<con>.<fem>.<port>.band, offline",
                counterpart="hardware_options.modulation_frequencies lo_freq"),
            "per_gate_detuning": VendorOnly(
                path="q.xy.x180.detuning", unit="Hz", kind="unique",
                doc="no qblox counterpart - experiments touching it run only here",
                edit="edit state.json directly"),
        }

    def operator_commands(self):
        return (
            OperatorCommand(
                name="close_qm",
                command="python -m scqo_qm.backend.close_qm",
                doc="Release the cluster hardware locks.",
                options="--qm-id ID  --dry-run",
                caution="DESTRUCTIVE and there is no confirmation prompt."),
            OperatorCommand(
                name="plain", command="python -m whatever",
                doc="No options, no caution."),
        )


class _Broken(SimulatedBackend):
    def operator_commands(self):
        raise RuntimeError("driver exploded")


def _payload(**over):
    """A synthetic payload: the golden-line tests must not depend on a session."""
    base = {"backend": "demo", "vendor_only": [], "operator_commands": [],
            "operator_commands_error": None}
    base.update(over)
    return base


# ------------------------------------------------------------ the whole view

def test_fields_renders_on_the_simulated_backend(capsys):
    """THE regression. Direct attribute access on the three catalog hooks made
    this raise AttributeError for every simulated setup."""
    assert state_cli._print_fields(_session(), CFG, as_json=False) == 0
    out = capsys.readouterr().out
    # written long ago, unreachable until now: the crash was three lines earlier
    assert "declares no field bindings" in out
    assert "placement rule: scqo state --rule" in out


def test_fields_json_is_pure_json(capsys):
    assert state_cli._print_fields(_session(_Rich), CFG, as_json=True) == 0
    payload = json.loads(capsys.readouterr().out)  # no comment line may precede
    assert payload["operator_commands_error"] is None
    assert [c["name"] for c in payload["operator_commands"]] == ["close_qm", "plain"]
    assert [v["name"] for v in payload["vendor_only"]] == [
        "readout_band", "per_gate_detuning"]


def test_new_vendor_only_attributes_reach_json_unedited(capsys):
    """``_fields_payload`` builds vendor_only rows with ``asdict()``, so a new
    ``VendorOnly`` attribute reaches ``--json`` with no edit there. Refactoring
    that comprehension to an explicit field list would silently drop the next
    one - this test is why it stays."""
    state_cli._print_fields(_session(_Rich), CFG, as_json=True)
    row = json.loads(capsys.readouterr().out)["vendor_only"][0]
    assert row["coupled"] == ["readout_upconverter_frequency"]
    assert row["edit"].startswith("state.json ports.mw_outputs")
    assert row["counterpart"] == "hardware_options.modulation_frequencies lo_freq"


# ------------------------------------------------- the vendor-only sub-lines

def test_vendor_only_sub_lines_render_only_when_set():
    full = {"name": "readout_band", "unit": "", "kind": "vendor",
            "path": "q.z.band", "doc": "the band",
            "coupled": ["readout_upconverter_frequency"],
            "edit": "offline, no live session", "counterpart": "lo_freq"}
    bare = {**full, "name": "bare", "coupled": [], "edit": "", "counterpart": ""}
    lines = state_cli._vendor_only_lines(_payload(vendor_only=[full, bare]))
    body = " | ".join(lines)
    assert "coupled: readout_upconverter_frequency" in body
    assert "edit: offline, no live session" in body
    assert "counterpart: lo_freq" in body
    # the bare row contributes its two lines and NO sub-lines
    assert sum(1 for x in lines if "coupled:" in x) == 1
    # dataclass declaration order, two columns in from the doc continuation (38)
    detail = [x for x in lines if x.startswith(" " * 40)]
    assert [x.split(":")[0].strip() for x in detail] == [
        "coupled", "edit", "counterpart"]


def test_vendor_only_sub_lines_render_for_the_unique_tier_too():
    """Both loops share one builder, so a unique entry is not a second format."""
    row = {"name": "per_gate_detuning", "unit": "Hz", "kind": "unique",
           "path": "q.xy.detuning", "doc": "no qblox counterpart",
           "coupled": [], "edit": "edit state.json directly", "counterpart": ""}
    lines = state_cli._vendor_only_lines(_payload(vendor_only=[row]))
    body = " | ".join(lines)
    assert "instrument-UNIQUE parameters" in body
    assert "edit: edit state.json directly" in body


# ---------------------------------------------------- the operator inventory

def test_operator_command_lines():
    cmd = {"name": "close_qm", "command": "python -m scqo_qm.backend.close_qm",
           "doc": "Release the locks.", "options": "--dry-run",
           "caution": "DESTRUCTIVE."}
    lines = state_cli._operator_command_lines(_payload(operator_commands=[cmd]))
    body = " | ".join(lines)
    assert "operator commands" in body and "NOT scqo subcommands" in body
    assert "python -m scqo_qm.backend.close_qm" in body
    assert "options: --dry-run" in body
    assert "CAUTION: DESTRUCTIVE." in body


def test_empty_operator_inventory_prints_no_header():
    """An empty section on the simulated backend is noise; the doctor pointer is
    what tells that operator the section exists at all."""
    assert state_cli._operator_command_lines(_payload()) == []


def test_operator_commands_hook_degrades_without_failing_the_view(capsys):
    """A broken driver must not cost the operator the bindings table they came
    for - and the failure must be a payload KEY, never a comment line on stdout,
    which would break --json."""
    sess = _session(_Broken)
    payload = state_cli._fields_payload(sess, CFG)
    assert payload["operator_commands"] == []
    assert payload["operator_commands_error"] == "RuntimeError: driver exploded"
    assert payload["kinds"], "the catalog view still renders"
    warn = state_cli._operator_command_lines(payload)
    assert any(x.startswith("# WARN:") and "driver exploded" in x for x in warn)
    assert state_cli._print_fields(sess, CFG, as_json=True) == 0
    json.loads(capsys.readouterr().out)


def test_backend_without_the_hook_reports_nothing_at_all():
    """The un-upgraded-driver case: an absent hook is not an error."""
    payload = state_cli._fields_payload(_session(), CFG)
    assert payload["operator_commands"] == []
    assert payload["operator_commands_error"] is None
    assert state_cli._operator_command_lines(payload) == []


# --------------------------------------------------------------- the guards

@pytest.mark.parametrize("argv", [
    ["--fields", "--physical"],
    ["--fields", "--sources"],
    ["--fields", "--entity", "q0"],
    ["--fields", "--history"],
    ["--json"],
])
def test_fields_argparse_guards_refuse_before_any_session(argv):
    """These refusals run before build_session, so they need no lab config."""
    with pytest.raises(SystemExit) as err:
        state_cli.main(argv)
    assert err.value.code == 2


def test_dataclass_shapes():
    """Frozen, and every attribute of the operational half carries a default
    declared AFTER ``kind`` - existing callers pass path/unit/doc positionally."""
    order = [f.name for f in dataclass_fields(VendorOnly)]
    assert order == ["path", "unit", "doc", "kind", "coupled", "edit", "counterpart"]
    v = VendorOnly("p", "u", "d")
    assert (v.kind, v.coupled, v.edit, v.counterpart) == ("vendor", (), "", "")
    c = OperatorCommand("n", "c", "d")
    assert (c.options, c.caution) == ("", "")
    with pytest.raises(FrozenInstanceError):
        v.path = "x"
    with pytest.raises(FrozenInstanceError):
        c.name = "x"
