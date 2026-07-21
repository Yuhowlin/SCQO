"""Suggested updates — captured from ``update()``, decided by a human.

An experiment's ``update()`` never writes directly: ``Session.run`` swaps a
:class:`SuggestionCapture` shadow device in around the call, so every
``self.device.component(name).<field> = value`` becomes a :class:`Suggestion` —
component, field, owning store/category, before/after — instead of a live write.
The captured list is stored on the run record (``record.json`` — the truth) with
per-item status:

* ``pending``  — proposed, nothing applied (the default fate of every suggestion);
* ``accepted`` — applied through the real stores (vendor-push-first for calibration
  knobs, :class:`~scqo.physical.PhysicalStore` for sample physics) with ChangeRecords
  stamped to the ORIGINATING run_id;
* ``rejected`` — explicitly declined (metadata only, no instrument needed).

Routing is per-component: the ROSTER decides which fields a name carries and
which store each belongs to (``roster.resolve``). Cross-component proposals are
first-class — a resonator fit captured while targeting ``q1`` lands on
``q1_res`` and is reviewed under that name. Unknown fields raise loudly with a
category-aware message.

Suggestions have two **origins**: ``estimator`` (captured from ``update()``, the
default) and ``operator`` (attached via ``Session.suggest`` / ``scqo suggest``).
"""

from __future__ import annotations

import math
from typing import Any, Literal

from pydantic import BaseModel

from . import config as _config
from .config import _now
from .device import ComponentView, DeviceModel
from .physical import PhysicalStore


class Suggestion(BaseModel):
    """One proposed field update, as stored on the run record."""

    component: str
    field: str
    #: which store owns the field: "instrument" (pushed/record-only calibration)
    #: or "physical" (sample physics — never touches a vendor).
    store: Literal["instrument", "physical"]
    #: the field-owning category, stamped at capture (self-describing rows).
    category: str | None = None
    #: the field's unit, stamped at capture (review tables need no catalog).
    unit: str = ""
    #: value at capture time (None = not measured before); the staleness guard
    #: compares this against the store's CURRENT value at accept time.
    before: float | None
    after: float  # capture refuses NaN/Inf, so this is always finite
    status: Literal["pending", "accepted", "rejected"] = "pending"
    decided_at: str | None = None  # ISO timestamp of the accept/reject decision
    decided_by: str | None = None  # OS login of whoever decided
    #: the decision comment; a failed apply also notes its error here (status stays
    #: pending so the item remains decidable once the cause is fixed).
    comment: str = ""
    #: who proposed the value: "estimator" (captured from update()) or "operator".
    origin: Literal["estimator", "operator"] = "estimator"
    proposed_by: str | None = None  # OS login of the proposer (operator-authored only)
    proposed_at: str | None = None  # when it was attached (a suggest can be days after the run)


def pending_count(suggestions: list[dict] | list[Suggestion]) -> int:
    """How many suggestions are still undecided (works on models or plain dicts)."""
    return sum(1 for s in suggestions if _get(s, "status") == "pending")


def select_suggestions(
    suggestions: list[Suggestion],
    *,
    components: list[str] | None = None,
    fields: list[str] | None = None,
    indices: list[int] | None = None,
    include_decided: bool = False,
) -> list[int]:
    """0-based positions of the PENDING suggestions matching the filters.

    ``indices`` are 0-based positions into the FULL stored list (the list order is
    stable — records are never reordered); a decided index is simply not selected.
    ``components``/``fields`` narrow the match; no filters = every pending item.
    ``include_decided=True`` (the ``--reapply`` mode) lifts the pending-only rule.
    """
    out = []
    for i, s in enumerate(suggestions):
        if s.status != "pending" and not include_decided:
            continue
        if indices is not None and i not in indices:
            continue
        if components is not None and s.component not in components:
            continue
        if fields is not None and s.field not in fields:
            continue
        out.append(i)
    return out


def _get(s: dict | Suggestion, key: str):
    return s.get(key) if isinstance(s, dict) else getattr(s, key)


