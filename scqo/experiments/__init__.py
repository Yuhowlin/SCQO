"""Experiment registry — the catalog an AI agent chooses from.

Core experiments are backend-free: their ``probe()`` raises, and each
registers itself at import so the catalog (and ``--help``) is complete with
no driver installed — the simulated backend never calls ``probe``. A driver
registers its concrete subclass under the SAME name, replacing the core
entry with one that can talk to its instrument::

    from scqo import register
    @register
    class QbloxResonatorSpectroscopy(ResonatorSpectroscopy):
        def probe(self): ...

Drivers do not need the consumer to import their package by hand: each
advertises it under the ``scqo.experiments`` entry-point group (sandbox
prototypes under ``scqo.experiments.contrib``), and ``catalog()``/``get()``
discover and import them on first use.
"""

from __future__ import annotations

from importlib.metadata import entry_points

from ..experiment import Experiment

_REGISTRY: dict[str, type[Experiment]] = {}
_MATURITY: dict[str, str] = {}
#: Entry-point groups in load order. Core drivers register under the first;
#: unpromoted sandbox experiments (scqo-contrib) under the second, and their
#: catalog entries are tagged "contrib" so humans, GUIs and AI loops can tell
#: them apart.
_GROUPS = (("scqo.experiments", "core"),
           ("scqo.experiments.contrib", "contrib"))
_discovered = False
_loading_maturity = "core"


