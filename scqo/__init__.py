"""scqo — Superconducting Qubit Orchestration.

An instrument-agnostic experiment API. The user (or an AI agent) works only with
*experiments* and *parameters*; concrete instrument drivers (LCHQMDriver for
Quantum Machines, LCHQBDriver for Qblox) plug in as backends.

Public surface::

    from scqo import Session, Experiment, Parameters, Result, register, catalog
"""

from .parameters import AveragingParameters, Parameters, QubitSelection
from .result import Outcome, Result
from .contract import ContractError, DatasetContract
from .config import ChangeRecord, RecordingDevice
from .device import DeviceModel, QubitView
from .backend import Backend
from .experiment import Experiment
from .registry import catalog, get, register
from .session import Session

__all__ = [
    "Parameters",
    "QubitSelection",
    "AveragingParameters",
    "Result",
    "Outcome",
    "DatasetContract",
    "ContractError",
    "ChangeRecord",
    "RecordingDevice",
    "DeviceModel",
    "QubitView",
    "Backend",
    "Experiment",
    "register",
    "get",
    "catalog",
    "Session",
]
