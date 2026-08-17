"""State-readout capability: the digital (FPGA-discriminated) acquisition modes.

An experiment HAS this capability exactly when its Parameters subclass
:class:`StateReadoutParameters`; the catalog derives the ``"state_readout"`` tag
from that subclass relation (never from a declared string). The capability owns
the Parameters fields (one canonical text), the contract fragments
(``POPULATION_ALT`` / ``SHOT_STATE_ALT``), the simulate-side draws and packers,
the estimate-side rename (:func:`signal_rename`), and the joint-population
reduction helpers of the unified readout schema.

THE READOUT SCHEMA (canonical text: TUTORIAL "The readout schema"; digest in
CLAUDE.md). Readout output is declared by two axes plus one multi-qubit form:

* ``use_state_discrimination`` — analog (``I``/``Q`` volts) vs digital
  (discriminated) acquisition;
* ``readout_mode`` — ``"average"`` (FPGA-averaged, no shot axis) vs ``"shot"``
  (every shot kept on a trailing ``shot_idx`` dim);
* a DIGITAL + AVERAGE multi-qubit target stores the JOINT distribution
  (``joint_population`` over a ``joint_state`` label dim) — marginals are its
  partial trace, derived not stored.

The variable names carry the semantics: ``state`` is ALWAYS a per-shot integer
level (0/1/2… — a qubit may read out |2>), ``population`` is the averaged
marginal probability, ``joint_population`` the averaged joint distribution.
A backend that cannot realize a requested combo REFUSES it by name, never
downgrades.

BOUNDARY RULE: the field name ``use_state_discrimination`` and its semantics
(True => the probe returns discriminated data; False => ``I`` + ``Q``)
cross the probe boundary on BOTH backends — LCHQMDriver shells pass it verbatim to
the vendor ``build_program``; LCHQBDriver reads it in ``experiments/_state.py`` and
asks for the ``ThresholdedAcquisition`` protocol — so NEITHER side is ever renamed.
The two realize the same semantics on different vendor knobs (QM's readout-operation
``threshold``, Qblox's ``element.measure.acq_threshold``), which is why the
discriminator fields are ``portable=False``: the calibrated numbers do not transfer.

``attach_readout_positions`` deliberately does NOT ride along: it is an
Experiment ClassVar owned by experiments whose scqat estimator consumes the
stored blob centers (axial IQ reduction) — orthogonal to whether the probe can
return discriminated data.
"""

from __future__ import annotations

import itertools
from typing import Literal, Sequence

import numpy as np
import xarray as xr
from pydantic import Field

from ...parameters import Parameters

#: contract fragment: digital + AVERAGE — the FPGA-discriminated averaged
#: marginal probability (variable ``population``).
POPULATION_ALT: tuple[tuple[str, ...], ...] = (("population",),)

#: contract fragment: digital + SHOT — the per-shot integer level (variable
#: ``state``; the experiment's sweeps or readout dims carry ``shot_idx``).
SHOT_STATE_ALT: tuple[tuple[str, ...], ...] = (("state",),)


class StateReadoutParameters(Parameters):
    """Mixin: FPGA state-discriminated acquisition (digital instead of I/Q)."""

    use_state_discrimination: bool = Field(
        False,
        description="Discriminate each shot on the FPGA and return discriminated "
        "data (the averaged population, or per-shot states in shot mode) instead "
        "of I/Q. Requires a calibrated discriminator "
        "(run single_shot_readout, then accept its readout_rotation_rad / "
        "readout_threshold suggestions).",
    )


class ReadoutModeParameters(Parameters):
    """Mixin: shot-vs-average acquisition, for experiments realizing BOTH.

    Most experiments are one mode by construction (a per-shot readout
    calibration IS shot mode; a coherence sweep IS average mode) and do not
    subclass this. An experiment that genuinely offers the trade
    (full per-shot information vs the compact averaged form) declares it here;
    a backend that cannot realize the requested mode refuses it by name.
    """

    readout_mode: Literal["average", "shot"] = Field(
        "average",
        description="What the instrument does with the shots: 'average' returns "
        "the FPGA-averaged form (no shot axis; a multi-qubit target stores the "
        "joint_population distribution), 'shot' keeps every shot on a trailing "
        "shot_idx axis (full information, more memory; digital data stays "
        "per-member integer levels).",
    )


def discrimination_method(readout_mode: str) -> str:
    """The scqat ``readout_fidelity`` method that fits data acquired in this
    readout mode — ``"gmm"`` for ``"shot"``, ``"average"`` for ``"average"``.

    DERIVED, never a second knob: an FPGA-averaged acquisition returns ONE I/Q
    point per prepared state, and no Gaussian mixture can be trained on that, so
    letting an operator pair ``readout_mode="average"`` with a mixture fit would
    only make an unrepresentable request representable.
    """
    if readout_mode not in ("average", "shot"):
        raise ValueError(
            f"unknown readout_mode {readout_mode!r}; expected 'average' or 'shot'")
    return "average" if readout_mode == "average" else "gmm"


def population_row(
    population: np.ndarray, rng: np.random.Generator, *, noise: float = 0.02
) -> np.ndarray:
    """One target's (or one slice's) averaged discriminated readout row: the
    population plus exactly ONE ``rng.normal(0, noise, size)`` draw, clipped to
    [0, 1].

    The single-draw guarantee is load-bearing: per-target draw ordering (physics
    draws first, then readout draws) is part of the offline sims' stable_seed
    contract — tests replay the stream (scqat
    ``tests/test_stored_positions.py``).
    """
    population = np.asarray(population, dtype=float)
    return np.clip(population + rng.normal(0.0, noise, population.size), 0.0, 1.0)


