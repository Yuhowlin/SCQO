"""Greenfield experiment registry — the AI's menu of measurements.

Transitional twin of :mod:`scqo.registry`: the greenfield ports register
here so both stacks coexist until the final cutover deletes the old one and
this package moves to ``scqo.experiments``. Same contract: ``@register`` a
subclass, ``get(name)`` for dispatch, ``catalog()`` for the decision menu.
Pending until the final cutover (tracked, not lost): derived capability
tags (from the mixin subclass relations), the ``maturity`` field, and the
contrib entry-point group merge.
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


from . import (  # noqa: E402,F401  (registration imports)
    pair_zz_coupler,
    qubit_drag_alternating,
    qubit_drag_equator,
    qubit_echo,
    qubit_echo_flux,
    qubit_pi_pulse_error,
    qubit_power_rabi,
    qubit_ramsey,
    qubit_relaxation,
    qubit_relaxation_flux,
    qubit_spectroscopy,
    qubit_spectroscopy_flux_pulse,
    qubit_sqrb,
    qubit_tomography,
    readout_frequency,
    readout_power,
    resonator_spectroscopy,
    resonator_spectroscopy_flux,
    resonator_spectroscopy_power_amp,
    resonator_spectroscopy_power_chain,
    single_shot_readout,
)
