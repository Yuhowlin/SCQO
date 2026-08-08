"""Suggested updates — captured from ``update()``, decided by a human.

Greenfield port of :mod:`scqo.suggestions`. An experiment's ``update()``
never writes directly: the session swaps a :class:`SuggestionCapture` shadow
device in around the call, so every write — attribute-style on mode/channel
entities, ``write_knob`` on composites — becomes a :class:`Suggestion`
instead of a live write. The captured list is stored on the run record
(``record.json`` — the truth) with per-item status pending/accepted/rejected.

Routing carries the field's ROLE (the one store-vocabulary of the model):
``fact`` applies to the physical store, ``knob``/``monitor`` through the
recording device. Cross-entity proposals are first-class — a resonator fit
captured while targeting ``q1`` lands on ``q1_res`` and is reviewed under
that name. Values may be arrays (a suggested waveform) — ``before``/``after``
are ``float | list[float]``.

Rows from the pre-greenfield era (``component``/``category`` keys) are
display-only: :func:`load_suggestions` refuses to DECIDE them, with a message
saying so (the fresh-start rule for record data that stays readable).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel

from .catalog import FieldSpec
from .device import CompositeView, EntityView
from .entities import Composite
from .roster import Roster
from .stores import Store, _current_operator, _now


class Suggestion(BaseModel):
    """One proposed field update, as stored on the run record."""

    entity: str
    field: str
    #: the field's role — the store router: fact -> physical.json,
    #: knob/monitor -> the recording device / state store.
    role: Literal["fact", "knob", "monitor"]
    #: the entity's kind, stamped at capture (self-describing rows).
    kind: str | None = None
    #: the field's unit, stamped at capture (review tables need no catalog).
    unit: str = ""
    #: value at capture time (None = not measured before); the staleness
    #: guard compares this against the store's CURRENT value at accept time.
    before: float | list[float] | None
    after: float | list[float]
    status: Literal["pending", "accepted", "rejected"] = "pending"
    decided_at: str | None = None
    decided_by: str | None = None
    #: the decision comment; a failed apply also notes its error here (status
    #: stays pending so the item remains decidable once the cause is fixed).
    comment: str = ""
    #: who proposed the value: "estimator" (captured from update()) or
    #: "operator" (attached via Session.suggest).
    origin: Literal["estimator", "operator"] = "estimator"
    proposed_by: str | None = None
    proposed_at: str | None = None
    #: CAMPAIGN-level rows stamp the experiment whose update() proposed the
    #: value (the campaign accept groups by it); run-level rows leave it None
    #: — the run record already knows its experiment. On the model, not a
    #: loose key: decision_editor round-trips model_dump, which drops
    #: anything undeclared.
    experiment: str | None = None


def load_suggestions(rows: list[dict]) -> list[Suggestion]:
    """Stored rows -> models; pre-greenfield rows are refused with a clear
    message (they stay readable on the record, but cannot be decided by this
    code — the fresh-start rule)."""
    out = []
    for r in rows:
        if "entity" not in r:
            raise ValueError(
                "this run's suggestions are pre-greenfield rows "
                "(component/category schema) — display-only under the "
                "fresh-start policy; re-run the experiment to get decidable "
                "proposals")
        out.append(Suggestion(**r))
    return out


def pending_count(suggestions: list[dict] | list[Suggestion]) -> int:
    """How many suggestions are still undecided (models or plain dicts)."""
    return sum(1 for s in suggestions if _get(s, "status") == "pending")


def _get(s: dict | Suggestion, key: str):
    return s.get(key) if isinstance(s, dict) else getattr(s, key)


def select_suggestions(
    suggestions: list[Suggestion],
    *,
    entities: list[str] | None = None,
    fields: list[str] | None = None,
    indices: list[int] | None = None,
    include_decided: bool = False,
) -> list[int]:
    """0-based positions of the PENDING suggestions matching the filters.

    ``indices`` index the FULL stored list (order is stable — records are
    never reordered); a decided index is simply not selected.
    ``include_decided=True`` (the ``--reapply`` mode) lifts the pending-only
    rule."""
    out = []
    for i, s in enumerate(suggestions):
        if s.status != "pending" and not include_decided:
            continue
        if indices is not None and i not in indices:
            continue
        if entities is not None and s.entity not in entities:
            continue
        if fields is not None and s.field not in fields:
            continue
        out.append(i)
    return out


def decision_editor(touched: dict[int, dict]):
    """Editor for ``DataStore.edit_suggestions`` that replaces ONLY the rows
    a decision touched — the stored list is append-only (suggest appends,
    decisions mutate in place), so positions are stable across writers."""

    def _apply(fresh: list[dict]) -> list[dict]:
        merged = list(fresh)
        for i, item in touched.items():
            if i < len(merged):  # can only fail on a hand-truncated record
                merged[i] = item
        return merged

    return _apply


def reject_suggestions(
    store,
    run_id: str,
    *,
    entities: list[str] | None = None,
    fields: list[str] | None = None,
    indices: list[int] | None = None,
    comment: str = "",
) -> dict:
    """Decline pending suggestions on a saved run — metadata only."""
    record = store.load_run(run_id)["record"]
    suggestions = load_suggestions(record.get("suggestions", []))
    selected = select_suggestions(suggestions, entities=entities,
                                  fields=fields, indices=indices)
    for i in selected:
        s = suggestions[i]
        s.status = "rejected"
        s.decided_at = _now()
        s.decided_by = _current_operator() or None
        s.comment = comment
    stored = store.edit_suggestions(
        run_id, decision_editor({i: suggestions[i].model_dump(mode="json")
                                 for i in selected}))
    return {
        "run_id": run_id,
        "rejected": [{"entity": suggestions[i].entity,
                      "field": suggestions[i].field} for i in selected],
        "pending_left": pending_count(stored["suggestions"]),
    }


def reject_campaign_suggestions(
    store,
    campaign_id: str,
    *,
    entities: list[str] | None = None,
    fields: list[str] | None = None,
    indices: list[int] | None = None,
    comment: str = "",
) -> dict:
    """Decline pending CAMPAIGN-level suggestions — metadata only, the
    campaign twin of :func:`reject_suggestions` (rows live on campaign.json,
    written through the locked ``edit_campaign_suggestions``)."""
    manifest = store.load_campaign(campaign_id)["manifest"]
    suggestions = load_suggestions(manifest.get("suggestions", []))
    selected = select_suggestions(suggestions, entities=entities,
                                  fields=fields, indices=indices)
    for i in selected:
        s = suggestions[i]
        s.status = "rejected"
        s.decided_at = _now()
        s.decided_by = _current_operator() or None
        s.comment = comment
    stored = store.edit_campaign_suggestions(
        campaign_id, decision_editor({i: suggestions[i].model_dump(mode="json")
                                      for i in selected}))
    return {
        "campaign_id": campaign_id,
        "rejected": [{"entity": suggestions[i].entity,
                      "field": suggestions[i].field} for i in selected],
        "pending_left": pending_count(stored.get("suggestions", [])),
    }


# ------------------------------------------------------------------ capture

def _capture_property(field: str, spec: FieldSpec) -> property:
    """Reads the real stores, turns writes into Suggestions."""

    def getter(self: EntityView):
        return self._parent._current(self.name, field)

    def setter(self: EntityView, value) -> None:
        self._parent._suggest(self.name, field, value)

    return property(getter, setter,
                    doc=f"{spec.doc} [{spec.unit}]" if spec.unit else spec.doc)


_capture_view_classes: dict[tuple, type] = {}


def _capture_view_class(roster: Roster, name: str) -> type:
    """The capture-view class for one entity's field set (cached by kind +
    field tuple). Exposes the UNION of the entity's legal fields — facts and
    knobs through one surface; routing is by role at apply time."""
    e = roster.entities[name]
    fields = roster.fields_of(name)
    key = (type(e).__name__, e.kind, tuple(sorted(fields)))
    cls = _capture_view_classes.get(key)
    if cls is None:
        ns: dict[str, Any] = {"kind": e.kind}
        for f, spec in fields.items():
            ns[f] = _capture_property(f, spec)
        known = ", ".join(sorted(fields))

        def __init__(self, parent: "SuggestionCapture", cname: str) -> None:
            object.__setattr__(self, "name", cname)
            object.__setattr__(self, "_parent", parent)

        def __setattr__(self, attr: str, value) -> None:
            # A typo'd field in an update() must fail LOUDLY.
            if isinstance(getattr(type(self), attr, None), property):
                object.__setattr__(self, attr, value)
                return
            raise AttributeError(
                f"{self.kind} entity {self.name!r} has no field {attr!r} — "
                f"its fields: {known or '(none)'}")

        ns["__init__"] = __init__
        ns["__setattr__"] = __setattr__
        base = EntityView
        # Composites mirror the live surface EXACTLY — same base class (the
        # isinstance-routing contract), same generic pair, same fact refusal
        # — so pair update() code is oblivious to capture vs live.
        if isinstance(e, Composite):
            base = CompositeView

            def _knob_or_monitor(self, f):
                spec = self._parent.roster.spec(self.name, f)
                if spec.role == "fact":
                    raise KeyError(
                        f"{self.name}.{f} is a fact — write it attribute-"
                        f"style (view.{f} = ...); the knob surface refuses "
                        f"facts, exactly like the live device")
                return spec

            def read_knob(self, f):
                _knob_or_monitor(self, f)
                return self._parent._current(self.name, f)

            def write_knob(self, f, v):
                _knob_or_monitor(self, f)
                self._parent._suggest(self.name, f, v)

            ns["read_knob"] = read_knob
            ns["write_knob"] = write_knob
        cls = type(f"Capture_{e.kind}_View", (base,), ns)
        _capture_view_classes[key] = cls
    return cls


class SuggestionCapture:
    """Shadow device swapped in around ``update()``: reads pass through,
    writes become :class:`Suggestion` entries. Never touches a vendor,
    never persists."""

    def __init__(self, device, physical: Store, roster: Roster) -> None:
        self._device = device
        self._physical = physical
        self.roster = roster
        self.suggestions: list[Suggestion] = []

    def component(self, name: str) -> EntityView:
        if name not in self.roster:
            raise KeyError(
                f"unknown entity {name!r} — the roster has: "
                f"{', '.join(sorted(self.roster.entities))}")
        return _capture_view_class(self.roster, name)(self, name)

    def channel(self, target: str, kind: str) -> EntityView:
        """Default-channel addressing, identical surface to the live device
        so update() is oblivious to capture vs live."""
        return self.component(self.roster.default_channel(target, kind))

    def resonator_of(self, target: str) -> str:
        """Topology passthrough, identical surface to the live device."""
        return self._device.resonator_of(target)

    def snapshot(self) -> dict:
        return self._device.snapshot()

    def save(self) -> None:
        """No-op: update() proposes; only an accept persists anything."""

    def _current(self, name: str, field: str):
        spec = self.roster.spec(name, field)
        if spec.role == "fact":
            return self._physical.get(name, field)
        try:
            view = self._device.component(name)
            if isinstance(view, CompositeView):
                return view.read_knob(field)
            return getattr(view, field)
        except KeyError:
            return None

    def _suggest(self, name: str, field: str, value) -> None:
        spec = self.roster.spec(name, field)
        # Shape/finiteness validation like the store the accept will write
        # to — but relations=False: a proposal's partner (a waveform's dt)
        # may itself only be proposed; the accept re-checks relations when
        # it actually records.
        value = (self._physical if spec.role == "fact"
                 else self._device._store).check(name, field, value,
                                                 relations=False)
        self.suggestions.append(Suggestion(
            entity=name, field=field, role=spec.role,
            kind=self.roster.entities[name].kind, unit=spec.unit,
            before=self._current(name, field), after=value,
        ))
