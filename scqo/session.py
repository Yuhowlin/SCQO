"""Session — the single entry point used identically by humans and AI agents.

Everything crosses this boundary as plain JSON-able Python (dicts / lists), so the
same calls drive a manual notebook *and* an LLM tool-use loop:

    sess, cfg = build_session()        # the lab-config way (scqo.cli.build_session):
                                       #   resolves device -> setup -> per-SETUP state file
    sess.catalog()                     # what can I measure? (with parameter schemas)
    sess.run("qubit_ramsey", {...})    # measure -> structured result (+ run_id + suggestions)
    sess.accept(run_id)                # apply the run's suggested updates (or a subset)
    sess.reject(run_id, comment="...") # decline them (metadata only)
    sess.find_runs(experiment="qubit_ramsey", qubit="q0")   # find my data (pending=True too)
    sess.load_run(run_id)              # reload a saved run (record/params/result/figures)
    sess.device_state()                # current calibration (this setup's SCQO config)
    sess.physical_state()              # the sample's measured physics (this setup's slice)
    sess.history()                     # every recorded change (the loop's memory)

No vendor/hardware object is ever exposed here. The Session owns the **authoritative
SCQO config + change history** (a :class:`~scqo.config.RecordingDevice` wrapping the
backend's vendor device) and, when ``data_root`` is given, a
:class:`~scqo.datastore.DataStore` that persists **every** run — dataset, parameters,
result, device snapshots, and the scqat analysis artifacts (figures included).
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any, Literal

from pydantic import ValidationError

from . import config as _config
from . import registry
from .backend import Backend
from .config import RecordingDevice, _now
from .contract import ContractError
from .datastore import DataStore
from .experiment import Experiment
from .physical import PHYSICAL_FILE, PhysicalStore
from .result import Outcome, Result
from .suggestions import Suggestion, SuggestionCapture, reject_suggestions, select_suggestions


class Session:
    """Bind a backend and expose the experiment-level API over an SCQO-owned config."""

    def __init__(
        self,
        backend: Backend,
        *,
        state_path: str | None = None,
        data_root: str | Path | None = None,
        device_name: str = "device",
        state_sync: Literal["push", "pull"] = "pull",
        default_tags: list[str] | None = None,
        parameter_defaults: dict[str, dict[str, Any]] | None = None,
        parameter_defaults_source: str | None = None,
        backend_label: str | None = None,
        setup_name: str | None = None,
        cooldown_id: str | None = None,
    ) -> None:
        self.backend = backend
        #: provenance label recorded on every run — the resolved setup's backend
        #: ("qblox" / "qm" / "simulated"); the class name alone would be ambiguous.
        self.backend_label = backend_label or type(backend).__name__
        #: NAMED setup (+ its cooldown cycle) this session was resolved to — the era
        #: identity stamped verbatim on every run; "" for notebook Sessions that never
        #: went through build_session — they stamp the cycle's only setup, or "".
        self.setup_name = setup_name or ""
        self.cooldown_id = cooldown_id or ""
        #: where the per-SETUP instrument state+history persists (None = in-memory).
        self.state_path = state_path
        self._persist = state_path is not None
        #: authoritative SCQO config + history over the backend's vendor device. With
        #: ``state_sync="pull"`` (default) the vendor wins at startup and only history is
        #: loaded; ``"push"`` loads the saved SCQO config and pushes it into the vendor
        #: (only for devices SCQO fully owns — see scqo.config). The state file lives in
        #: the setup's per-(cooldown, setup) ``scqo/`` folder; each ChangeRecord is
        #: stamped with this session's setup.
        self.device = RecordingDevice(backend.device, state_path=state_path,
                                      on_load=state_sync, setup=self.setup_name or None)
        #: measured physics of the SAMPLE (T1, arch/dispersive parameters, ...) — one
        #: file per (cooldown, setup) context. When a state_path is set it sits beside
        #: it in the same ``scqo/`` folder (the make_session/CLI path); a setup-less
        #: direct-API session with a data_root falls back to a device-level
        #: <data_root>/<device>/physical.json; a bare state_path lands it next door.
        #: Losing measured physics on restart is THE regression the store prevents.
        if state_path is not None:  # the scqo/ folder (CLI) or the bare state dir (direct API)
            physical_path = Path(state_path).expanduser().parent / PHYSICAL_FILE
        elif data_root is not None:  # setup-less direct-API escape hatch: device-level
            physical_path = Path(data_root).expanduser() / device_name / PHYSICAL_FILE
        else:
            physical_path = None
        self.physical = PhysicalStore(physical_path, setup=self.setup_name or None)
        #: run datastore (folders + rebuildable SQLite index); None disables persistence.
        self.datastore = (
            DataStore(data_root, device_name=device_name,
                      setup=self.setup_name or None, cooldown=self.cooldown_id or None)
            if data_root is not None else None
        )
        self.default_tags = list(default_tags or [])
        #: standing per-experiment parameter defaults (``~/.scqo/parameters.toml`` via
        #: make_session), merged UNDER the caller's params in run(): code < file < caller.
        self.parameter_defaults = dict(parameter_defaults or {})
        self.parameter_defaults_source = parameter_defaults_source

    def catalog(self) -> list[dict]:
        """List available measurements with their JSON parameter schemas.

        When this Session carries standing parameter defaults, each affected schema
        shows the EFFECTIVE default (the file value) marked with an ``x-default-source``
        key naming the file, and a file-supplied key is dropped from ``required`` — a
        caller of this Session genuinely need not pass it. ``registry.catalog()`` keeps
        the pristine code schemas.
        """
        entries = registry.catalog()
        if not self.parameter_defaults:
            return entries
        source = self.parameter_defaults_source or "parameter_defaults"
        overlaid = []
        for entry in entries:
            table = self.parameter_defaults.get(entry["name"]) or {}
            props = entry["parameters_schema"].get("properties", {})
            # Keys unknown to the schema are skipped here; they fail at run() instead.
            known = {k: v for k, v in table.items() if k in props}
            if known:
                entry = copy.deepcopy(entry)  # registry entries stay pristine
                schema = entry["parameters_schema"]
                required = schema.get("required", [])
                for key, value in known.items():
                    schema["properties"][key]["default"] = value
                    schema["properties"][key]["x-default-source"] = source
                    if key in required:
                        required.remove(key)
            overlaid.append(entry)
        return overlaid

    def run(
        self,
        experiment: str,
        params: dict[str, Any],
        update: str | bool = "suggest",
        *,
        tags: list[str] | None = None,
        note: str = "",
    ) -> dict:
        """Run an experiment by name; return its structured result as a dict.

        ``update`` selects what happens to the fitted values after a successful run:

        * ``"suggest"`` (default) — ``update()`` runs against a capture shim: every
          write becomes a pending :class:`~scqo.suggestions.Suggestion` (qubit, field,
          store, before, after) returned in the payload's ``suggestions`` key and
          stored on the run record. NOTHING is applied — a human decides, right away
          (the CLI prompts) or later via :meth:`accept` with the run_id.
        * ``"apply"`` (or legacy ``True``) — capture, then immediately accept every
          suggestion through the same apply path: calibration knobs are pushed to the
          vendor + recorded in the change history, physical fields land in
          ``physical.json``, and both states are persisted. The pre-v0.6 behavior,
          kept for the AI loop / unattended bring-up.
        * ``"none"`` (or legacy ``False``) — analyze only; nothing is even captured.

        A failed run (a non-conforming probe dataset or a raising estimator) is
        returned as a structured result with ``error`` set and every qubit marked
        failed, never raised: the Session boundary stays JSON in/out. ``updated_device``
        on the run record means "at least one suggestion has been APPLIED" (a later
        :meth:`accept` flips it).

        With a ``data_root`` configured, **every** run — successful or failed — is saved
        to its own run folder and indexed; the returned dict gains ``run_id`` and
        ``data_path``. ``tags`` (merged with the Session's ``default_tags``) and ``note``
        are stored searchably. A persistence failure never destroys a measurement
        result: it is reported as ``datastore_error`` in the returned dict instead.

        Standing per-experiment defaults (``parameter_defaults``, wired from
        ``~/.scqo/parameters.toml`` by :func:`~scqo.labconfig.make_session`) are merged
        under ``params`` first — code defaults < defaults file < ``params``. Parameters
        that do not validate (a typo'd key, an out-of-range value) return a structured
        all-failed result naming the offending key — and, when it came from the defaults
        file, the file's path. Nothing is measured then and nothing is persisted (no
        ``run_id``): unlike a probe/estimator failure there is no dataset to debug.
        """
        mode = {True: "apply", False: "none"}.get(update, update)  # legacy bool aliases
        if mode not in ("suggest", "apply", "none"):
            raise ValueError(f"update must be 'suggest', 'apply' or 'none' (or a bool), got {update!r}")
        cls = registry.get(experiment)
        defaults = self.parameter_defaults.get(experiment, {})
        merged = {**defaults, **params}  # code defaults fill the rest at validation
        try:
            validated = cls.Parameters(**merged)
        except ValidationError as err:
            return self._invalid_params(cls, merged, defaults, params, err).model_dump(mode="json")
        exp = cls(self.backend, validated)
        exp.device = self.device  # route reads/writes through the recording config

        started_at = _now()
        run_id: str | None = None
        run_dir: Path | None = None
        if self.datastore is not None:
            run_id, run_dir = self.datastore.new_run_dir(experiment)
            exp.artifact_dir = run_dir / "analysis"  # scqat writes metadata/figures here

        device_before = self.device.snapshot()
        self.device.set_context(experiment, run_id)
        updated = False
        suggestions: list[Suggestion] = []
        try:
            try:
                result = exp.run()
            except Exception as err:  # probe/estimator failure -> structured result
                result = self._failure(cls, exp, err)
            else:
                if mode != "none" and result.any_success:
                    # Capture what update() WOULD write. Failures must not raise and
                    # must not destroy the measurement: the fit stays in the result.
                    capture = SuggestionCapture(self.device, self.physical)
                    exp.device = capture
                    try:
                        exp.update()
                        suggestions = capture.suggestions
                    except Exception as err:
                        result.error = (
                            f"measurement succeeded but suggestion capture failed "
                            f"(nothing suggested or applied): {type(err).__name__}: {err}"
                        )
                    finally:
                        exp.device = self.device
                    if mode == "apply" and suggestions:
                        try:
                            applied, errors = self._apply(
                                suggestions, experiment=experiment, run_id=run_id
                            )
                            updated = bool(applied)
                            if self._persist:
                                self.device.save()
                            self.physical.save()
                            if errors:
                                result.error = (
                                    f"measurement succeeded but device update failed "
                                    f"(device may be partially updated): {'; '.join(errors)}"
                                )
                        except Exception as err:  # save() etc. — never raise across the boundary
                            result.error = (
                                f"measurement succeeded but device update/save failed "
                                f"(device may be partially updated): {type(err).__name__}: {err}"
                            )
        finally:
            self.device.set_context(None, None)

        payload = result.model_dump(mode="json")
        suggestion_dicts = [s.model_dump(mode="json") for s in suggestions]
        if self.datastore is not None:
            assert run_id is not None and run_dir is not None
            # Raw output-chain values at run END (provenance; the absolute experiment's
            # sweep top is recoverable from parameters.json). Must never fail a run.
            try:
                power_context = self.backend.power_context(
                    list(getattr(exp.params, "qubits", []))) or {}
            except Exception:
                power_context = {}
            try:
                self.datastore.persist_run(
                    run_id=run_id,
                    run_dir=run_dir,
                    experiment=experiment,
                    params=exp.params,
                    dataset=exp.dataset,
                    result=payload,
                    device_before=device_before,
                    device_after=self.device.snapshot(),
                    started_at=started_at,
                    ended_at=_now(),
                    backend=self.backend_label,
                    updated_device=updated,
                    suggestions=suggestion_dicts,
                    power_context=power_context,
                    tags=list(dict.fromkeys([*self.default_tags, *(tags or [])])),
                    note=note,
                )
            except Exception as err:  # never lose a measurement over a save problem
                payload["datastore_error"] = f"{type(err).__name__}: {err}"
            else:
                payload["run_id"] = run_id
                payload["data_path"] = str(run_dir)
        payload["suggestions"] = suggestion_dicts  # also without a datastore (AI loop)
        return payload

    def _apply(
        self,
        suggestions: list[Suggestion],
        *,
        experiment: str | None,
        run_id: str | None,
        comment: str = "",
        reapply: bool = False,
    ) -> tuple[list[Suggestion], list[str]]:
        """Apply PENDING suggestions through the real stores; mutates their statuses.

        Calibration knobs go through the RecordingDevice (vendor-push-FIRST, then
        history) exactly like the pre-v0.6 live path; physical fields go to the
        PhysicalStore. Per-qubit atomicity: if one item fails (e.g. the vendor
        rejects a value), that qubit's REMAINING items are skipped — they stay
        pending with the error noted — but other qubits proceed. ``reapply`` also
        applies items that were ALREADY decided (rollback / accept-after-reject);
        they end up ``accepted`` with refreshed provenance, and the ChangeRecord's
        ``old`` is the live value at apply time — the truthful history. Returns
        (the items actually applied, error strings) — the status alone cannot tell
        (a failed reapply leaves its earlier ``accepted`` status in place). Does
        NOT save; the caller persists.
        """
        applied: list[Suggestion] = []
        errors: list[str] = []
        failed_qubits: set[str] = set()
        self.device.set_context(experiment, run_id)
        try:
            for s in suggestions:
                if (s.status != "pending" and not reapply) or s.qubit in failed_qubits:
                    continue
                try:
                    if s.store == "physical":
                        self.physical.record(
                            s.qubit, s.field, s.after, experiment=experiment, run_id=run_id
                        )
                    else:
                        setattr(self.device.qubit(s.qubit), s.field, s.after)
                except Exception as err:
                    failed_qubits.add(s.qubit)  # no half-applied qubit
                    s.comment = f"apply failed: {type(err).__name__}: {err}"
                    errors.append(f"{s.qubit}.{s.field}: {type(err).__name__}: {err}")
                    continue
                s.status = "accepted"
                s.decided_at = _now()
                s.decided_by = _config._current_operator() or None
                if comment:
                    s.comment = comment
                applied.append(s)
        finally:
            self.device.set_context(None, None)
        return applied, errors

    def accept(
        self,
        run_id: str,
        *,
        qubits: list[str] | None = None,
        fields: list[str] | None = None,
        indices: list[int] | None = None,
        comment: str = "",
        force: bool = False,
        reapply: bool = False,
        dry_run: bool = False,
    ) -> dict:
        """Apply a saved run's pending suggested updates — possibly days later.

        Selection: no filters = every pending item; ``qubits``/``fields`` narrow it;
        ``indices`` are 0-based positions in the stored list (partial accept is
        first-class). Two guards protect a deferred apply unless ``force``:

        * **era guard** — the run's (cooldown, setup name) must match the device's
          current one: a value measured under a different setup may not transfer;
        * **staleness guard** — each item's ``before`` must equal the store's CURRENT
          value: if something else updated the field since, the item is skipped and
          reported under ``stale``.

        ``reapply=True`` re-decides items that were ALREADY accepted/rejected —
        roll back to this run's value after regretting a newer one, or accept an
        item rejected earlier. A rollback deliberately overwrites a newer value, so
        the staleness guard is OFF in this mode (the summary's ``current`` field
        shows exactly what each item overwrote); the era guard still applies.

        Applied items go through the same path as ``update="apply"`` (vendor-push-
        first, ChangeRecords stamped with the ORIGINATING run_id), both states are
        saved, and the decision is persisted on the run record (survives reindex).
        Returns ``{run_id, applied, stale, errors, pending_left}``.

        ``dry_run=True`` evaluates and REPORTS instead of applying — the CLI's
        confirmation prompts are built from it. Nothing is mutated or saved, the
        era mismatch is reported rather than raised, and the selection always
        includes decided items (the preview must describe what a re-apply would
        confirm). Returns ``{run_id, era: {run, current, match}, items: [{index,
        qubit, field, store, status, before, current, after, stale, decided_at,
        decided_by, comment}]}``. The wrong-device check still raises.
        """
        store = self._require_datastore()
        record = store.load_run(run_id)["record"]
        if record.get("device") != store.device_name:
            raise RuntimeError(
                f"run {run_id} belongs to device {record.get('device')!r} but this session "
                f"is bound to {store.device_name!r} — select that device and retry"
            )
        suggestions = [Suggestion(**s) for s in record.get("suggestions", [])]
        selected = select_suggestions(suggestions, qubits=qubits, fields=fields,
                                      indices=indices, include_decided=reapply or dry_run)

        run_era = (record.get("cooldown", ""), record.get("setup", ""))
        if dry_run or not force:  # under force the registry is deliberately not consulted
            current_era = store.run_stamps()
            era = {"run": list(run_era), "current": list(current_era),
                   "match": run_era == current_era}
        else:
            era = {"run": list(run_era), "current": None, "match": True}

        device_snapshot = self.device.snapshot()
        items: list[dict[str, Any]] = []
        for i in selected:
            s = suggestions[i]
            current = (
                self.physical.get(s.qubit, s.field)
                if s.store == "physical"
                else device_snapshot.get(s.qubit, {}).get(s.field)
            )
            items.append({
                "index": i, "qubit": s.qubit, "field": s.field, "store": s.store,
                "status": s.status, "before": s.before, "current": current,
                "after": s.after, "stale": current != s.before,
                "decided_at": s.decided_at, "decided_by": s.decided_by,
                "comment": s.comment,
            })
        if dry_run:
            return {"run_id": run_id, "era": era, "items": items}

        summary: dict[str, Any] = {"run_id": run_id, "applied": [], "stale": [], "errors": []}
        if not selected:
            summary["pending_left"] = sum(1 for s in suggestions if s.status == "pending")
            return summary

        if not force and not era["match"]:
            raise RuntimeError(
                f"run {run_id} was measured under cooldown/setup {run_era} but the device "
                f"is now on {tuple(era['current'])} — its values may not transfer; use "
                f"force=True (--force) to apply anyway"
            )

        to_apply: list[Suggestion] = []
        current_of: dict[int, float | None] = {}
        for item in items:
            s = suggestions[item["index"]]
            current_of[id(s)] = item["current"]
            # A reapply deliberately overwrites a newer value, so staleness cannot
            # be an error there — the summary's `current` shows what it overwrote.
            if not force and not reapply and item["stale"]:
                summary["stale"].append(
                    {"qubit": s.qubit, "field": s.field, "before": s.before,
                     "current": item["current"], "after": s.after}
                )
                continue
            to_apply.append(s)

        applied, errors = self._apply(
            to_apply, experiment=record.get("experiment"), run_id=run_id,
            comment=comment, reapply=reapply,
        )
        summary["errors"] = errors
        summary["applied"] = [
            {"qubit": s.qubit, "field": s.field, "store": s.store,
             "before": s.before, "current": current_of.get(id(s)), "after": s.after}
            for s in applied
        ]
        # From here on nothing may raise: the vendor already carries the applied
        # values, so the decision MUST reach record.json (else a retry could
        # double-apply) and any save problem is reported, not thrown.
        if applied:
            try:
                if self._persist:
                    self.device.save()
                self.physical.save()
            except Exception as err:
                summary["errors"].append(
                    f"values were applied to the device but saving state failed "
                    f"(state files may lag the instrument): {type(err).__name__}: {err}"
                )
        try:
            store.update_suggestions(
                run_id,
                [s.model_dump(mode="json") for s in suggestions],
                updated_device=True if applied else None,
            )
        except Exception as err:
            summary["errors"].append(
                f"values were applied to the device but persisting the decision failed — "
                f"record.json still lists them as pending (a blind retry could double-apply): "
                f"{type(err).__name__}: {err}"
            )
        summary["pending_left"] = sum(1 for s in suggestions if s.status == "pending")
        return summary

    def reject(
        self,
        run_id: str,
        *,
        qubits: list[str] | None = None,
        fields: list[str] | None = None,
        indices: list[int] | None = None,
        comment: str = "",
    ) -> dict:
        """Decline pending suggestions (metadata only — no instrument is touched).

        Same selection semantics as :meth:`accept`; no era/staleness guards apply
        because nothing is written to any store. The decision (status, operator,
        timestamp, comment) is persisted on the run record.
        """
        return reject_suggestions(
            self._require_datastore(), run_id,
            qubits=qubits, fields=fields, indices=indices, comment=comment,
        )

    @staticmethod
    def _failure(cls: type[Experiment], exp: Experiment, err: Exception) -> Result:
        """Build an all-failed structured result for a run that could not complete."""
        outcome = Outcome.NO_DATA if isinstance(err, ContractError) else Outcome.FAILED
        qubits = getattr(exp.params, "qubits", [])
        return cls.Result(
            outcomes={q: outcome for q in qubits},
            error=f"{type(err).__name__}: {err}",
        )

    def _invalid_params(
        self, cls: type[Experiment], merged: dict, defaults: dict, caller: dict, err: ValidationError
    ) -> Result:
        """Structured all-failed result for parameters that did not validate.

        A bad key present only in the defaults overlay is attributed to the parameters
        file by path — the fix belongs there, not on the command line.
        """
        hints = []
        for detail in err.errors():
            key = str(detail["loc"][0]) if detail.get("loc") else ""
            if key and key in defaults and key not in caller:
                source = self.parameter_defaults_source or "the session's parameter_defaults"
                hints.append(f"'{key}' came from {source} [{cls.name}] — fix it there")
        qubits = merged.get("qubits")
        qubits = [q for q in qubits if isinstance(q, str)] if isinstance(qubits, list) else []
        message = f"invalid parameters for {cls.name!r}: {err}"
        if hints:
            message += "\n" + "\n".join(hints)
        return cls.Result(outcomes={q: Outcome.FAILED for q in qubits}, error=message)

    # ------------------------------------------------------------------ datastore
    def find_runs(self, **filters: Any) -> list[dict]:
        """Query saved runs (newest first). Filters: experiment, qubit, tag, since,
        until, outcome, device, operator, cooldown, setup, pending, limit. Returns []
        when no ``data_root`` is configured. ``pending=True`` = runs with undecided
        suggested updates; ``setup`` filters by setup NAME (unique per cycle only —
        combine with ``cooldown``)."""
        if self.datastore is None:
            return []
        return self.datastore.find_runs(**filters)

    def load_run(self, run_id: str) -> dict:
        """Reload a saved run: record + parameters + result + figure paths (JSON-able)."""
        return self._require_datastore().load_run(run_id)

    def tag_run(
        self, run_id: str, *, add: list[str] | None = None,
        remove: list[str] | None = None, note: str | None = None,
    ) -> dict:
        """Add/remove searchable tags (or set the note) on an already-saved run."""
        return self._require_datastore().tag_run(run_id, add=add, remove=remove, note=note)

    def _require_datastore(self) -> DataStore:
        if self.datastore is None:
            raise RuntimeError("this Session has no data_root configured (no datastore)")
        return self.datastore

    # ------------------------------------------------------------------ state
    def device_state(self) -> dict:
        """Return a JSON snapshot of the authoritative SCQO calibration config."""
        return self.device.snapshot()

    def physical_state(self) -> dict:
        """The sample's measured physics for THIS SESSION'S (cooldown, setup) context
        (flat ``{qubit: {field: value}}``). Other contexts' measurements live in their
        own files; compare across them via the run index / trends."""
        return self.physical.snapshot()

    def live_sources(self) -> dict:
        """Which run each CURRENT value traces to (both stores; JSON-able).

        ``{"instrument": {qubit: {field: info}}, "physical": {...}}`` under the
        strict-match rule of :mod:`scqo.provenance`: a run is credited only while
        its recorded value still equals the live one — vendor reseeds and other
        tools' writes show as ``"external"``, never a false credit. Both stores are
        per (cooldown, setup), so their whole history belongs to this context.
        """
        from .provenance import live_sources as _live_sources

        return {
            "instrument": _live_sources(self.device_state(), self.history(store="instrument")),
            "physical": _live_sources(self.physical_state(), self.history(store="physical")),
        }

    def history(self, store: str = "instrument") -> list[dict]:
        """Return the recorded change history (JSON-able): the loop's memory.

        ``store="instrument"`` (default) reads the device-config history;
        ``"physical"`` reads the physical-parameter history (``physical.json``).
        """
        if store == "physical":
            return [record.as_dict() for record in self.physical.history()]
        if store != "instrument":
            raise ValueError(f"store must be 'instrument' or 'physical', got {store!r}")
        return [record.as_dict() for record in self.device.history()]