def _discover() -> None:
    """Import every installed driver's experiments so the catalog is complete.

    Idempotent, and tolerant of a backend that fails to import (its vendor
    library may be absent) — that backend is skipped rather than breaking
    discovery for the rest. Core loads before contrib, so a contrib
    experiment shadowing a core name wins in the registry but stays visibly
    tagged "contrib".
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
    """Class decorator: add an experiment to the catalog (keyed by
    ``cls.name``). Registrations made while the contrib group is loading are
    tagged ``"contrib"``; a class may also declare ``maturity`` explicitly."""
    if not getattr(cls, "name", None):
        raise ValueError(
            f"{cls.__name__} must define a class-level `name` to be registered.")
    _REGISTRY[cls.name] = cls
    _MATURITY[cls.name] = getattr(cls, "maturity", None) or _loading_maturity
    return cls


def get(name: str) -> type[Experiment]:
    """Look up a registered experiment class by name."""
    _discover()
    try:
        return _REGISTRY[name]
    except KeyError:
        raise KeyError(f"Unknown experiment {name!r}. "
                       f"Available: {sorted(_REGISTRY)}") from None


def _derived_tags(cls: type[Experiment]) -> list[str]:
    """Capability tags DERIVED from Parameters-mixin subclassing — never a
    declared string, so a tag cannot lie or rot as the code evolves.
    Experiments with no tags are legitimate (a new experiment may not be
    classifiable yet)."""
    from ._capabilities import (
        AmplitudeSweepParameters,
        FluxPulseSweepParameters,
        FluxSweepParameters,
        QubitResetParameters,
        StateReadoutParameters,
    )

    # Derivation order is fixed (tests pin the exact lists): the two original
    # capabilities first, then each later addition appended at the END so it
    # does not reshuffle every existing entry — qubit_reset, then flux_pulse,
    # then amplitude.
    tags = []
    if issubclass(cls.Parameters, StateReadoutParameters):
        tags.append("state_readout")
    if issubclass(cls.Parameters, FluxSweepParameters):
        tags.append("flux")
    if issubclass(cls.Parameters, QubitResetParameters):
        tags.append("qubit_reset")
    # A REFINEMENT of "flux", not a sibling: the window is measured from the
    # channel's idle_flux rather than from the DAC zero. Carriers must also end
    # their name in "_pulse" (test_capabilities pins it).
    if issubclass(cls.Parameters, FluxPulseSweepParameters):
        tags.append("flux_pulse")
    # the swept amplitude window (a FACTOR of the target's standing amplitude);
    # carriers also attach the absolute `digital_amp` axis
    if issubclass(cls.Parameters, AmplitudeSweepParameters):
        tags.append("amplitude")
    return tags


def catalog() -> list[dict]:
    """``[{name, description, maturity, tags, target_kinds,
    required_operations, parameters_schema}, ...]`` for every registered
    experiment. ``maturity`` is ``"core"`` (promoted, governed) or
    ``"contrib"`` (sandbox prototype — an AI loop should avoid these unless
    told); ``tags`` are derived capability markers."""
    _discover()
    return [
        {
            "name": cls.name,
            "description": cls.description,
            "maturity": _MATURITY.get(cls.name, "core"),
            "tags": _derived_tags(cls),
            "target_kinds": list(cls.target_kinds),
            "required_operations": list(cls.required_operations),
            "parameters_schema": cls.Parameters.model_json_schema(),
        }
        for cls in sorted(_REGISTRY.values(), key=lambda c: c.name)
    ]


# Importing the modules runs their @register decorators; re-exporting the
# classes is the driver-facing API (`from scqo.experiments import
# ResonatorSpectroscopy` to subclass with a probe).
#: the driver-facing slice of the capability package: a probe resolves its
#: thermal-reset wait through reset_wait_ns rather than reading the knob or the
#: override itself (the precedence rule has exactly one home).
from ._capabilities import (  # noqa: E402
    QubitResetParameters,
    reset_wait_ns,
)
from .pair_swap_chevron import PairSwapChevron  # noqa: E402
from .pair_swap_flux_map import PairSwapFluxMap  # noqa: E402
from .pair_zz_coupler import PairZZCoupler  # noqa: E402
from .qubit_ramsey_cryoscope import QubitRamseyCryoscope  # noqa: E402
from .qubit_deterministic_benchmarking import QubitDeterministicBenchmarking  # noqa: E402
from .qubit_drag_alternating import QubitDragAlternating  # noqa: E402
from .qubit_drag_equator import QubitDragEquator  # noqa: E402
from .qubit_echo import QubitEcho  # noqa: E402
from .qubit_echo_flux_pulse import QubitEchoFluxPulse  # noqa: E402
from .qubit_parity_switch import QubitParitySwitch  # noqa: E402
from .qubit_pi_pulse_error import QubitPiPulseError  # noqa: E402
from .qubit_power_rabi import QubitPowerRabi  # noqa: E402
from .qubit_ramsey import QubitRamsey  # noqa: E402
from .qubit_relaxation import QubitRelaxation  # noqa: E402
from .qubit_relaxation_flux_pulse import QubitRelaxationFluxPulse  # noqa: E402
from .qubit_spectroscopy import QubitSpectroscopy  # noqa: E402
from .qubit_spectroscopy_flux_pulse import (  # noqa: E402
    QubitSpectroscopyFluxPulse,
)
from .qubit_spectroscopy_overlap import QubitSpectroscopyOverlap  # noqa: E402
from .qubit_sqrb import QubitSQRB  # noqa: E402
from .qubit_thermal_population import QubitThermalPopulation  # noqa: E402
from .qubit_tomography import QubitTomography  # noqa: E402
from .qubit_xyz_delay import QubitXyzDelay  # noqa: E402
from .readout_frequency import ReadoutFrequency  # noqa: E402
from .readout_power import ReadoutPower  # noqa: E402
from .resonator_spectroscopy import ResonatorSpectroscopy  # noqa: E402
from .resonator_spectroscopy_flux import ResonatorSpectroscopyFlux  # noqa: E402
from .resonator_spectroscopy_power_amp import (  # noqa: E402
    ResonatorSpectroscopyPowerAmp,
)
from .resonator_spectroscopy_power_chain import (  # noqa: E402
    ResonatorSpectroscopyPowerChain,
)
from .single_shot_readout import SingleShotReadout  # noqa: E402
from .single_shot_readout_gef import SingleShotReadoutGEF  # noqa: E402

__all__ = [
    "catalog", "get", "register",
    "QubitResetParameters", "reset_wait_ns",
    "PairSwapChevron", "PairSwapFluxMap",
    "PairZZCoupler", "QubitRamseyCryoscope", "QubitDeterministicBenchmarking", "QubitDragAlternating", "QubitDragEquator", "QubitEcho",
    "QubitEchoFluxPulse", "QubitParitySwitch", "QubitPiPulseError", "QubitPowerRabi", "QubitRamsey",
    "QubitRelaxation", "QubitRelaxationFluxPulse", "QubitSQRB",
    "QubitSpectroscopy", "QubitSpectroscopyFluxPulse", "QubitSpectroscopyOverlap",
    "QubitThermalPopulation", "QubitTomography", "QubitXyzDelay",
    "ReadoutFrequency", "ReadoutPower", "ResonatorSpectroscopy",
    "ResonatorSpectroscopyFlux", "ResonatorSpectroscopyPowerAmp",
    "ResonatorSpectroscopyPowerChain", "SingleShotReadout", "SingleShotReadoutGEF",
]

