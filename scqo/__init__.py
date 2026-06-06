"""scqo — Superconducting Qubit Orchestration.

An instrument-agnostic experiment API. The user (or an AI agent) works only with
*protocols* and *parameters*; concrete instrument drivers (LCHQMDriver for
Quantum Machines, LCHQBDriver for Qblox) plug in as backends.

Public surface::

    from scqo import Session, Protocol, Parameters, Result, register, catalog
"""

from .parameters import AveragingParameters, Parameters, QubitSelection
from .result import Outcome, Result
from .device import DeviceModel, QubitView
from .backend import Backend
from .protocol import Protocol
from .registry import catalog, get, register
from .session import Session

__all__ = [
    "Parameters",
    "QubitSelection",
    "AveragingParameters",
    "Result",
    "Outcome",
    "DeviceModel",
    "QubitView",
    "Backend",
    "Protocol",
    "register",
    "get",
    "catalog",
    "Session",
]
