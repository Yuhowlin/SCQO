"""Capability modules: shared Parameters mixins + helpers behind the derived catalog tags.

One capability = one module: the canonical Parameters mixin, the contract
fragment, and the simulate/estimate helpers that every carrier uses instead of
copy-pasting. The registry derives an experiment's ``tags`` from which mixins its
Parameters subclass — a tag can therefore never lie or rot, and experiments with
no tags are legitimate (a new experiment may not be classifiable yet).
"""

from .flux import (
    FLUX_AXIS,
    MAX_FLUX_DESC,
    MIN_FLUX_DESC,
    NUM_FLUX_DESC,
    FluxComponentParameters,
    FluxSweepParameters,
    flux_sweep,
    foreign_flux_source,
)
from .qubit_reset import (
    ACTIVE_RESET_DEPLETION_DESC,
    ACTIVE_RESET_ROUNDS_DESC,
    RESET_METHOD_DESC,
    THERMALIZATION_TIME_DESC,
    QubitResetParameters,
    reset_wait_ns,
)
from .state_readout import (
    STATE_ALT,
    StateReadoutParameters,
    readout_vars,
    signal_rename,
    state_row,
)

__all__ = [
    "ACTIVE_RESET_DEPLETION_DESC",
    "ACTIVE_RESET_ROUNDS_DESC",
    "FLUX_AXIS",
    "MAX_FLUX_DESC",
    "MIN_FLUX_DESC",
    "NUM_FLUX_DESC",
    "RESET_METHOD_DESC",
    "STATE_ALT",
    "THERMALIZATION_TIME_DESC",
    "FluxComponentParameters",
    "FluxSweepParameters",
    "QubitResetParameters",
    "StateReadoutParameters",
    "flux_sweep",
    "foreign_flux_source",
    "readout_vars",
    "reset_wait_ns",
    "signal_rename",
    "state_row",
]
