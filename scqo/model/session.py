"""Session — the single entry point used identically by humans and AI agents.

Greenfield port of the state half of :mod:`scqo.session` (the run()/catalog()
experiment flow plugs in with the experiment-base cutover). Everything
crosses this boundary as plain JSON-able Python, so the same calls drive a
manual notebook and an LLM tool-use loop::

    sess.set_values({"q0.pi_amp": 0.2})   # runless manual write, validated,
                                          #   recorded as (manual)
    sess.suggest(run_id, {"q0.readout_freq_hz": 5.91e9})  # attach a
                                          #   figure-read value to a run
    sess.accept(run_id)                   # apply pending suggestions
    sess.reject(run_id, comment="...")    # decline them (metadata only)
    sess.device_state()                   # per-entity operating state
    sess.qubit_state("q0")                # the per-qubit assembled view
    sess.physical_state()                 # the sample's measured physics
    sess.history()                        # every recorded change

Addressing is the QUBIT-CLOSURE sugar (:meth:`Roster.resolve_field`):
``q0.pi_amp`` routes to ``q0_xy``, ``q0.readout_freq_hz`` to ``q0_ro``,
``q0.f_r_hz`` to ``q0_res`` — explicit entity names always work. Suggestions
carry the field's ROLE; applying routes facts to the physical store and
knobs/monitors through the recording device (vendor-push-first).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from ..datastore import DataStore
from .design import Design
from .device import CompositeView, RecordingDevice
from .roster import Roster
from .stores import Store, _current_operator, _now, physical_store, state_store
from .suggestions import (
    Suggestion,
    decision_editor,
    load_suggestions,
    pending_count,
    reject_suggestions,
    select_suggestions,
)


class Session:
    """Bind a backend and expose the state-management API over an SCQO-owned
    config (the experiment flow arrives with the experiment cutover)."""

    def __init__(
        self,
        backend,
        roster: Roster,
        *,
        design: Design | None = None,
        scqo_dir: str | Path | None = None,
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
        self.roster = roster
        self.design = design if design is not None else Design({})
        self.backend_label = backend_label or type(backend).__name__
        self.setup_name = setup_name or ""
        self.cooldown_id = cooldown_id or ""
        #: the per-(cooldown, setup) scqo/ folder holding both stores
        #: (None = in-memory session: full validation, no persistence).
        self.scqo_dir = Path(scqo_dir) if scqo_dir is not None else None
        self._persist = scqo_dir is not None
        self.state = state_store(scqo_dir, roster, setup=self.setup_name)
        if scqo_dir is None and data_root is not None:
            # Setup-less direct-API escape hatch: measured physics still
            # persists at the device level — losing it on restart is THE
            # regression the store prevents (old-session parity).
            from .stores import PHYSICAL_FILE, Store
            self.physical = Store(
                Path(data_root) / device_name / PHYSICAL_FILE, roster,
                roles=frozenset({"fact"}), setup=self.setup_name)
        else:
            self.physical = physical_store(scqo_dir, roster,
                                           setup=self.setup_name)
        self.device = RecordingDevice(backend.device, roster, self.state,
                                      on_load=state_sync)
        self.datastore = (
            DataStore(data_root, device_name=device_name,
                      setup=self.setup_name or None,
                      cooldown=self.cooldown_id or None)
            if data_root is not None else None)
        self.default_tags = list(default_tags or [])
        self.parameter_defaults = dict(parameter_defaults or {})
        self.parameter_defaults_source = parameter_defaults_source

    # ------------------------------------------------------------- writing

    def _write_state(self, entity: str, field: str, value) -> None:
        """One knob/monitor write through the recording device (push-first
        for knobs; record-only for monitors)."""
        view = self.device.component(entity)
        if isinstance(view, CompositeView):
            view.write_knob(field, value)
        else:
            setattr(view, field, value)

    def _apply(
        self,
        suggestions: list[Suggestion],
        *,
        experiment: str | None,
        run_id: str | None,
        comment: str = "",
        reapply: bool = False,
    ) -> tuple[list[Suggestion], list[str]]:
        """Apply PENDING suggestions through the real stores; mutates their
        statuses. Facts go to the physical store, knobs/monitors through the
        recording device (vendor-push-FIRST). Per-entity atomicity: one
        failed item skips that entity's REMAINING items; other entities
        proceed. Does NOT save; the caller persists."""
        applied: list[Suggestion] = []
        errors: list[str] = []
        eligible = [s for s in suggestions
                    if s.status == "pending" or reapply]
        # Group per entity, ordered by catalog field position within the
        # group: a stored [waveform, dt] pair applies dt-first (the same
        # ordering doctrine as the push order), and the group's relation
        # consistency is pre-checked so a doomed group fails WHOLE, with the
        # cause on every item, before any of it touches a store.
        groups: dict[str, list[Suggestion]] = {}
        for s in eligible:
            groups.setdefault(s.entity, []).append(s)
        self.device.set_context(experiment, run_id)
        try:
            for entity, group in groups.items():
                order = {f: i for i, f
                         in enumerate(self.roster.fields_of(entity))}
                group.sort(key=lambda s: order.get(s.field, len(order)))
                try:
                    self._validate_batch(
                        [(s.entity, s.field,
                          self.roster.fields_of(s.entity)[s.field], s.after)
                         for s in group])
                except ValueError as err:
                    for s in group:
                        s.comment = f"apply failed: {err}"
                    errors.append(f"{entity}: {err}")
                    continue
                for s in group:
                    try:
                        if s.role == "fact":
                            self.physical.record(s.entity, s.field, s.after,
                                                 experiment=experiment,
                                                 run_id=run_id)
                        else:
                            self._write_state(s.entity, s.field, s.after)
                    except Exception as err:
                        s.comment = (f"apply failed: "
                                     f"{type(err).__name__}: {err}")
                        errors.append(f"{s.entity}.{s.field}: "
                                      f"{type(err).__name__}: {err}")
                        break  # no half-applied entity
                    s.status = "accepted"
                    s.decided_at = _now()
                    s.decided_by = _current_operator() or None
                    if comment:
                        s.comment = comment
                    applied.append(s)
        finally:
            self.device.set_context(None, None)
        return applied, errors

    # -------------------------------------------------------- suggest/accept

    def accept(
        self,
        run_id: str,
        *,
        entities: list[str] | None = None,
        fields: list[str] | None = None,
        indices: list[int] | None = None,
        comment: str = "",
        force: bool = False,
        reapply: bool = False,
        dry_run: bool = False,
    ) -> dict:
        """Apply a saved run's pending suggested updates — possibly days
        later. Era guard (the run's cooldown/setup must match the current
        one) and staleness guard (each item's ``before`` must equal the
        store's CURRENT value) protect a deferred apply unless ``force``;
        ``reapply`` re-decides already-decided items with staleness OFF (a
        rollback deliberately overwrites). ``dry_run`` evaluates and reports
        without mutating anything."""
        store, record = self._load_run_record(run_id)
        suggestions = load_suggestions(record.get("suggestions", []))
        selected = select_suggestions(
            suggestions, entities=entities, fields=fields, indices=indices,
            include_decided=reapply or dry_run)

        run_era = (record.get("cooldown", ""), record.get("setup", ""))
        if dry_run or not force:
            current_era = store.run_stamps()
            era = {"run": list(run_era), "current": list(current_era),
                   "match": run_era == current_era}
        else:
            era = {"run": list(run_era), "current": None, "match": True}

        device_snapshot = self.device.snapshot()
        items: list[dict[str, Any]] = []
        for i in selected:
            s = suggestions[i]
            current = (self.physical.get(s.entity, s.field)
                       if s.role == "fact"
                       else device_snapshot.get(s.entity, {}).get(s.field))
            items.append({
                "index": i, "entity": s.entity, "field": s.field,
                "role": s.role, "status": s.status, "before": s.before,
                "current": current, "after": s.after,
                "stale": current != s.before, "decided_at": s.decided_at,
                "decided_by": s.decided_by, "comment": s.comment,
            })
        if dry_run:
            return {"run_id": run_id, "era": era, "items": items}

        summary: dict[str, Any] = {"run_id": run_id, "applied": [],
                                   "stale": [], "errors": []}
        if not selected:
            summary["pending_left"] = pending_count(suggestions)
            return summary

        if not force and not era["match"]:
            raise RuntimeError(
                f"run {run_id} was measured under cooldown/setup {run_era} "
                f"but the device is now on {tuple(era['current'])} — its "
                f"values may not transfer; use force=True (--force) to "
                f"apply anyway")

        to_apply: list[Suggestion] = []
        current_of: dict[int, Any] = {}
        for item in items:
            s = suggestions[item["index"]]
            current_of[id(s)] = item["current"]
            # A reapply deliberately overwrites a newer value, so staleness
            # cannot be an error there.
            if not force and not reapply and item["stale"]:
                summary["stale"].append(
                    {"entity": s.entity, "field": s.field,
                     "before": s.before, "current": item["current"],
                     "after": s.after})
                continue
            to_apply.append(s)

        applied, errors = self._apply(
            to_apply, experiment=record.get("experiment"), run_id=run_id,
            comment=comment, reapply=reapply)
        summary["errors"] = errors
        summary["applied"] = [
            {"entity": s.entity, "field": s.field, "role": s.role,
             "before": s.before, "current": current_of.get(id(s)),
             "after": s.after}
            for s in applied]
        # From here on nothing may raise: the vendor already carries the
        # applied values, so the decision MUST reach record.json.
        if applied:
            try:
                if self._persist:
                    self.device.save()
                self.physical.save()
            except Exception as err:
                summary["errors"].append(
                    f"values were applied to the device but saving state "
                    f"failed (state files may lag the instrument): "
                    f"{type(err).__name__}: {err}")
        try:
            stored = store.edit_suggestions(
                run_id,
                decision_editor({i: suggestions[i].model_dump(mode="json")
                                 for i in selected}),
                updated_device=True if applied else None)
            summary["pending_left"] = pending_count(stored["suggestions"])
        except Exception as err:
            summary["errors"].append(
                f"values were applied to the device but persisting the "
                f"decision failed — record.json still lists them as pending "
                f"(a blind retry could double-apply): "
                f"{type(err).__name__}: {err}")
            summary["pending_left"] = pending_count(suggestions)
        return summary

    def reject(self, run_id: str, *, entities: list[str] | None = None,
               fields: list[str] | None = None,
               indices: list[int] | None = None, comment: str = "") -> dict:
        """Decline pending suggestions (metadata only)."""
        return reject_suggestions(
            self._require_datastore(), run_id, entities=entities,
            fields=fields, indices=indices, comment=comment)

    def suggest(self, run_id: str, assignments: dict[str, Any],
                comment: str = "") -> dict:
        """Attach YOUR manually-read values to a saved run as pending
        suggestions (origin="operator"); decided exactly like estimator
        suggestions. ``before`` is captured NOW — what the staleness guard
        compares at accept time."""
        if not assignments:
            raise ValueError(
                "no assignments given — expected {'entity.field': value}")
        store, record = self._load_run_record(run_id)
        if any("entity" not in r for r in record.get("suggestions", [])):
            raise ValueError(
                f"run {run_id}'s stored suggestions are pre-greenfield rows "
                f"(display-only) — appending would make yours undecidable "
                f"too; propose against a new run")
        proposed_at = _now()
        new: list[Suggestion] = []
        for entity, field, spec, value in self._parse_assignments(
                assignments, relations=False):
            new.append(Suggestion(
                entity=entity, field=field, role=spec.role,
                kind=self.roster.entities[entity].kind, unit=spec.unit,
                before=self._current_value(entity, field, spec.role),
                after=value, comment=comment, origin="operator",
                proposed_by=_current_operator() or None,
                proposed_at=proposed_at))
        new_dicts = [s.model_dump(mode="json") for s in new]
        stored = store.edit_suggestions(run_id, lambda fresh: fresh + new_dicts)
        return {
            "run_id": run_id,
            "added": [{"entity": s.entity, "field": s.field, "role": s.role,
                       "before": s.before, "after": s.after} for s in new],
            "pending_total": pending_count(stored["suggestions"]),
        }

    def set_values(self, assignments: dict[str, Any], *,
                   dry_run: bool = False) -> dict:
        """Write operator-known values directly — the RUNLESS counterpart of
        suggest. ALL assignments validate before ANYTHING is written; writes
        go through the normal stores immediately (knobs vendor-push-FIRST,
        ChangeRecord with experiment=None — the ``(manual)`` provenance).
        Per-entity atomicity as in accept."""
        if not assignments:
            raise ValueError(
                "no assignments given — expected {'entity.field': value}")
        validated = self._parse_assignments(assignments)

        if dry_run:
            return {"items": [
                {"entity": n, "field": f, "role": spec.role,
                 "unit": spec.unit,
                 "current": self._current_value(n, f, spec.role), "after": v}
                for n, f, spec, v in validated]}

        applied: list[dict[str, Any]] = []
        errors: list[str] = []
        failed_entities: set[str] = set()
        for entity, field, spec, value in validated:
            if entity in failed_entities:
                continue
            # `before` is read live right before the write: an earlier item
            # of this call — or its coupled echo — may have moved it.
            before = self._current_value(entity, field, spec.role)
            try:
                if spec.role == "fact":
                    self.physical.record(entity, field, value)
                else:
                    self._write_state(entity, field, value)
            except Exception as err:
                failed_entities.add(entity)  # no half-applied entity
                errors.append(
                    f"{entity}.{field}: {type(err).__name__}: {err}")
                continue
            applied.append({"entity": entity, "field": field,
                            "role": spec.role, "before": before,
                            "after": value})
        summary: dict[str, Any] = {"applied": applied, "errors": errors}
        if applied:
            try:
                if self._persist:
                    self.device.save()
                self.physical.save()
            except Exception as err:
                summary["errors"].append(
                    f"values were applied to the device but saving state "
                    f"failed (state files may lag the instrument): "
                    f"{type(err).__name__}: {err}")
        return summary

    # ------------------------------------------------------------ plumbing

    def _parse_assignments(self, assignments: dict[str, Any], *,
                           relations: bool = True):
        """Shared suggest/set_values validation: every key is
        ``entity.field`` resolved through the qubit-closure sugar; every
        value passes the owning store's shape/finiteness validation. ALL
        assignments validate before anything is written; two keys that
        alias one resolved (entity, field) are refused.

        ``relations=True`` (set_values — a direct write must be immediately
        consistent) additionally validates the batch AS A SEQUENCE: the
        waveform-dt prerequisite per step (satisfiable by ordering the dict
        dt-first), and paired-array lengths at batch END — so a redone fit
        may change both partners' length in ONE call. ``relations=False``
        (suggest — a proposal's partner may itself only be proposed) skips
        that; the accept re-checks when it records."""
        out = []
        resolved: dict[tuple[str, str], str] = {}
        for key, value in assignments.items():
            name, _, field = key.partition(".")
            if not name or not field:
                raise ValueError(
                    f"assignment key {key!r} must be 'entity.field' "
                    f"(e.g. q1.pi_amp, q1_res.f_r_hz)")
            try:
                entity, spec = self.roster.resolve_field(name, field)
            except Exception as err:
                raise ValueError(str(err)) from None
            prior = resolved.get((entity, field))
            if prior is not None:
                raise ValueError(
                    f"{key!r} and {prior!r} both resolve to "
                    f"{entity}.{field} — one assignment per field")
            resolved[(entity, field)] = key
            store = self.physical if spec.role == "fact" else self.state
            try:
                value = store.check(entity, field, value, relations=False)
            except Exception as err:
                raise ValueError(str(err)) from None
            out.append((entity, field, spec, value))
        if relations:
            self._validate_batch(out)
        return out

    def _batch_reader(self, overlay: dict):
        def current(entity: str, field: str):
            if field in overlay.get(entity, {}):
                return overlay[entity][field]
            spec = self.roster.fields_of(entity)[field]
            store = self.physical if spec.role == "fact" else self.state
            return store.get(entity, field)
        return current

    def _validate_batch(self, items) -> None:
        """Relation consistency of the WHOLE batch before anything is
        written: waveform-dt per step against the batch overlay, paired
        lengths at batch END per touched entity."""
        overlay: dict[str, dict[str, Any]] = {}
        current = self._batch_reader(overlay)
        for entity, field, spec, value in items:
            if (spec.shape == "float[]" and field.endswith("_waveform")
                    and current(entity, f"{field}_dt_s") is None):
                raise ValueError(
                    f"{entity}.{field}: set {field}_dt_s first (in the same "
                    f"call is fine — order the assignments dt before "
                    f"waveform)")
            overlay.setdefault(entity, {})[field] = value
        for entity in overlay:
            for field, spec in self.roster.fields_of(entity).items():
                if not spec.paired_with:
                    continue
                a = current(entity, field)
                b = current(entity, spec.paired_with)
                if a is not None and b is not None and len(a) != len(b):
                    raise ValueError(
                        f"{entity}.{field}/{spec.paired_with}: the batch "
                        f"would leave unequal paired lengths ({len(a)} != "
                        f"{len(b)}) — change both sides in one call")

    def _current_value(self, entity: str, field: str, role: str):
        if role == "fact":
            return self.physical.get(entity, field)
        return self.device.snapshot().get(entity, {}).get(field)

    def _load_run_record(self, run_id: str) -> tuple[DataStore, dict]:
        store = self._require_datastore()
        record = store.load_run(run_id)["record"]
        if record.get("device") != store.device_name:
            raise RuntimeError(
                f"run {run_id} belongs to device {record.get('device')!r} "
                f"but this session is bound to {store.device_name!r}")
        return store, record

    def _require_datastore(self) -> DataStore:
        if self.datastore is None:
            raise RuntimeError(
                "this Session has no data_root configured (no datastore)")
        return self.datastore

    # ----------------------------------------------------------- datastore

    def find_runs(self, **filters: Any) -> list[dict]:
        """Query saved runs (newest first); [] without a data_root."""
        if self.datastore is None:
            return []
        return self.datastore.find_runs(**filters)

    def load_run(self, run_id: str) -> dict:
        return self._require_datastore().load_run(run_id)

    def tag_run(self, run_id: str, *, add: list[str] | None = None,
                remove: list[str] | None = None,
                note: str | None = None) -> dict:
        return self._require_datastore().tag_run(run_id, add=add,
                                                 remove=remove, note=note)

    # --------------------------------------------------------------- state

    def device_state(self) -> dict:
        """The operating state per entity (knobs + monitors)."""
        return self.device.snapshot()

    def physical_state(self) -> dict:
        """The sample's measured physics for THIS context."""
        return self.physical.values()

    def qubit_state(self, name: str) -> dict:
        """The per-qubit ASSEMBLED view: the mode's facts plus every closure
        member (default channels, attached resonator) — grouping is derived
        at read time from refs, never declared."""
        e = self.roster.entities.get(name)
        if e is None:
            raise KeyError(f"unknown entity {name!r}")
        from .entities import Mode
        if not isinstance(e, Mode):
            hint = (f" — did you mean {e.target[0]!r}?"
                    if getattr(e, "target", None) and len(e.target) == 1
                    else "")
            raise KeyError(
                f"qubit_state takes a mode; {name!r} is a "
                f"{type(e).__name__.lower()}{hint}")
        out: dict[str, dict] = {}
        physical = self.physical.values()
        state = self.device.snapshot()
        members = [name]
        members += [c.name for c in self.roster.channels_of(name)
                    if self.roster.defaults.get((name, c.kind)) == c.name]
        members += [m.name for m in self.roster.modes().values()
                    if m.refs.get("qubit") == name]
        for member in members:
            merged = {**physical.get(member, {}), **state.get(member, {})}
            if merged:
                out[member] = merged
        return out

    def history(self, store: str = "state") -> list[dict]:
        """The recorded change history (the loop's memory): ``"state"``
        (knobs + monitors) or ``"physical"`` (measured facts)."""
        if store == "physical":
            return [r.as_dict() for r in self.physical.history()]
        if store != "state":
            raise ValueError(
                f"store must be 'state' or 'physical', got {store!r}")
        return [r.as_dict() for r in self.device.history()]
