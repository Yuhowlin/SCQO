"""Live-source provenance — which run does each CURRENT value trace to?

The values in use matter more than the pending ones. For every (qubit, field)
with a current value, the LAST ChangeRecord for that pair is the candidate
source; under the STRICT-MATCH rule it is credited only while its recorded value
still equals the live value. A drifted value (the vendor reseeded at startup,
qualibrate wrote QUAM directly, someone hand-edited a config) is reported as
"external" — a run is NEVER credited for a value the device no longer runs.

Pure functions over plain dicts, no I/O: the viewer feeds them a context's state
files (``scqo_state.json`` config+history / ``physical.json`` values+history in the
same ``<cooldown>/<setup>/scqo/`` folder — both per (cooldown, setup), so the whole
file belongs to one context and no slicing/filtering is needed), the Session feeds
them its live state (:meth:`scqo.session.Session.live_sources`), and both get the
same answer for the same facts.
"""

from __future__ import annotations

from typing import Any

#: source-info ``status`` values (see :func:`live_sources`).
SOURCE_STATUSES = ("run", "manual", "external", "unrecorded")


def live_sources(values: dict, history: list[dict]) -> dict[str, dict[str, dict]]:
    """``{qubit: {field: source-info}}`` for every (qubit, field) with a value.

    ``values`` is the current store snapshot (``config``/``values``/
    ``device_state()``/``physical_state()``); ``history`` is the ChangeRecord
    dicts in file order (append-only == chronological). Source-info::

        {"qubit", "field", "value",             # the CURRENT value
         "status":  "run"         # last record has a run_id AND still matches
                  | "manual"      # last record was a manual write (no run) and matches
                  | "external"    # a record exists but the value drifted -> NO run credited
                  | "unrecorded", # value present, no record at all (vendor pull-seed)
         "run_id",                              # only for status == "run"
         "timestamp", "operator", "experiment", # from the last record (None if unrecorded)
         "recorded"}                            # the last record's value (differs iff external)
    """
    last: dict[tuple[str, str], dict] = {}
    for record in history:  # forward pass: dict overwrite == last record wins
        last[(record["qubit"], record["field"])] = record

    out: dict[str, dict[str, dict]] = {}
    for qubit, fields in values.items():
        for field, value in fields.items():
            if value is None:
                continue
            record = last.get((qubit, field))
            info: dict[str, Any] = {
                "qubit": qubit, "field": field, "value": value,
                "run_id": None, "timestamp": None, "operator": None,
                "experiment": None, "recorded": None,
            }
            if record is None:
                info["status"] = "unrecorded"
            else:
                info["timestamp"] = record.get("timestamp")
                info["operator"] = record.get("operator")
                info["experiment"] = record.get("experiment")
                info["recorded"] = record.get("new")
                if record.get("new") != value:
                    info["status"] = "external"  # strict match: never credit a drifted value
                elif record.get("run_id"):
                    info["status"] = "run"
                    info["run_id"] = record["run_id"]
                else:
                    info["status"] = "manual"
            out.setdefault(qubit, {})[field] = info
    return out


def live_run_map(*sources: dict) -> dict[str, list[tuple[str, str]]]:
    """Merge :func:`live_sources` results -> ``{run_id: [(qubit, field), ...]}``.

    Only ``status == "run"`` entries contribute; pass the instrument and physical
    maps together to get the device's full "built from these runs" picture.
    """
    out: dict[str, list[tuple[str, str]]] = {}
    for source in sources:
        for qubit, fields in source.items():
            for field, info in fields.items():
                if info["status"] == "run":
                    out.setdefault(info["run_id"], []).append((qubit, field))
    return out


def summarize_live(pairs: list[tuple[str, str]]) -> str:
    """``[(q0, readout_freq), (q1, readout_freq), (q1, t1_s)]`` ->
    ``"readout_freq (q0,q1), t1_s (q1)"`` (field first-appearance order, qubits sorted)."""
    by_field: dict[str, list[str]] = {}
    for qubit, field in pairs:
        by_field.setdefault(field, []).append(qubit)
    return ", ".join(f"{field} ({','.join(sorted(qubits))})" for field, qubits in by_field.items())
