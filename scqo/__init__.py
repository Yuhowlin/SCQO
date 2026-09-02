"""scqo — Superconducting Qubit Orchestration.

An instrument-agnostic experiment API. The user (or an AI agent) works only
with *experiments* and *parameters*; concrete instrument drivers
(LCHQMDriver for Quantum Machines, LCHQBDriver for Qblox) plug in as
backends.

The device is described by the schema in ``docs/greenfield-schema.md``:
**modes** (quantum degrees of freedom), **composites** (named mode groups
with joint physics), **lines** (physical control paths) and **channels**
(one signal on a line). Field routing is per-field by role — facts to
``physical.json``, knobs and monitors to ``scqo_state.json``.

Public surface::

    from scqo import Session, Experiment, Parameters, Result, register, catalog
"""

from .parameters import AveragingParameters, Parameters, TargetSelection
from .result import Outcome, Result
from .contract import ContractError, DatasetContract
from .catalog import (
    CHANNELS,
    COMPOSITES,
    MODES,
    ChannelKind,
    CompositeKind,
    FieldSpec,
    ModeKind,
    derived_op,
)
from .entities import Channel, Composite, Entity, Line, Mode, Provenance
from .roster import Roster, RosterError, load_components, parse_components
from .design import (
    Design,
    DesignError,
    load_design,
    parse_design,
    seed_anchor,
    seed_value,
)
from .stores import (
    ChangeRecord,
    Store,
    StoreError,
    physical_store,
    state_store,
)
from .device import (
    ComponentInfo,
    CompositeView,
    DeviceModel,
    EntityView,
    RecordingDevice,
    make_view_base,
)
from .suggestions import Suggestion, SuggestionCapture
from .lock import LockError, freeze, verify
from .checks import Check, all_checks
from .backend import Backend
from .experiment import Experiment
from .experiments import catalog, get, register
from .campaign import CampaignPlan, CampaignStep, load_campaign_plan
from .campaign_query import get_latest_metric_stat, query_campaign_statistics
from .datastore import DataStore, RunRecord, reindex
from .labconfig import LabConfig, load as load_lab_config, make_session
from .session import Session

__all__ = [
    # parameters / results / contracts
    "Parameters", "TargetSelection", "AveragingParameters",
    "Result", "Outcome", "DatasetContract", "ContractError",
    # the device model
    "MODES", "COMPOSITES", "CHANNELS", "ModeKind", "CompositeKind",
    "ChannelKind", "FieldSpec", "derived_op",
    "Entity", "Mode", "Composite", "Line", "Channel", "Provenance",
    "Roster", "RosterError", "load_components", "parse_components",
    "Design", "DesignError", "load_design", "parse_design",
    "seed_anchor", "seed_value",
    # stores + device
    "Store", "StoreError", "ChangeRecord", "physical_store", "state_store",
    "DeviceModel", "EntityView", "CompositeView", "ComponentInfo",
    "RecordingDevice", "make_view_base",
    "Suggestion", "SuggestionCapture",
    # operations
    "freeze", "verify", "LockError", "Check", "all_checks",
    # orchestration
    "Backend", "Experiment", "register", "get", "catalog",
    "CampaignPlan", "CampaignStep", "load_campaign_plan",
    "query_campaign_statistics", "get_latest_metric_stat",
    "DataStore", "RunRecord", "reindex",
    "LabConfig", "load_lab_config", "make_session", "Session",
]
