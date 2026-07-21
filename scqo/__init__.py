"""scqo — Superconducting Qubit Orchestration.

An instrument-agnostic experiment API. The user (or an AI agent) works only with
*experiments* and *parameters*; concrete instrument drivers (LCHQMDriver for
Quantum Machines, LCHQBDriver for Qblox) plug in as backends.

Public surface::

    from scqo import Session, Experiment, Parameters, Result, register, catalog
"""

from .parameters import AveragingParameters, Parameters, TargetSelection
from .result import Outcome, Result
from .contract import ContractError, DatasetContract
from .categories import CATEGORIES, CategorySpec
from .config import ChangeRecord, FieldSpec, RecordingDevice
from .device import ComponentInfo, ComponentView, DeviceModel, make_view_base
from .physical import PhysicalStore
from .roster import Component, Roster, RosterError, load_components
from .suggestions import Suggestion
from .backend import Backend
from .experiment import Experiment
from .registry import catalog, get, register
from .datastore import DataStore, RunRecord, reindex
from .labconfig import LabConfig, load as load_lab_config, make_session
from .session import Session

__all__ = [
    "Parameters",
    "TargetSelection",
    "AveragingParameters",
    "Result",
    "Outcome",
    "DatasetContract",
    "ContractError",
    "CATEGORIES",
    "CategorySpec",
    "FieldSpec",
    "ChangeRecord",
    "RecordingDevice",
    "PhysicalStore",
    "Component",
    "Roster",
    "RosterError",
    "load_components",
    "Suggestion",
    "DeviceModel",
    "ComponentView",
    "ComponentInfo",
    "make_view_base",
    "Backend",
    "Experiment",
    "register",
    "get",
    "catalog",
    "DataStore",
    "RunRecord",
    "reindex",
    "LabConfig",
    "load_lab_config",
    "make_session",
    "Session",
]
