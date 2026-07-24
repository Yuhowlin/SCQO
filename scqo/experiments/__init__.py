"""Backend-free physics experiments.

Each module here defines an experiment's Parameters, Result, sweep, simulator, analysis
and device writeback — everything *except* ``probe()``. Concrete drivers subclass
these and implement ``probe()`` for their instrument, then ``@register`` the subclass.
"""

from ._capabilities import (
    FluxComponentParameters,
    FluxSweepParameters,
    StateReadoutParameters,
)
from .pair_zz_coupler import PairZZCoupler, PairZZCouplerParameters, PairZZCouplerResult
from .qubit_drag_alternating import (
    QubitDragAlternating,
    QubitDragAlternatingParameters,
    QubitDragAlternatingResult,
)
from .qubit_drag_equator import (
    QubitDragEquator,
    QubitDragEquatorParameters,
    QubitDragEquatorResult,
)
from .qubit_echo_flux import QubitEchoFlux, QubitEchoFluxParameters, QubitEchoFluxResult
from .qubit_pi_pulse_error import (
    QubitPiPulseError,
    QubitPiPulseErrorParameters,
    QubitPiPulseErrorResult,
)
from .qubit_power_rabi import QubitPowerRabi, QubitPowerRabiParameters, QubitPowerRabiResult
from .qubit_relaxation_flux import (
    QubitRelaxationFlux,
    QubitRelaxationFluxParameters,
    QubitRelaxationFluxResult,
)
from .qubit_sqrb import QubitSQRB, QubitSQRBParameters, QubitSQRBResult
from .qubit_tomography import QubitTomography, QubitTomographyParameters, QubitTomographyResult
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
from .qubit_spectroscopy_flux_pulse import (
    QubitSpectroscopyFluxPulse,
    QubitSpectroscopyFluxPulseParameters,
    QubitSpectroscopyFluxPulseResult,
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
    "FluxComponentParameters",
    "FluxSweepParameters",
    "StateReadoutParameters",
    "PairZZCoupler",
    "PairZZCouplerParameters",
    "PairZZCouplerResult",
    "QubitSQRB",
    "QubitSQRBParameters",
    "QubitSQRBResult",
    "QubitPiPulseError",
    "QubitPiPulseErrorParameters",
    "QubitPiPulseErrorResult",
    "QubitTomography",
    "QubitTomographyParameters",
    "QubitTomographyResult",
    "QubitDragAlternating",
    "QubitDragAlternatingParameters",
    "QubitDragAlternatingResult",
    "QubitDragEquator",
    "QubitDragEquatorParameters",
    "QubitDragEquatorResult",
    "QubitEchoFlux",
    "QubitEchoFluxParameters",
    "QubitEchoFluxResult",
    "QubitRelaxationFlux",
    "QubitRelaxationFluxParameters",
    "QubitRelaxationFluxResult",
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
    "QubitSpectroscopyFluxPulse",
    "QubitSpectroscopyFluxPulseParameters",
    "QubitSpectroscopyFluxPulseResult",
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
