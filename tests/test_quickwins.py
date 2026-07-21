"""Tests for the quick-win fixes and entry-point discovery.

Covers:
  * A1 — simulator seeds are reproducible across processes (not PYTHONHASHSEED-dependent).
  * A2 — Session.run returns a structured failure (never raises) on a contract violation.
  * A3 — a partial run still writes back the qubits that succeeded.
  * B  — registry discovers experiments advertised via the ``scqo.experiments`` entry point.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap

import numpy as np

import scqo.registry as registry
from scqo import Outcome, Session, register
from scqo.experiments import ResonatorSpectroscopy
from scqo.testing import InMemoryDevice, SimulatedBackend, demo_roster


def _device() -> InMemoryDevice:
    return InMemoryDevice(
        {
            "q0": {"readout_freq": 5.95e9, "drive_freq": 3.87e9, "pi_amp": 0.2, "readout_amp": 0.25},
            "q1": {"readout_freq": 6.05e9, "drive_freq": 4.01e9, "pi_amp": 0.18, "readout_amp": 0.22},
        }
    )


# Canonical-name demo registration so this module passes standalone (same idiom as
# test_parameter_defaults); re-registering the same shape elsewhere is harmless.
@register
class _QwResonatorSpectroscopy(ResonatorSpectroscopy):
    def probe(self):
        return None


# --------------------------------------------------------------------------- A1
def test_simulator_seed_is_process_independent():
    """The simulator's hidden truth must not depend on PYTHONHASHSEED.

    Runs the same ``simulate()`` in three subprocesses with different hash seeds and
    asserts identical output — the regression that ``abs(hash(...))`` introduced.
    """
    code = textwrap.dedent(
        """
        from scqo.testing import InMemoryDevice, SimulatedBackend
        from scqo.experiments import ResonatorSpectroscopy

        class Demo(ResonatorSpectroscopy):
            def probe(self):
                return None

        dev = InMemoryDevice({"q0": {"readout_freq": 6e9, "drive_freq": 4e9, "pi_amp": 0.2, "readout_amp": 0.25}})
        exp = Demo(SimulatedBackend(dev), Demo.Parameters(targets=["q0", "q1"]))
        out = exp.simulate(exp.define_sweep())
        print(repr(float(out["I"].sum())))
        """
    )
    outputs = set()
    for seed in ("1", "2", "3"):
        env = {**os.environ, "PYTHONHASHSEED": seed}
        proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env)
        assert proc.returncode == 0, proc.stderr
        outputs.add(proc.stdout.strip())
    assert len(outputs) == 1, f"simulator not reproducible across PYTHONHASHSEED: {outputs}"


# --------------------------------------------------------------------------- A2
@register
class _BrokenExperiment(ResonatorSpectroscopy):
    """Concrete experiment whose simulate() omits a required variable -> contract miss."""

    name = "broken_contract"

    def probe(self):
        return None

    def simulate(self, coords):
        detuning = coords["detuning_hz"]
        # Missing "Q": the contract requires variables=("I", "Q").
        return {"I": np.zeros((len(self.params.targets), detuning.size))}


def test_session_returns_structured_failure_on_contract_violation():
    sess = Session(SimulatedBackend(_device()), demo_roster())
    before = sess.device_state()["q0"]["readout_freq"]

    result = sess.run("broken_contract", {"targets": ["q0"]})  # must NOT raise

    assert result["error"], "failed run should carry a non-empty error message"
    assert result["outcomes"]["q0"] == Outcome.NO_DATA.value
    # nothing was written back on failure
    assert sess.device_state()["q0"]["readout_freq"] == before


# --------------------------------------------------------------------------- A3
@register
class _PartialExperiment(ResonatorSpectroscopy):
    """q0 succeeds, q1 fails — to check per-qubit writeback on a partial run."""

    name = "partial_success"

    def probe(self):
        return None

    def estimate(self):
        result = self.Result()
        result.fit["q0"] = {
            "readout_freq": 7.0e9,
            "dip_detuning_hz": 0.0,
            "old_readout_freq": float(self.device.component("q0").readout_freq),
        }
        result.outcomes["q0"] = Outcome.SUCCESSFUL
        result.outcomes["q1"] = Outcome.FAILED
        return result


def test_partial_success_writes_only_good_qubits():
    sess = Session(SimulatedBackend(_device()), demo_roster())
    before_q1 = sess.device_state()["q1"]["readout_freq"]

    result = sess.run("partial_success", {"targets": ["q0", "q1"]}, update="apply")

    assert result["outcomes"]["q0"] == Outcome.SUCCESSFUL.value
    assert result["outcomes"]["q1"] == Outcome.FAILED.value
    state = sess.device_state()
    assert np.isclose(state["q0"]["readout_freq"], 7.0e9)  # good qubit written
    assert np.isclose(state["q1"]["readout_freq"], before_q1)  # failed qubit untouched


# ---------------------------------------------------------------------------- B
def test_discovery_imports_entry_points(monkeypatch):
    """catalog()/get() pull in experiments advertised under the entry-point group."""
    registry._discovered = False

    class _FakeEP:
        name = "fake"

        def load(self):
            from scqo.experiments import QubitRamsey

            @register
            class _DiscoveredRamsey(QubitRamsey):
                name = "discovered_ramsey"

                def probe(self):
                    return None

    monkeypatch.setattr(
        registry,
        "entry_points",
        lambda group=None: [_FakeEP()] if group == "scqo.experiments" else [],
    )

    names = {entry["name"] for entry in registry.catalog()}
    assert "discovered_ramsey" in names


def test_contrib_entry_point_group_is_tagged(monkeypatch):
    """Experiments loaded from the contrib group are tagged maturity='contrib' in the
    catalog; core-group and hand-registered ones stay 'core'."""
    registry._discovered = False

    class _CoreEP:
        name = "core"

        def load(self):
            @register
            class _CoreExp(ResonatorSpectroscopy):
                name = "maturity_core_exp"

                def probe(self):
                    return None

    class _ContribEP:
        name = "contrib"

        def load(self):
            @register
            class _ContribExp(ResonatorSpectroscopy):
                name = "maturity_contrib_exp"

                def probe(self):
                    return None

    def fake_entry_points(group=None):
        return {
            "scqo.experiments": [_CoreEP()],
            "scqo.experiments.contrib": [_ContribEP()],
        }.get(group, [])

    monkeypatch.setattr(registry, "entry_points", fake_entry_points)
    maturity = {e["name"]: e["maturity"] for e in registry.catalog()}
    assert maturity["maturity_core_exp"] == "core"
    assert maturity["maturity_contrib_exp"] == "contrib"
    assert maturity["resonator_spectroscopy"] == "core"  # hand-registered demo classes


def test_discovery_skips_failing_entry_point(monkeypatch):
    """A backend that fails to import is skipped, not fatal to discovery."""
    registry._discovered = False

    class _BadEP:
        name = "bad"

        def load(self):
            raise ImportError("vendor library not installed")

    monkeypatch.setattr(registry, "entry_points", lambda group=None: [_BadEP()])

    registry.catalog()  # must not raise
