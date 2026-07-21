"""Backend-free physics experiments.

Each module here defines an experiment's Parameters, Result, sweep, simulator, analysis
and device writeback — everything *except* ``probe()``. Concrete drivers subclass
these and implement ``probe()`` for their instrument, then ``@register`` the subclass.
"""

from .pair_zz_coupler import PairZZCoupler, PairZZCouplerParameters, PairZZCouplerResult
from .qubit_power_rabi import QubitPowerRabi, QubitPowerRabiParameters, QubitPowerRabiResult
from .qubit_ramsey import QubitRamsey, QubitRamseyParameters, QubitRamseyResult
from .qubit_spectroscopy import (
    QubitSpectroscopy,
    QubitSpectroscopyParameters,
    QubitSpectroscopyResult,
)
from .resonator_spectroscopy_power_amp import (
    ResonatorSpectroscopyPowerAmp,
    ResonatorSpectroscopyPowerAmpParameters,
    ResonatorSpectroscopyPowerAmpResult,
)
from .resonator_spectroscopy_power_chain import (
    ResonatorSpectroscopyPowerChain,
    ResonatorSpectroscopyPowerChainParameters,
    ResonatorSpectroscopyPowerChainResult,
)
from .qubit_relaxation import QubitRelaxation, QubitRelaxationParameters, QubitRelaxationResult
from .qubit_echo import QubitEcho, QubitEchoParameters, QubitEchoResult
from .qubit_spectroscopy_flux import (
    QubitSpectroscopyFlux,
    QubitSpectroscopyFluxParameters,
    QubitSpectroscopyFluxResult,
)
from .single_shot_readout import (
    SingleShotReadout,
    SingleShotReadoutParameters,
    SingleShotReadoutResult,
)
from .resonator_spectroscopy_flux import (
    ResonatorSpectroscopyFlux,
    ResonatorSpectroscopyFluxParameters,
    ResonatorSpectroscopyFluxResult,
)
from .readout_power import ReadoutPower, ReadoutPowerParameters, ReadoutPowerResult
from .readout_frequency import (
    ReadoutFrequency,
    ReadoutFrequencyParameters,
    ReadoutFrequencyResult,
)
from .resonator_spectroscopy import (
    ResonatorSpectroscopy,
    ResonatorSpectroscopyParameters,
    ResonatorSpectroscopyResult,
)

__all__ = [
    "PairZZCoupler",
    "PairZZCouplerParameters",
    "PairZZCouplerResult",
    "ResonatorSpectroscopy",
    "ResonatorSpectroscopyParameters",
    "ResonatorSpectroscopyResult",
    "ResonatorSpectroscopyPowerAmp",
    "ResonatorSpectroscopyPowerAmpParameters",
    "ResonatorSpectroscopyPowerAmpResult",
    "ResonatorSpectroscopyPowerChain",
    "ResonatorSpectroscopyPowerChainParameters",
    "ResonatorSpectroscopyPowerChainResult",
    "QubitRelaxation",
    "QubitRelaxationParameters",
    "QubitRelaxationResult",
    "QubitEcho",
    "QubitEchoParameters",
    "QubitEchoResult",
    "QubitSpectroscopyFlux",
    "QubitSpectroscopyFluxParameters",
    "QubitSpectroscopyFluxResult",
    "SingleShotReadout",
    "SingleShotReadoutParameters",
    "SingleShotReadoutResult",
    "ResonatorSpectroscopyFlux",
    "ResonatorSpectroscopyFluxParameters",
    "ResonatorSpectroscopyFluxResult",
    "ReadoutPower",
    "ReadoutPowerParameters",
    "ReadoutPowerResult",
    "ReadoutFrequency",
    "ReadoutFrequencyParameters",
    "ReadoutFrequencyResult",
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