def decision_editor(touched: dict[int, dict]):
    """Editor for :meth:`~scqo.datastore.DataStore.edit_suggestions` that replaces
    ONLY the rows a decision touched. The stored list is append-only (suggest
    appends, decisions mutate in place), so positions are stable across writers —
    rows appended or decided by a concurrent session since our load are preserved
    instead of being clobbered by a stale whole-list snapshot."""

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
    components: list[str] | None = None,
    fields: list[str] | None = None,
    indices: list[int] | None = None,
    comment: str = "",
) -> dict:
    """Decline pending suggestions on a saved run — metadata only, no instrument.

    ``store`` is a :class:`~scqo.datastore.DataStore` (duck-typed: ``load_run`` +
    ``edit_suggestions``); rejecting needs no backend. Same selection semantics
    as ``Session.accept``.
    """
    record = store.load_run(run_id)["record"]
    suggestions = [Suggestion(**s) for s in record.get("suggestions", [])]
    selected = select_suggestions(suggestions, components=components, fields=fields,
                                  indices=indices)
    for i in selected:
        s = suggestions[i]
        s.status = "rejected"
        s.decided_at = _now()
        s.decided_by = _config._current_operator() or None
        s.comment = comment
    stored = store.edit_suggestions(
        run_id, decision_editor({i: suggestions[i].model_dump(mode="json") for i in selected})
    )
    return {
        "run_id": run_id,
        "rejected": [
            {"component": suggestions[i].component, "field": suggestions[i].field}
            for i in selected
        ],
        "pending_left": pending_count(stored["suggestions"]),
    }


def _capture_property(field: str, side: str, spec) -> property:
    """A property that reads the real store but turns writes into Suggestions."""

    def getter(self: ComponentView) -> float | None:
        return self._parent._current(self.name, field, side)

    def setter(self: ComponentView, value: float) -> None:
        self._parent._suggest(self.name, field, side, spec, value)

    return property(getter, setter, doc=f"{spec.doc} [{spec.unit}]" if spec.unit else spec.doc)


_capture_view_classes: dict[tuple[str | None, str | None], type] = {}


def _capture_view_class(roster, name: str) -> type:
    """The generated capture-view class for one component's category pair.

    Exposes the UNION of both slots' fields (pruned by requires_physical), so
    ``update()`` addresses calibration knobs and physical parameters through one
    surface; routing is by the declaring side."""
    key = roster.category(name)
    cls = _capture_view_classes.get(key)
    if cls is None:
        fields = roster.fields_of(name)
        ns: dict[str, Any] = {}
        for f, (side, spec) in fields.items():
            ns[f] = _capture_property(f, side, spec)
        known = ", ".join(sorted(fields))
        phys, instr = key

        def __init__(self, parent: "SuggestionCapture", cname: str) -> None:
            object.__setattr__(self, "name", cname)
            object.__setattr__(self, "_parent", parent)

        def __setattr__(self, attr: str, value) -> None:
            # A typo'd field in an update() must fail LOUDLY.
            if isinstance(getattr(type(self), attr, None), property):
                object.__setattr__(self, attr, value)
                return
            cats = " + ".join(x for x in (phys, instr) if x)
            raise AttributeError(
                f"{cats or 'component'} {self.name!r} has no field {attr!r} — "
                f"its fields: {known or '(none)'}")

        ns["__init__"] = __init__
        ns["__setattr__"] = __setattr__
        ns["category"] = instr or phys
        cls = type(f"Capture_{phys or ''}_{instr or ''}_View", (ComponentView,), ns)
        _capture_view_classes[key] = cls
    return cls


class SuggestionCapture(DeviceModel):
    """Shadow device swapped in around ``update()``: reads pass through, writes
    become :class:`Suggestion` entries. Never touches a vendor, never persists."""

    def __init__(self, device: DeviceModel, physical: PhysicalStore, roster) -> None:
        self._device = device
        self._physical = physical
        self.roster = roster
        self.suggestions: list[Suggestion] = []

    def component(self, name: str) -> ComponentView:
        if name not in self.roster:
            raise KeyError(f"unknown component {name!r} — roster has: "
                           f"{', '.join(sorted(self.roster.components))}")
        return _capture_view_class(self.roster, name)(self, name)

    def one(self, name: str, category: str) -> str:
        """Topology passthrough so update() can reach satellites
        (``self.device.one(q, "Resonator")``) identically under capture and live."""
        return self.roster.one(name, category)

    def design(self, name: str, field: str) -> float | None:
        """Design-value passthrough (bring-up anchors read through the device)."""
        return self.roster.design(name, field)

    def snapshot(self) -> dict:
        return self._device.snapshot()

    def save(self) -> None:
        """No-op: update() proposes; only an accept persists anything."""

    def _current(self, name: str, field: str, side: str) -> float | None:
        if side == "physical":
            return self._physical.get(name, field)
        try:
            return getattr(self._device.component(name), field)
        except KeyError:
            return None

    def _suggest(self, name: str, field: str, side: str, spec, value: float) -> None:
        value = float(value)
        if not math.isfinite(value):  # a NaN fit output is not a proposable value
            raise ValueError(f"refusing to suggest non-finite {field}={value!r} for {name}")
        phys, instr = self.roster.category(name)
        self.suggestions.append(
            Suggestion(
                component=name, field=field, store=side,  # type: ignore[arg-type]
                category=phys if side == "physical" else instr,
                unit=spec.unit,
                before=self._current(name, field, side), after=value,
            )
        )
