"""Single-excitation swap chevron — flux amplitude x pulse duration, record-only.

Excite ONE member of a pair, then sweep a flux pulse on one member's flux line
against its duration and read BOTH members out jointly. Where the two qubits are
brought into resonance the excitation oscillates between them, so the transfer
map draws the familiar chevron arch: full contrast on the resonance amplitude,
arms narrowing and oscillating faster as the detuning grows. The arch locates
both the resonance amplitude and the full-swap time — the bring-up predecessor
to :mod:`scqo.experiments.pair_zz_coupler` (find the swap point, then the
decouple point).

RECORD-ONLY for the DEVICE: there is no ``update()`` and nothing lands on the
device surface; the per-map summary lives in ``result.fit``. A scqat estimator
(``pair_swap_chevron``) now draws the raw joint state populations — a per-pair
2x2 population figure plus plotdata/metadata under ``analysis/<pair>/`` — but it
only VISUALIZES the maps and proposes nothing; the SUCCESS verdict
(``min_transfer``) is made here in ``estimate()``, not by the estimator.
"""

from __future__ import annotations

from typing import ClassVar, Literal

import numpy as np
from pydantic import Field

from ..contract import DatasetContract
from ._capabilities.qubit_reset import QubitResetParameters
from ._capabilities.state_readout import joint_state_labels
from ._sim import stable_seed
from ._time_grid import time_axis_ns
from ..parameters import AveragingParameters, TargetSelection
from ..result import Outcome, Result
from ..experiment import Experiment
from . import register

#: canonical role-selector texts, shared with pair_swap_flux_map so the two
#: catalog entries cannot drift apart.
DRIVE_SIDE_DESC = (
    "Which pair member is excited with the pi pulse (roster roles; the driver "
    "maps high/low onto its vendor control/target)."
)
FLUX_SIDE_DESC = (
    "Which pair member's flux line carries the swept flux pulse (roster roles; "
    "the driver maps high/low onto its vendor control/target)."
)
MIN_TRANSFER_DESC = (
    "Peak excitation transfer onto the undriven member below which the map is "
    "reported FAILED — no swap feature was found anywhere in the window. Pure "
    "reporting: this experiment writes nothing back."
)


class PairSwapChevronParameters(TargetSelection, AveragingParameters, QubitResetParameters):
    """Inputs for the swap chevron. ``targets`` are PAIR components."""

    min_flux_amp_v: float = Field(
        0.0,
        description="Lowest flux-pulse amplitude (V at the DAC; the usable range is "
                    "the flux port's own, and the backend refuses past it).")
    max_flux_amp_v: float = Field(
        0.3, description="Highest flux-pulse amplitude (V).")
    num_amp_points: int = Field(41, gt=4, description="Number of flux-amplitude points.")
    min_swap_time_ns: float = Field(
        1.0, ge=1.0,
        description="Shortest flux-pulse duration (ns; 1 ns is the floor — the driver "
                    "realizes sub-clock granularity by baking, and a zero-length pulse "
                    "has no waveform).")
    max_swap_time_ns: float = Field(
        100.0, gt=1.0,
        description="Longest flux-pulse duration (ns; quantized to the instrument grid "
                    "by the driver).")
    num_time_points: int = Field(100, gt=4, description="Number of duration points.")
    drive_side: Literal["high", "low"] = Field("low", description=DRIVE_SIDE_DESC)
    flux_side: Literal["high", "low"] = Field("low", description=FLUX_SIDE_DESC)
    min_transfer: float = Field(0.3, ge=0.0, le=1.0, description=MIN_TRANSFER_DESC)


class PairSwapChevronResult(Result):
    """``fit[pair]``: ``best_transfer`` (peak excitation on the UNDRIVEN member —
    which member that is follows from the ``drive_side`` parameter) and its
    ``best_flux_amp_v`` / ``best_swap_time_ns`` coordinates, the per-map ranges
    ``p_high_min/max`` and ``p_low_min/max``, ``p_ee_max`` (the
    double-excitation witness) and the axis sizes. Record-only: no ``update()``,
    nothing written to the device."""