def readout_vars(use_state: bool, population, i_data, q_data) -> dict[str, np.ndarray]:
    """The AVERAGE-mode simulate() return: ``{"population": ...}`` in
    discriminated mode, else ``{"I", "Q"}``."""
    return {"population": population} if use_state else {"I": i_data, "Q": q_data}


def shot_state_vars(use_state: bool, state, i_data, q_data) -> dict[str, np.ndarray]:
    """The SHOT-mode simulate() return: ``{"state": ...}`` (per-shot integer
    levels) in discriminated mode, else per-shot ``{"I", "Q"}``."""
    return {"state": state} if use_state else {"I": i_data, "Q": q_data}


def signal_rename(
    ds: xr.Dataset,
    base: dict[str, str] | None = None,
    *,
    iq_fallback: str | None = None,
) -> dict[str, str]:
    """The estimate-side rename dict: ``base`` (sweep-axis renames) merged with
    the signal-source mapping — ``population -> signal`` when the dataset is
    discriminated, else ``iq_fallback -> signal`` for estimators that consume a
    pre-reduced 1-D signal (the flux coherence pair maps ``"I"``).
    ``iq_fallback=None`` leaves I/Q untouched (the coherent-drive family hands
    complex IQ to scqat's axial reduction)."""
    rename = dict(base or {})
    if "population" in ds.data_vars:
        rename["population"] = "signal"
    elif iq_fallback is not None:
        rename[iq_fallback] = "signal"
    return rename


# --------------------------------------------------------------------------- #
# Joint-population helpers (the multi-qubit half of the readout schema).      #
# Used by simulate(), by the drivers' reduce_raw, and by tests — scqat never  #
# imports these (it reads the dataset).                                       #
# --------------------------------------------------------------------------- #


def joint_state_labels(m: int, n_levels: int = 2) -> list[str]:
    """The ``joint_state`` coordinate labels for an ``m``-member target:
    per-member level-digit strings, leftmost digit = member 0 (the HIGH role).

    Binary pair: ``["00", "01", "10", "11"]``; ``n_levels=3`` adds the
    f-resolved digits (``"02"``, ``"12"``, …). Always generated, never
    hand-listed — the digit order IS the member order."""
    if m < 1 or n_levels < 2:
        raise ValueError(f"need m >= 1 members and n_levels >= 2, got m={m}, n_levels={n_levels}")
    digits = "".join(str(level) for level in range(n_levels))
    return ["".join(combo) for combo in itertools.product(digits, repeat=m)]


def states_to_joint_population(
    state: xr.DataArray,
    *,
    member_dim: str = "member",
    shot_dim: str = "shot_idx",
    n_levels: int = 2,
) -> xr.DataArray:
    """Per-shot member levels -> the averaged joint distribution.

    ``state`` carries integer levels with a ``member_dim`` (member order = the
    label digit order, high first) and a ``shot_dim``; every other dim is
    preserved. Returns ``joint_population`` with a leading ``joint_state`` label
    dim replacing both: the fraction of shots landing in each joint outcome,
    summing to 1 over ``joint_state`` at every remaining point. Levels are
    clipped to ``n_levels - 1`` (a binary reduction counts |2> as excited)."""
    m = int(state.sizes[member_dim])
    levels = state.clip(0, n_levels - 1).astype(np.int64)
    weights = xr.DataArray(
        (n_levels ** np.arange(m)[::-1]).astype(np.int64),
        dims=member_dim,
        coords={member_dim: state[member_dim]} if member_dim in state.coords else None,
    )
    codes = (levels * weights).sum(member_dim)  # dims: shot + remaining
    labels = joint_state_labels(m, n_levels)
    joint = xr.concat(
        [(codes == v).mean(shot_dim) for v in range(n_levels ** m)],
        dim="joint_state",
    ).assign_coords(joint_state=labels)
    return joint.rename("joint_population")


def joint_to_marginals(
    joint_population: xr.DataArray, members: Sequence[str] | None = None
) -> xr.DataArray:
    """Partial trace: P(member excited, level >= 1) per member.

    The inverse direction of :func:`states_to_joint_population`'s averaging —
    the marginal ``population`` of each member, from the stored joint. Returns a
    DataArray with a leading ``member`` dim (coord = ``members`` when given,
    else the digit positions), other dims preserved."""
    labels = [str(v) for v in joint_population["joint_state"].values]
    m = len(labels[0]) if labels else 0
    if m == 0 or any(len(label) != m for label in labels):
        raise ValueError(f"malformed joint_state labels: {labels}")
    marginals = [
        joint_population.sel(joint_state=[lb for lb in labels if lb[i] != "0"]).sum("joint_state")
        for i in range(m)
    ]
    coord = list(members) if members is not None else list(range(m))
    if members is not None and len(coord) != m:
        raise ValueError(f"{m} label digits but {len(coord)} member names ({coord})")
    return xr.concat(marginals, dim="member").assign_coords(member=coord).rename("population")


def member_order(roster, target: str) -> tuple[str, ...]:
    """The member mode names of a composite target in ROLE order (high, low) —
    the digit order of its ``joint_state`` labels and its ``member`` coordinate.

    The roster's declared roles are the only source (design-nominal topology; a
    live-f_01 ordering legitimately crosses during tuning), and each role must
    name exactly one member."""
    entity = roster.entities.get(target)
    roles = getattr(entity, "roles", None) or {}
    out: list[str] = []
    for role in ("high", "low"):
        members = roles.get(role, ())
        if len(members) != 1:
            raise ValueError(
                f"{target}: role {role!r} has {len(members)} member(s) "
                f"{list(members)} - the member order needs exactly one each")
        out.append(members[0])
    return tuple(out)
