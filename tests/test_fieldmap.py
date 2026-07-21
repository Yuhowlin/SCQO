"""The cross-backend field catalog: Backend defaults, FieldSpec portability
metadata, the per-category --fields payload, and the `scqo state --fields` CLI view.

The catalog is declared per DRIVER (each driver's fieldmap module, drift-tested
in that repo's suite) and keyed per CATEGORY since the component cutover; core
owns the shapes (scqo.fieldmap), the Backend defaults, and the rendering —
which is what this file covers.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from scqo.categories import CATEGORIES, pushed_fields
from scqo.fieldmap import VendorBinding, VendorOnly
from scqo.session import Session
from scqo.testing import InMemoryDevice, SimulatedBackend, demo_roster

QUBITS = {"q0": {"readout_freq": 5.9e9, "drive_freq": 4.8e9, "pi_amp": 0.2,
                 "readout_amp": 0.1, "readout_power_dbm": -30.0}}
NO_DEVICE_CFG = SimpleNamespace(device=None)

#: The instrument category the stub backend declares bindings for.
PUSHED = pushed_fields("ReadableTransmon")
#: Every neutral field name across the whole category catalog.
ALL_FIELDS = {f for spec in CATEGORIES.values() for f in spec.fields}


class _CatalogBackend(SimulatedBackend):
    """Simulated backend declaring bindings for every pushed ReadableTransmon
    field but the last (so the missing-binding report has something to name)."""

    BINDINGS = {
        field: VendorBinding(path=f"stub.{field}", unit="Hz")
        for field in PUSHED[:-1]
    }
    BINDINGS[PUSHED[0]] = VendorBinding(
        path=f"stub.{PUSHED[0]}", unit="GHz", convert="value / 1e9",
        coupled=("readout_amp",), note="1 kHz grid",
    )

    def field_bindings(self) -> dict[str, dict[str, VendorBinding]]:
        return {"ReadableTransmon": dict(self.BINDINGS)}

    def vendor_only(self) -> dict[str, VendorOnly]:
        return {
            "stub_knob": VendorOnly(path="stub.only", unit="ns",
                                    doc="a stub-only vendor knob"),
            "stub_att": VendorOnly(path="stub.att", unit="dB", kind="realizer",
                                   doc="realizes readout_power_dbm - use scqo set"),
            "stub_special": VendorOnly(path="stub.special", unit="", kind="unique",
                                       doc="no counterpart elsewhere"),
        }


def _category(payload: dict, name: str) -> dict:
    (entry,) = [c for c in payload["categories"] if c["category"] == name]
    return entry


def test_backend_catalog_defaults_are_empty():
    """A backend without a catalog (simulated, pre-catalog drivers) declares
    nothing — all three surfaces default to {} and nothing downstream breaks."""
    backend = SimulatedBackend(InMemoryDevice(QUBITS))
    assert backend.field_bindings() == {}
    assert backend.unrealized() == {}
    assert backend.vendor_only() == {}


def test_fieldspec_portable_metadata():
    """The dimensionless per-chain amplitudes and the DAC-plane flux volts are
    marked non-portable; every other field (absolute physical quantities, sample
    physics) stays portable."""
    non_portable = {
        name
        for spec in CATEGORIES.values() if spec.side == "instrument"
        for name, fs in spec.fields.items() if not fs.portable
    }
    assert non_portable == {"pi_amp", "readout_amp", "idle_flux_v",
                            "coupler_decouple_v", "coupler_interaction_v"}
    for spec in CATEGORIES.values():
        if spec.side == "physical":
            assert all(fs.portable for fs in spec.fields.values())


def test_fields_payload_without_catalog():
    """No declared bindings: every neutral field renders unbound, and the
    missing-binding report stays EMPTY (an empty declaration is a backend
    without a catalog, not a drifted one)."""
    from scqo.cli.state import _fields_payload

    sess = Session(SimulatedBackend(InMemoryDevice(QUBITS)), demo_roster())
    payload = _fields_payload(sess, NO_DEVICE_CFG)
    assert [c["category"] for c in payload["categories"]] == list(CATEGORIES)
    for cat in payload["categories"]:
        assert [f["name"] for f in cat["fields"]] == list(CATEGORIES[cat["category"]].fields)
        assert all(f["binding"] is None and f["unrealized"] is None for f in cat["fields"])
    assert payload["missing_bindings"] == []
    assert payload["vendor_only"] == []


def test_fields_payload_with_catalog():
    """Declared bindings surface verbatim under their category; kinds route
    correctly; a pushed field the backend neither binds nor declares Unrealized
    is named in missing_bindings as Category.field."""
    from scqo.cli.state import _fields_payload

    sess = Session(_CatalogBackend(InMemoryDevice(QUBITS)), demo_roster())
    payload = _fields_payload(sess, NO_DEVICE_CFG)
    readable = _category(payload, "ReadableTransmon")
    by_name = {f["name"]: f for f in readable["fields"]}

    first = by_name[PUSHED[0]]["binding"]
    assert first["path"] == f"stub.{PUSHED[0]}"
    assert first["convert"] == "value / 1e9"
    assert tuple(first["coupled"]) == ("readout_amp",)
    assert first["note"] == "1 kHz grid"

    assert by_name["readout_freq"]["kind"] == "pushed"
    assert by_name["readout_fidelity"]["kind"] == "record-only"
    assert by_name["readout_fidelity"]["binding"] is None  # no vendor knob
    transmon = {f["name"]: f for f in _category(payload, "FixedTransmon")["fields"]}
    assert transmon["t1_s"]["kind"] == "physical"

    assert payload["missing_bindings"] == [f"ReadableTransmon.{PUSHED[-1]}"]
    assert payload["vendor_only"] == [
        {"name": "stub_knob", "path": "stub.only", "unit": "ns",
         "doc": "a stub-only vendor knob", "kind": "vendor"},
        {"name": "stub_att", "path": "stub.att", "unit": "dB",
         "doc": "realizes readout_power_dbm - use scqo set", "kind": "realizer"},
        {"name": "stub_special", "path": "stub.special", "unit": "",
         "doc": "no counterpart elsewhere", "kind": "unique"},
    ]


def test_cli_state_fields_table(capsys):
    """`scqo state --fields` on the built-in demo device: one section per
    category with its full field list, non-portable fields flagged, and the
    no-catalog info line (not a WARN)."""
    from scqo.cli import state as state_cli

    assert state_cli.main(["--fields"]) == 0
    out = capsys.readouterr().out
    for cat, spec in CATEGORIES.items():
        assert cat in out
        for name in spec.fields:
            assert name in out
    assert "NO" in out  # pi_amp / readout_amp / idle_flux_v portability flag
    assert "declares no field bindings" in out
    assert "WARN" not in out


def test_cli_state_fields_json(capsys):
    """--json emits pure JSON on stdout (| jq safe: no context header lines)."""
    from scqo.cli import state as state_cli

    assert state_cli.main(["--fields", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["backend"] == "simulated"
    assert {f["name"] for c in payload["categories"] for f in c["fields"]} == ALL_FIELDS
    assert payload["missing_bindings"] == []


def test_vendor_only_kind_metadata():
    """kind defaults to "vendor" and the valid values are the rule's four tiers."""
    from scqo.fieldmap import VENDOR_ONLY_KINDS

    assert VENDOR_ONLY_KINDS == ("realizer", "candidate", "vendor", "unique")
    assert VendorOnly(path="p", unit="", doc="d").kind == "vendor"