@register
class PairSwapChevron(Experiment):
    """Backend-agnostic swap chevron. ``probe()`` is supplied by a driver."""

    name: ClassVar[str] = "pair_swap_chevron"
    description: ClassVar[str] = (
        "Single-excitation swap chevron: excite ONE member of a pair, then sweep a flux "
        "pulse (absolute volts) on one member's flux line against its duration, reading "
        "both members' joint populations. The arch of the excitation transfer locates the "
        "resonance amplitude and the full-swap time — the bring-up step before a coupler "
        "decouple point exists. Record-only diagnostic: the per-map summary lands in "
        "result.fit and nothing is written back to the device."
    )
    Parameters: ClassVar[type] = PairSwapChevronParameters
    Result: ClassVar[type] = PairSwapChevronResult
    Contract: ClassVar[DatasetContract] = DatasetContract(
        sweeps=("flux_amp_v", "swap_time_ns"), sweep_units=("V", "ns"),
        # the readout schema's digital+average+joint form: the stored variable
        # is the joint distribution over the pair's basis states (digit order
        # high, low); member marginals are its partial trace, derived not stored.
        variables=("joint_population",), readout_dims=("joint_state",),
    )
    target_kinds: ClassVar[tuple[str, ...]] = ("qubit_pair",)
    #: none, deliberately: a composite's operations are DECLARED, and this
    #: experiment exists to find the swap point BEFORE any two-qubit gate is
    #: defined. Requiring "iswap" (or "cz") would refuse exactly the bring-up
    #: chip it is for.
    required_operations: ClassVar[tuple[str, ...]] = ()

    params: PairSwapChevronParameters

    def define_sweep(self) -> dict[str, np.ndarray]:
        return {
            "flux_amp_v": np.linspace(self.params.min_flux_amp_v,
                                      self.params.max_flux_amp_v,
                                      self.params.num_amp_points),
            # grid_ns=1: one contiguous pulse, no echo arms to keep whole.
            "swap_time_ns": time_axis_ns(self.params.min_swap_time_ns,
                                         self.params.max_swap_time_ns,
                                         self.params.num_time_points, grid_ns=1),
        }

    def simulate(self, coords: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
        """Resonant two-level exchange — the model the arch comes from.

        With detuning ``delta(v)`` and coupling ``J`` (Hz), the transfer at time
        ``t`` is ``(2J)^2/omega^2 * sin^2(pi*omega*t)`` with
        ``omega = sqrt(delta^2 + (2J)^2)``: full contrast only on resonance,
        arms narrowing and oscillating faster off it.
        """
        v = coords["flux_amp_v"]
        t_ns = coords["swap_time_ns"]
        pairs = self.params.targets
        rng = np.random.default_rng(stable_seed("pair_swap_chevron", *pairs))
        span = float(np.ptp(v)) or 1.0
        shape = (len(pairs), v.size, t_ns.size)
        p_driven = np.empty(shape)
        p_partner = np.empty(shape)
        p_ee = np.empty(shape)
        v_step = span / max(v.size - 1, 1)
        for k in range(len(pairs)):
            v0 = float(rng.uniform(v.min() + 0.25 * span, v.min() + 0.75 * span))
            j_hz = float(rng.uniform(3e6, 9e6))
            # The detuning slope is drawn RELATIVE to the amplitude grid so the
            # arch is a few points wide — i.e. the sweep resolves it, which is
            # what an operator narrows the window until it does. Drawing an
            # absolute Hz/V slope instead lets the arch fall between two
            # amplitude points, and the simulator then reports "no swap" on
            # data that is merely under-sampled.
            slope = (2 * j_hz) * float(rng.uniform(0.3, 0.8)) / v_step  # Hz per V
            tau_ns = float(rng.uniform(300.0, 900.0))
            prep = float(rng.uniform(0.94, 0.99))               # pi-pulse fidelity
            therm = float(rng.uniform(0.005, 0.02))             # residual |ee>
            delta = slope * (v - v0)
            omega = np.sqrt(delta ** 2 + (2 * j_hz) ** 2)
            swap = (((2 * j_hz) ** 2 / omega ** 2)[:, None]
                    * np.sin(np.pi * omega[:, None] * t_ns[None, :] * 1e-9) ** 2
                    * np.exp(-t_ns[None, :] / tau_ns))
            p_partner[k] = prep * swap + rng.normal(0, 0.02, (v.size, t_ns.size))
            p_driven[k] = (prep * (1.0 - swap) * np.exp(-t_ns[None, :] / (8 * tau_ns))
                           + rng.normal(0, 0.02, (v.size, t_ns.size)))
            p_ee[k] = therm + rng.normal(0, 0.005, (v.size, t_ns.size))
        for arr in (p_driven, p_partner, p_ee):
            np.clip(arr, 0.0, 1.0, out=arr)
        joint = _joint_from_roles(self.params.drive_side, p_driven, p_partner, p_ee)
        return {"joint_population": (
            ("target", "joint_state", "flux_amp_v", "swap_time_ns"), joint)}

    def readout_coords(self) -> dict:
        return {"joint_state": joint_state_labels(2)}

    def estimate(self) -> PairSwapChevronResult:
        assert self.dataset is not None, "run() populates self.dataset before estimate()"
        ds = self.dataset.transpose("target", "joint_state", "flux_amp_v", "swap_time_ns")
        # Raw joint-state-population maps -> scqat artifacts (figure + plotdata +
        # metadata, one folder per pair). Record-only: the SUCCESS verdict below
        # (min_transfer) stays here; the estimator only draws the populations.
        from scqat.estimators.pair_swap_chevron import PairSwapChevronEstimator
        from .._scqat import per_qubit_results

        per_qubit_results(ds, PairSwapChevronEstimator(), artifact_dir=self.artifact_dir,
                          drive_side=self.params.drive_side, flux_side=self.params.flux_side,
                          per_target_kwargs=_role_names(self.device, self.params.targets))
        result = PairSwapChevronResult()
        for pair in self.params.targets:
            fit, ok = summarize_transfer_map(
                ds.sel(target=pair), self.params.drive_side,
                ("flux_amp_v", "swap_time_ns"), self.params.min_transfer)
            result.fit[pair] = fit
            result.outcomes[pair] = Outcome.SUCCESSFUL if ok else Outcome.FAILED
        return result

    @classmethod
    def validate_targets(cls, roster, targets):
        """The flux line the amplitude axis rides. WHICH member carries it is a
        parameter (``flux_side``) and this hook cannot see params, so the roster
        gate is "at least one member can"; the driver refuses the SELECTED
        member pre-probe when it is the one without a flux channel.

        No coupler gate: the swap pulse rides a qubit's own flux line and the
        coupler is never addressed — unlike ``pair_swap_flux_map``."""
        return _flux_member_problems(roster, targets,
                                     "nothing to play the swap pulse on")

    def probe(self):  # pragma: no cover - driver half
        raise NotImplementedError("a driver backend supplies probe()")


def _joint_from_roles(drive_side: str, p_driven: np.ndarray, p_partner: np.ndarray,
                      p_ee: np.ndarray) -> np.ndarray:
    """Package simulated driven/partner marginals as the joint distribution.

    The sim kernels produce role-free marginals (driven / partner / |ee>);
    this labels them by ROSTER role and stacks the four basis populations on a
    new axis 1 in ``joint_state_labels(2)`` order ("00", "01", "10", "11" —
    digit order high, low). Branching on ``drive_side`` is what makes an
    offline test exercise the role mapping instead of silently accepting
    either orientation."""
    driven_is_high = drive_side == "high"
    p_high = p_driven if driven_is_high else p_partner
    p_low = p_partner if driven_is_high else p_driven
    p11 = np.clip(p_ee, 0.0, 1.0)
    p10 = np.clip(p_high - p_ee, 0.0, 1.0)
    p01 = np.clip(p_low - p_ee, 0.0, 1.0)
    p00 = np.clip(1.0 - (p01 + p10 + p11), 0.0, 1.0)
    return np.stack([p00, p01, p10, p11], axis=1)


def summarize_transfer_map(one_pair, drive_side: str, axes: tuple[str, str],
                           min_transfer: float) -> tuple[dict, bool]:
    """The record-only summary of one pair's 2D transfer map.

    ``one_pair`` is the dataset sliced to a single target, carrying the joint
    form (``joint_population`` over ``joint_state``). The reported peak is of
    the UNDRIVEN member's marginal (its partial trace) — that is the excitation
    transfer, and the driven member's own decay cannot be told from a swap.
    Returns ``(fit, ok)``; ``ok`` is the SUCCESSFUL/FAILED verdict."""
    jp = one_pair["joint_population"]
    p_high = (jp.sel(joint_state="10") + jp.sel(joint_state="11")).values
    p_low = (jp.sel(joint_state="01") + jp.sel(joint_state="11")).values
    transfer = np.asarray(p_high if drive_side == "low" else p_low, dtype=float)
    # `fit` is the NUMERIC extraction surface (Result.fit is dict[str, float]);
    # which member the peak came from is derivable from the persisted
    # drive_side parameter, so it does not belong here as a string.
    fit = {
        "p_high_min": float(np.nanmin(p_high)),
        "p_high_max": float(np.nanmax(p_high)),
        "p_low_min": float(np.nanmin(p_low)),
        "p_low_max": float(np.nanmax(p_low)),
        "p_ee_max": float(np.nanmax(jp.sel(joint_state="11").values)),
        **{f"n_{axis}": int(one_pair.sizes[axis]) for axis in axes},
    }
    if not np.isfinite(transfer).any():
        # an all-NaN map is a failed acquisition, not a zero-transfer chip
        fit["best_transfer"] = float("nan")
        return fit, False
    i, j = np.unravel_index(int(np.nanargmax(transfer)), transfer.shape)
    fit["best_transfer"] = float(transfer[i, j])
    fit[f"best_{axes[0]}"] = float(one_pair[axes[0]].values[i])
    fit[f"best_{axes[1]}"] = float(one_pair[axes[1]].values[j])
    return fit, fit["best_transfer"] >= min_transfer


def _flux_member_problems(roster, targets, why: str) -> list[str]:
    """Shared roster gate: each pair needs a member whose flux channel exists."""
    problems = []
    for pair in targets:
        entity = roster.entities.get(pair)
        members = [m for role in ("high", "low")
                   for m in getattr(entity, "roles", {}).get(role, ())]
        if not members:
            problems.append(f"{pair}: declares no high/low members")
        elif not any((m, "flux") in roster.defaults for m in members):
            problems.append(f"{pair}: neither member has a flux channel — {why}")
    return problems


def _role_names(device, targets) -> dict[str, dict[str, str]]:
    """Per-pair ``{"high_name", "low_name"}`` from the roster so the estimator
    labels the figure with the ACTUAL member qubit names (q0/q1) instead of the
    high/low roles. Falls back to the role words when the mapping is unavailable
    (no device/roster, or a pair that declares no members) so plotting never
    depends on it."""
    roster = getattr(device, "roster", None)
    out: dict[str, dict[str, str]] = {}
    for pair in targets:
        roles = getattr(roster.entities.get(pair), "roles", {}) if roster is not None else {}
        roles = roles or {}
        hi = roles.get("high", ())
        lo = roles.get("low", ())
        out[pair] = {
            "high_name": hi[0] if hi else "high",
            "low_name": lo[0] if lo else "low",
        }
    return out
