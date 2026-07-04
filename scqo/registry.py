"""Experiment registry — the catalog an AI agent chooses from.

A driver registers its concrete experiments at import time::

    from scqo import register
    @register
    class QbloxResonatorSpectroscopy(ResonatorSpectroscopy):
        def probe(self): ...

``catalog()`` then returns a JSON-friendly menu (name + description + parameter
schema) — the agent's list of available measurement approaches.

Drivers do not need the consumer to import their experiments package by hand: each
advertises it under the ``scqo.experiments`` entry-point group, and ``catalog()``/``get()``
discover and import them on first use (which runs their ``@register`` decorators).
"""

from __future__ import annotations

from importlib.metadata import entry_points

from .experiment import Experiment

_REGISTRY: dict[str, type[Experiment]] = {}
_MATURITY: dict[str, str] = {}
#: Entry-point groups, in load order. Core drivers register under the first;
#: unpromoted sandbox experiments (the scqo-contrib repo) under the second, and their
#: catalog entries are tagged "contrib" so humans, GUIs and AI loops can tell them apart.
_GROUPS = (("scqo.experiments", "core"), ("scqo.experiments.contrib", "contrib"))
_discovered = False
_loading_maturity = "core"  # maturity stamped on registrations from the group being loaded


def _discover() -> None:
    """Import every installed driver's experiments so the catalog is complete.

    Each driver advertises its experiments package under an entry-point group in
    ``_GROUPS``; loading it runs that package's ``@register`` decorators. Idempotent,
    and tolerant of a backend that fails to import (e.g. its vendor library is absent) — the
    offending backend is simply skipped rather than breaking discovery for the rest.
    Core loads before contrib, so a contrib experiment shadowing a core name wins in the
    registry but stays visibly tagged "contrib".
    """
    global _discovered, _loading_maturity
    if _discovered:
        return
    _discovered = True
    for group, maturity in _GROUPS:
        _loading_maturity = maturity
        try:
            for ep in entry_points(group=group):
                try:
                    ep.load()
                except Exception:
                    continue
        finally:
            _loading_maturity = "core"


def register(cls: type[Experiment]) -> type[Experiment]:
    """Class decorator: add a concrete experiment to the catalog (keyed by ``cls.name``).

    Maturity: registrations that happen while the contrib entry-point group is being
    loaded are tagged ``"contrib"`` automatically; everything else is ``"core"``. A class
    may also declare ``maturity = "contrib"`` explicitly (e.g. when imported by hand in a
    notebook during prototyping).
    """
    if not getattr(cls, "name", None):
        raise ValueError(f"{cls.__name__} must define a class-level `name` to be registered.")
    _REGISTRY[cls.name] = cls
    _MATURITY[cls.name] = getattr(cls, "maturity", None) or _loading_maturity
    return cls


def get(name: str) -> type[Experiment]:
    """Look up a registered experiment class by name."""
    _discover()
    try:
        return _REGISTRY[name]
    except KeyError:
        raise KeyError(f"Unknown experiment {name!r}. Available: {sorted(_REGISTRY)}") from None


def catalog() -> list[dict]:
    """Return ``[{name, description, maturity, parameters_schema}, ...]`` for every
    registered experiment. ``maturity`` is ``"core"`` (promoted, governed) or
    ``"contrib"`` (sandbox prototype — an AI loop should avoid these unless told)."""
    _discover()
    return [
        {
            "name": cls.name,
            "description": cls.description,
            "maturity": _MATURITY.get(cls.name, "core"),
            "parameters_schema": cls.Parameters.model_json_schema(),
        }
        for cls in _REGISTRY.values()
    ]
