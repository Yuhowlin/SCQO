"""Standing per-experiment parameter defaults — the 3-tier cascade at the Session.

Precedence under test (lowest -> highest): pydantic Field defaults < the session's
``parameter_defaults`` (wired from ~/.scqo/parameters.toml by make_session) < the
caller's params dict. Also covers the structured validation failure (a bad key must
never raise across the JSON boundary — and must name the defaults file when it came
from there) and the effective-defaults overlay in ``Session.catalog()``.
"""

from __future__ import annotations

from scqo import Session, register, registry
from scqo.experiments import ResonatorSpectroscopy
from scqo.testing import InMemoryDevice, SimulatedBackend, demo_roster


# Concrete demo experiment (probe is a no-op under SimulatedBackend); registering under
# the canonical name is idempotent across test modules.
@register
class _PdResonatorSpectroscopy(ResonatorSpectroscopy):
    def probe(self):
        return None


def _device() -> InMemoryDevice:
    return InMemoryDevice(
        {
            "q0": {"readout_freq": 5.95e9, "drive_freq": 3.87e9, "pi_amp": 0.2, "readout_amp": 0.25},
            "q1": {"readout_freq": 6.05e9, "drive_freq": 4.01e9, "pi_amp": 0.18, "readout_amp": 0.22},
        }
    )


def _session(tmp_path=None, **kwargs) -> Session:
    if tmp_path is not None:
        kwargs.setdefault("data_root", tmp_path / "data")
        kwargs.setdefault("device_name", "devA")
    return Session(SimulatedBackend(_device()), demo_roster(), **kwargs)


# ---------------------------------------------------------------- merge precedence


def test_run_merges_file_defaults(tmp_path):
    sess = _session(tmp_path, parameter_defaults={"resonator_spectroscopy": {"num_points": 51}})
    result = sess.run("resonator_spectroscopy", {"targets": ["q0"]})
    assert result["outcomes"]["q0"] == "successful"
    # the persisted parameters are the fully-resolved values actually used
    assert sess.load_run(result["run_id"])["parameters"]["num_points"] == 51


def test_caller_params_beat_file_defaults(tmp_path):
    sess = _session(tmp_path, parameter_defaults={"resonator_spectroscopy": {"num_points": 51}})
    result = sess.run("resonator_spectroscopy", {"targets": ["q0"], "num_points": 75})
    assert sess.load_run(result["run_id"])["parameters"]["num_points"] == 75


def test_code_defaults_when_no_table(tmp_path):
    sess = _session(tmp_path, parameter_defaults={"qubit_ramsey": {"num_points": 51}})
    result = sess.run("resonator_spectroscopy", {"targets": ["q0"]})
    assert sess.load_run(result["run_id"])["parameters"]["num_points"] == 101  # pydantic default


def test_file_defaults_can_supply_required_targets():
    """Even a required knob (targets has no code default) may get a standing default."""
    sess = _session(parameter_defaults={"resonator_spectroscopy": {"targets": ["q1"]}})
    result = sess.run("resonator_spectroscopy", {})
    assert result["outcomes"] == {"q1": "successful"}


# ---------------------------------------------------------------- validation failures


def test_validation_error_returns_structured_failure():
    """Regression: a typo'd key used to raise a raw pydantic ValidationError across the
    'Session never raises' JSON boundary. It must come back as a structured failure."""
    sess = _session()
    result = sess.run("resonator_spectroscopy", {"targets": ["q0"], "frequncy_span_hz": 1e6})
    assert result["outcomes"] == {"q0": "failed"}
    assert "frequncy_span_hz" in result["error"]


def test_validation_error_names_defaults_file():
    """A bad key that came from the defaults file (not the caller) names the file."""
    sess = _session(
        parameter_defaults={"resonator_spectroscopy": {"frequncy_span_hz": 1e6}},
        parameter_defaults_source="X:/parameters.toml",
    )
    result = sess.run("resonator_spectroscopy", {"targets": ["q0"]})
    assert result["outcomes"] == {"q0": "failed"}
    assert "X:/parameters.toml" in result["error"]
    assert "frequncy_span_hz" in result["error"]


def test_validation_failure_is_not_persisted(tmp_path):
    """Nothing touched the instrument and there is no dataset — a typo must not
    allocate a run folder or an index row."""
    sess = _session(tmp_path)
    result = sess.run("resonator_spectroscopy", {"targets": ["q0"], "nope": 1})
    assert result["error"]
    assert "run_id" not in result and "data_path" not in result
    assert sess.find_runs() == []


# ---------------------------------------------------------------- catalog overlay


def test_catalog_overlays_effective_defaults():
    sess = _session(
        parameter_defaults={"resonator_spectroscopy": {"num_points": 51, "targets": ["q1"], "bogus_key": 1}},
        parameter_defaults_source="X:/parameters.toml",
    )
    entry = next(e for e in sess.catalog() if e["name"] == "resonator_spectroscopy")
    props = entry["parameters_schema"]["properties"]
    assert props["num_points"]["default"] == 51
    assert props["num_points"]["x-default-source"] == "X:/parameters.toml"
    # a file-supplied required key is no longer required for THIS session's callers
    assert "targets" not in entry["parameters_schema"].get("required", [])
    assert props["targets"]["default"] == ["q1"]
    # unknown keys are skipped by the overlay (they fail at run() instead)
    assert "bogus_key" not in props

    # the raw registry and a defaults-free Session stay pristine (no cross-pollution)
    raw = next(e for e in registry.catalog() if e["name"] == "resonator_spectroscopy")
    assert raw["parameters_schema"]["properties"]["num_points"]["default"] == 101
    assert "x-default-source" not in raw["parameters_schema"]["properties"]["num_points"]
    assert "targets" in raw["parameters_schema"]["required"]
    plain = next(e for e in _session().catalog() if e["name"] == "resonator_spectroscopy")
    assert plain["parameters_schema"]["properties"]["num_points"]["default"] == 101
