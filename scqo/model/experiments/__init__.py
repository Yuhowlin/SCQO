"""Greenfield experiment registry — the AI's menu of measurements.

Transitional twin of :mod:`scqo.registry`: the greenfield ports register
here so both stacks coexist until the final cutover deletes the old one and
this package moves to ``scqo.experiments``. Same contract: ``@register`` a
subclass, ``get(name)`` for dispatch, ``catalog()`` for the decision menu.
Derived capability tags arrive with the capability-module port.
"""

from __future__ import annotations

from ..experiment import Experiment

_REGISTRY: dict[str, type[Experiment]] = {}


def register(cls: type[Experiment]) -> type[Experiment]:
    _REGISTRY[cls.name] = cls
    return cls


def get(name: str) -> type[Experiment]:
    try:
        return _REGISTRY[name]
    except KeyError:
        raise KeyError(
            f"unknown experiment {name!r} — available: "
            f"{', '.join(sorted(_REGISTRY))}") from None


def catalog() -> list[dict]:
    """Registered measurements with their JSON parameter schemas."""
    return [
        {
            "name": cls.name,
            "description": cls.description,
            "target_kinds": list(cls.target_kinds),
            "required_operations": list(cls.required_operations),
            "parameters_schema": cls.Parameters.model_json_schema(),
        }
        for cls in sorted(_REGISTRY.values(), key=lambda c: c.name)
    ]


from . import resonator_spectroscopy  # noqa: E402,F401  (registration import)
