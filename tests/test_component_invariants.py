"""Invariants the component-model cutover must NOT break (insurance, written first).

(a) ``result.fit`` is keyed by RUN TARGET names — viewer trends read the index's
    fit JSON at ``$.{target}.{quantity}`` (datastore.fit_trend), so re-homing
    store fields to satellite components (q1_res, q1_ro) must never re-key the
    fit dicts. Cross-component update() writes go to satellite VIEWS; the fit
    stays on the target.
(b) OLD run records (written before the targets rename, carrying ``"qubits"``)
    must stay findable after reindex — run folders are truth and are never
    fresh-started.
"""

from __future__ import annotations

import json

from scqo import Session
from scqo.testing import InMemoryDevice, SimulatedBackend, demo_roster

# The Demo* concrete experiments register on import; _flux_roster gives every
# experiment (flux ones included) a roster that passes the pre-probe gate.
from tests.test_end_to_end import _device, _flux_roster  # noqa: F401  (registration side effect)

#: One minimal-params run per experiment (mirrors tests/test_end_to_end.py).
EXPERIMENT_PARAMS: dict[str, dict] = {
    "resonator_spectroscopy": {"frequency_span_hz": 15e6, "num_points": 201},
    "qubit_ramsey": {},
    "qubit_power_rabi": {"max_amp_factor": 2.0, "num_points": 201},
    "qubit_spectroscopy": {"frequency_span_hz": 60e6},
    "qubit_relaxation": {},
    "qubit_echo": {},
    "qubit_spectroscopy_flux": {},
    "resonator_spectroscopy_flux": {},
    "single_shot_readout": {"num_shots": 1500},
    "readout_power": {},
    "readout_frequency": {},
    "resonator_spectroscopy_power_amp": {},
    "resonator_spectroscopy_power_chain": {},
}


def test_fit_dicts_are_keyed_by_run_targets():
    """For EVERY experiment: fit keys are a subset of the run's target names.

    This is the trend-continuity contract: fit_trend() looks quantities up by
    the target name, so a fit keyed by a satellite (q1_res) would silently
    vanish from every trend. The cutover moves STORE homes, never fit keys.
    """
    targets = ["q0", "q1"]
    exercised = 0
    for name, extra in EXPERIMENT_PARAMS.items():
        sess = Session(SimulatedBackend(_device()), _flux_roster())
        payload = sess.run(name, {"targets": targets, **extra})
        fit = payload.get("fit") or {}
        assert set(fit) <= set(targets), (
            f"{name}: fit keyed by non-target names {sorted(set(fit) - set(targets))}"
        )
        if fit:
            exercised += 1
    assert exercised >= 10  # the invariant must actually be exercised, not vacuous


def test_old_format_run_records_stay_findable(tmp_path):
    """A record.json in the PRE-rename format (``"qubits"`` key) must reindex
    into a searchable row — the one data exemption of the no-compat policy."""
    sess = Session(SimulatedBackend(_device()), demo_roster(),
                   data_root=tmp_path, device_name="chipT")
    payload = sess.run("resonator_spectroscopy",
                       {"targets": ["q0"], "frequency_span_hz": 15e6})
    run_id = payload["run_id"]

    # Force the record into the OLD shape: whatever key the current code wrote,
    # the on-disk legacy fixture says "qubits" — this manufactures the legacy
    # format the reindex must accept.
    from pathlib import Path

    record_file = Path(payload["data_path"]) / "record.json"
    record = json.loads(record_file.read_text(encoding="utf-8"))
    names = record.pop("targets", None) or record.pop("qubits", None) or ["q0"]
    record["qubits"] = names
    record.pop("targets", None)
    record_file.write_text(json.dumps(record, indent=2), encoding="utf-8")

    sess.datastore.reindex()
    # The FIXTURE above (old-format record.json) is the part that must keep
    # passing unchanged; the query kwarg is the cutover's target=.
    found = sess.find_runs(experiment="resonator_spectroscopy", target="q0")
    assert any(r["run_id"] == run_id for r in found)
