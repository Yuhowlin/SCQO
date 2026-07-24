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
from .state_readout import (
    STATE_ALT,
    StateReadoutParameters,
    readout_vars,
    signal_rename,
    state_row,
)

__all__ = [
    "FLUX_AXIS",
    "MAX_FLUX_DESC",
    "MIN_FLUX_DESC",
    "NUM_FLUX_DESC",
    "STATE_ALT",
    "FluxComponentParameters",
    "FluxSweepParameters",
    "StateReadoutParameters",
    "flux_sweep",
    "foreign_flux_source",
    "readout_vars",
    "signal_rename",
    "state_row",
]