def test_cli_state_rule_prints_checklist(capsys):
    """`scqo state --rule` is static text: no config, no driver, no session."""
    from scqo.cli import state as state_cli

    assert state_cli.main(["--rule"]) == 0
    out = capsys.readouterr().out
    for token in ("placement rule", "physical.json", "scqo_state.json",
                  "run record", "[realizer]", "[unique]", "chip in the dark",
                  "components.toml"):
        assert token in out
    with pytest.raises(SystemExit) as err:  # combines with nothing
        state_cli.main(["--rule", "--fields"])
    assert err.value.code == 2
    capsys.readouterr()


def test_print_fields_kind_tags_and_unique_section(capsys):
    """--fields tags shared vendor rows with [kind] and isolates unique entries
    under the lock-in header."""
    from scqo.cli.state import _print_fields

    sess = Session(_CatalogBackend(InMemoryDevice(QUBITS)), demo_roster())
    _print_fields(sess, NO_DEVICE_CFG, as_json=False)
    out = capsys.readouterr().out
    assert "[vendor] stub.only" in out
    assert "[realizer] stub.att" in out
    assert "instrument-UNIQUE" in out and "stub.special" in out
    assert "placement rule: scqo state --rule" in out


def test_cli_state_fields_flag_guards(capsys):
    """--fields is a schema view: value/history flags don't combine with it, and
    --json belongs to --fields."""
    from scqo.cli import state as state_cli

    for argv in (["--fields", "--sources"], ["--fields", "--history"],
                 ["--fields", "--component", "q0"], ["--json"]):
        with pytest.raises(SystemExit) as err:
            state_cli.main(argv)
        assert err.value.code == 2  # argparse usage error
    capsys.readouterr()  # swallow the usage text
