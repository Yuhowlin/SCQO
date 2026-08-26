"""Capability modules: shared Parameters mixins + helpers behind the catalog's capabilities.

One capability = one module: the canonical Parameters mixin, the contract
fragment, and the simulate/estimate helpers that every carrier uses instead of
copy-pasting. The registry derives an experiment's ``capabilities`` from which
mixins its Parameters subclass — a capability can therefore never lie or rot,
and experiments with no capabilities are legitimate (a new experiment may not be
classifiable yet). Not "tags": that word is the datastore's (user-attached run
tags), and the two must never share a name.
"""

from .amplitude import (
    ABS_AMP_COORD,
    ABS_AMP_LABEL,
    AMP_AXIS,
    MAX_AMP_FACTOR_DESC,
    MIN_AMP_FACTOR_DESC,
    NUM_AMP_POINTS_DESC,
    NUM_AMP_POINTS_OPTIONAL_DESC,
    AmplitudeSweepParameters,
    absolute_amps,
    amp_anchor,
    amp_sweep,
    attach_absolute_amp,
)
from .detuning import (
    DETUNING_AXIS,
    END_DRIVE_DETUNING_DESC,
    END_READOUT_DETUNING_DESC,
    NUM_FREQ_POINTS_DESC,
    START_DRIVE_DETUNING_DESC,
    START_READOUT_DETUNING_DESC,
    DriveDetuningSweepParameters,
    ReadoutDetuningSweepParameters,
    drive_detuning_sweep,
    readout_detuning_sweep,
)
from .flux import (
    FLUX_AXIS,
    FLUX_FRAME_ABSOLUTE,
    FLUX_FRAME_RELATIVE,
    MAX_FLUX_DESC,
    MAX_FLUX_PULSE_DESC,
    MIN_FLUX_DESC,
    MIN_FLUX_PULSE_DESC,
    NUM_FLUX_DESC,
    FluxComponentParameters,
    FluxPulseSweepParameters,
    FluxSweepParameters,
    flux_anchor_v,
    flux_frame,
    flux_sweep,
    foreign_flux_source,
)
from .qubit_reset import (
    ACTIVE_RESET_ROUNDS_DESC,
    RESET_METHOD_DESC,
    THERMALIZATION_TIME_DESC,
    QubitResetParameters,
    reset_wait_ns,
)
from .state_readout import (
    POPULATION_ALT,
    SHOT_STATE_ALT,
    ReadoutModeParameters,
    StateReadoutParameters,
    discrimination_method,
    joint_state_labels,
    joint_to_marginals,
    member_order,
    population_row,
    readout_vars,
    shot_state_vars,
    signal_rename,
    states_to_joint_population,
)

#: One line per capability for the CLI catalog browser (`scqo run --capability`),
#: in derivation order (``scqo.experiments._derived_capabilities``). Curated by
#: hand rather than scraped from the mixin docstrings: those carry reST markup,
#: and this package also holds mixins that are deliberately NOT capabilities
#: (ReadoutModeParameters, FluxComponentParameters), so scanning would mislead.
#: test_capabilities pins the keys to exactly the derivable set.
CAPABILITY_SUMMARIES = {
    "state_readout": "chooses analog I/Q vs FPGA state-discriminated acquisition",
    "flux": "sweeps a flux-bias window on a z line (absolute volts)",
    "qubit_reset": "resets the qubit between shots (thermal wait or active)",
    "flux_pulse": "the flux window is a pulse relative to idle_flux",
    "amplitude": "sweeps amplitude as a factor of the standing amplitude",
    "drive_detuning": "sweeps the drive detuning relative to the current drive frequency",
    "readout_detuning": "sweeps the readout detuning relative to the current readout frequency",
}

__all__ = [
    "ABS_AMP_COORD",
    "ABS_AMP_LABEL",
    "ACTIVE_RESET_ROUNDS_DESC",
    "AMP_AXIS",
    "CAPABILITY_SUMMARIES",
    "DETUNING_AXIS",
    "END_DRIVE_DETUNING_DESC",
    "END_READOUT_DETUNING_DESC",
    "FLUX_AXIS",
    "MAX_AMP_FACTOR_DESC",
    "MIN_AMP_FACTOR_DESC",
    "NUM_AMP_POINTS_DESC",
    "NUM_AMP_POINTS_OPTIONAL_DESC",
    "FLUX_FRAME_ABSOLUTE",
    "FLUX_FRAME_RELATIVE",
    "MAX_FLUX_DESC",
    "MAX_FLUX_PULSE_DESC",
    "MIN_FLUX_DESC",
    "MIN_FLUX_PULSE_DESC",
    "NUM_FLUX_DESC",
    "NUM_FREQ_POINTS_DESC",
    "POPULATION_ALT",
    "RESET_METHOD_DESC",
    "SHOT_STATE_ALT",
    "START_DRIVE_DETUNING_DESC",
    "START_READOUT_DETUNING_DESC",
    "THERMALIZATION_TIME_DESC",
    "DriveDetuningSweepParameters",
    "ReadoutDetuningSweepParameters",
    "FluxComponentParameters",
    "FluxPulseSweepParameters",
    "FluxSweepParameters",
    "AmplitudeSweepParameters",
    "QubitResetParameters",
    "ReadoutModeParameters",
    "StateReadoutParameters",
    "absolute_amps",
    "amp_anchor",
    "amp_sweep",
    "attach_absolute_amp",
    "discrimination_method",
    "drive_detuning_sweep",
    "flux_anchor_v",
    "flux_frame",
    "flux_sweep",
    "foreign_flux_source",
    "joint_state_labels",
    "joint_to_marginals",
    "member_order",
    "population_row",
    "readout_detuning_sweep",
    "readout_vars",
    "reset_wait_ns",
    "shot_state_vars",
    "signal_rename",
    "states_to_joint_population",
]
