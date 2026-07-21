"""Device model — per-category component views over a vendor device.

A backend's :class:`DeviceModel` serves one :class:`ComponentView` per component
NAME; the view's property surface is the component's INSTRUMENT category's
pushed fields (physical fields never have vendor realizations — they exist only
in the SCQO stores). The category catalog (:mod:`scqo.categories`) is the single
schema source; the per-device roster (:mod:`scqo.roster`) decides which names
exist and what they are.

Drivers subclass :func:`make_view_base` per category they realize::

    class QbloxReadableTransmon(make_view_base("ReadableTransmon")):
        # implement the generated abstract property pairs (readout_freq, ...)

The AUTHORITATIVE per-backend field catalog (vendor paths, units, conversion
descriptions) is each driver's ``fieldmap`` module — view it with
``scqo state --fields``. This keeps experiment physics free of any vendor
attribute path.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field as _field
from functools import lru_cache

from .categories import CATEGORIES


class ComponentView(ABC):
    """Backend-agnostic accessor for ONE component's vendor-realized fields.

    Concrete per-category bases are produced by :func:`make_view_base`; drivers
    implement the generated abstract properties against their native tree.
    """

    name: str
    category: str


@dataclass(frozen=True)
class ComponentInfo:
    """One entry of a driver's derived inventory (the doctor's witness)."""

    category: str
    operations: tuple[str, ...] = ()
    members: dict[str, str] = _field(default_factory=dict)


def _abstract_pair(field: str, doc: str) -> property:
    def getter(self):  # pragma: no cover - abstract surface
        raise NotImplementedError(field)

    def setter(self, value):  # pragma: no cover - abstract surface
        raise NotImplementedError(field)

    getter.__isabstractmethod__ = True  # type: ignore[attr-defined]
    setter.__isabstractmethod__ = True  # type: ignore[attr-defined]
    # property.__isabstractmethod__ is read-only, COMPUTED from the accessors
    # flagged above — do not assign it (AttributeError on CPython).
    return property(getter, setter, doc=doc)


@lru_cache(maxsize=None)
def make_view_base(category: str) -> type[ComponentView]:
    """The driver-facing abstract base for one INSTRUMENT category.

    Declares one abstract read/write property per PUSHED field of the category
    — and ONLY pushed fields: record-only monitors (readout_fidelity) and all
    physical fields have no vendor knob and must not burden driver classes.
    ``requires_physical``-gated fields (idle_flux_v) ARE declared on the base;
    a driver for a device that never realizes them declares them Unrealized in
    its fieldmap instead of implementing them.
    """
    spec = CATEGORIES[category]
    assert spec.side == "instrument", f"{category}: driver views are instrument-side"
    ns: dict = {"category": category, "__doc__": f"Driver view base for {category}."}
    for f, fs in spec.fields.items():
        if fs.push:
            ns[f] = _abstract_pair(f, f"{fs.doc} [{fs.unit}]" if fs.unit else fs.doc)
    return type(f"{category}ViewBase", (ComponentView,), ns)


class DeviceModel(ABC):
    """Container of :class:`ComponentView` objects plus persistence."""

    @abstractmethod
    def component(self, name: str) -> ComponentView:
        """The view for one vendor-realized component by name. Raises KeyError
        for names the vendor does not realize (bare resonators, interaction
        terms — those are SCQO-store-only)."""

    @abstractmethod
    def save(self) -> None:
        """Persist current device state (e.g. to JSON)."""

    @abstractmethod
    def snapshot(self) -> dict:
        """JSON-serialisable ``{name: {field: value}}`` of vendor-realized state."""

    def components(self) -> dict[str, ComponentInfo]:
        """The driver's DERIVED inventory (name -> category/operations) read
        from the vendor tree — a WITNESS the doctor cross-checks against the
        authoritative roster, never the source of truth. Default: every
        snapshot name is a ReadableTransmon with the standard operations."""
        return {n: ComponentInfo("ReadableTransmon", operations=("rx", "readout"))
                for n in self.snapshot()}
