"""Backend-free physics experiments.

Each module here defines an experiment's Parameters, Result, sweep, simulator, analysis
and device writeback — everything *except* ``probe()``. Concrete drivers subclass
these and implement ``probe()`` for their instrument, then ``@register`` the subclass.
"""

from .qubit_power_rabi import QubitPowerRabi, QubitPowerRabiParameters, QubitPowerRabiResult
from .qubit_ramsey import QubitRamsey, QubitRamseyParameters, QubitRamseyResult
from .qubit_spectroscopy import (
    QubitSpectroscopy,
    QubitSpectroscopyParameters,
    QubitSpectroscopyResult,
)
from .resonator_spectroscopy_power import (
    ResonatorSpectroscopyPower,
    ResonatorSpectroscopyPowerParameters,
    ResonatorSpectroscopyPowerResult,
)
from .t1_relaxation import T1Relaxation, T1RelaxationParameters, T1RelaxationResult
from .resonator_spectroscopy import (
    ResonatorSpectroscopy,
    ResonatorSpectroscopyParameters,
    ResonatorSpectroscopyResult,
)

__all__ = [
    "ResonatorSpectroscopy",
    "ResonatorSpectroscopyParameters",
    "ResonatorSpectroscopyResult",
    "ResonatorSpectroscopyPower",
    "ResonatorSpectroscopyPowerParameters",
    "ResonatorSpectroscopyPowerResult",
    "T1Relaxation",
    "T1RelaxationParameters",
    "T1RelaxationResult",
    "QubitSpectroscopy",
    "QubitSpectroscopyParameters",
    "QubitSpectroscopyResult",
    "QubitRamsey",
    "QubitRamseyParameters",
    "QubitRamseyResult",
    "QubitPowerRabi",
    "QubitPowerRabiParameters",
    "QubitPowerRabiResult",
]
