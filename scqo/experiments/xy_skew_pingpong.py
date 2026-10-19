"""XY line skew by a DRAG-like ping-pong (physics half; contrib, being hardware-tested).

Measures, with the sensor qubit itself, the arrival-time difference tau at the sensor between its own
xy line (L1) and a second xy line (L2, the source qubit's line) that reaches the sensor by crosstalk.
L2 plays at the SENSOR's frequency, so its leak drives the sensor resonantly.

Per pulse C(s), s = +-1 (the whole waveform sign, both lines together):

    L1:  s * [ x180(t) - kflip * k * Y * Ox(t) ]            x180 = the sensor's own DRAG pulse
    L2:  s * kflip * (k / c_amp) * exp(i via_phase) * Ox(t - tau_sw)

Ox is x180's in-phase envelope, Y the sensor's y180/x180 axis factor (+-i). With c_amp / via_phase
from mode='calibrate', the leak of L2 at the sensor is +kflip*k*Y*Ox(t - tau_sw - tau): the two
Y parts cancel only when tau_sw = -tau. The residual k*Y*[Ox(t - tau_sw - tau) - Ox(t)] is a DRAG-like
derivative term, i.e. a detuning that the sequence

    y90 - [C(+), C(-)] x N - read P1

accumulates over the N pairs while a c_amp / via_phase miscalibration (first order) cancels within each
pair. The signal S = P1(kflip=+1) - P1(kflip=-1) is odd in (tau_sw + tau); its zero crossing tau_sw* is
the alignment point. kflip = 0 (include_background) plays L1's plain x180 pairs only: the sensor's own
DRAG background, which sets the slope's size and SIGN at each N (16 ns pulses turn it fast).

tau_sw is a software shift: each value is a separate pre-computed waveform (band-limited fractional
delay of the 1 GS/s samples).

mode='calibrate' measures the L2 -> sensor crosstalk at the sensor's frequency by tomography of N
repeated small pulses: own_x / own_y (the sensor's x180 / y180 scaled by cal_own_scale) and via_0 /
via_90 (L2 playing cal_via_scale * Ox at waveform phase 0 / 90 deg). The fitted rotation axes give
via_phase (L2's leak along the sensor's +Y, handedness from via_90) and the rates c_amp.
Optional checks in the same tomography:
  * include_null: 'null' plays the scan's two Y parts together without x180 (L1 -cal_null_k Y Ox and
    L2 (cal_null_k / c_amp) e^{i via_phase} Ox, the run's c_amp / via_phase). null_ratio ~0 confirms
    amplitude and direction (~2: flipped); the residual gives refined values. It compares AREAS only,
    so it cannot see a timing offset.
  * include_flip_check: 'l1_flip' plays L1's Y part alone and 'l2_flip' L2's scan op alone (tau_sw
    nearest 0), alternating the + and - waveforms. A line whose sign really flips nets ~0; one that
    does not accumulates k x the sensor's rate per pulse (flip_ratio ~1).

Sign: the reported tau_sw* is the software shift of L2 that aligns it. Do not trust the sign convention
before the hardware check: add +1 ns to L2's port delay and the zero crossing must move by -1 ns.

Analysis: scqat's XySkewPingpongEstimator (figures: S(tau_sw) + raw P1 for a scan; tomography
trajectories + the equatorial rotation axes for a calibration).
"""

from __future__ import annotations

import math
from typing import Any, ClassVar, Literal

import numpy as np
import xarray as xr
from pydantic import Field, field_validator, model_validator

from ..contract import ContractError, DatasetContract
from ..experiment import Experiment
from ..parameters import AveragingParameters, TargetSelection
from ..result import Outcome, Result
from . import register
from ._capabilities.qubit_reset import QubitResetParameters
from ._capabilities.state_readout import POPULATION_ALT, StateReadoutParameters
from ._sim import stable_seed

MODES = ("scan", "calibrate")
CAL_CONDITIONS = ("own_x", "own_y", "via_0", "via_90")
NULL_CONDITION = "null"
FLIP_CONDITIONS = ("l1_flip", "l2_flip")
BASES = ("z", "x", "y")
#: largest |L2 waveform sample| allowed (MW-FEM waveform range is [-1, 1))
MAX_VIA_AMPLITUDE = 0.95
#: the offline simulator's truth: L2's leak arrives SIM_SKEW_NS late; crosstalk amplitude and axis
SIM_SKEW_NS = 0.35
SIM_C_AMP = 0.09
SIM_C_AXIS = 0.7


def _parse_float_list(val: Any) -> Any:
    """'a:b:step' (inclusive), 'a,b,c' or a single number -> list of floats."""
    if isinstance(val, str):
        parts = [p.strip() for p in val.split(":")]
        if len(parts) == 3:
            start, stop, step = (float(p) for p in parts)
            n = int(round((stop - start) / step)) + 1
            return [round(start + i * step, 6) for i in range(max(n, 1))]
        return [float(p) for p in val.split(",")]
    if isinstance(val, (int, float)):
        return [float(val)]
    return val


def _parse_int_list(val: Any) -> Any:
    """'a:b:step' (inclusive), 'a,b,c' or a single int -> list of ints."""
    if isinstance(val, str):
        parts = [p.strip() for p in val.split(":")]
        if len(parts) in (2, 3):
            step = int(parts[2]) if len(parts) == 3 else 1
            return list(range(int(parts[0]), int(parts[1]) + 1, step))
        return [int(p) for p in val.split(",")]
    if isinstance(val, (int, float)):
        return [int(val)]
    return val


class XySkewPingpongParameters(TargetSelection, AveragingParameters, StateReadoutParameters, QubitResetParameters):
    """Inputs for xy_skew_pingpong."""

    sensor_qubit: str = Field("q1", description="Qubit that senses the skew (its own line is L1).")
    source_qubit: str = Field("q3", description="Qubit whose xy line is L2; L2 plays at the sensor's frequency.")
    targets: list[str] = Field(default_factory=list, description="[sensor_qubit, source_qubit]; auto-derived.")

    mode: Literal["scan", "calibrate"] = Field(
        "scan",
        description="'calibrate': tomography of the L2 -> sensor crosstalk (gives c_amp, via_phase). "
        "'scan': the ping-pong S(tau_sw) sweep.",
    )

    # ---- scan
    k: float = Field(
        0.1, gt=0.0, le=1.0,
        description="Crosstalk Rabi amplitude at the sensor as a fraction of the sensor's own x180 amplitude.",
    )
    c_amp: float = Field(
        0.09, gt=0.0,
        description="|c|: leak amplitude at the sensor per unit L2 amplitude, relative to L1 (from mode='calibrate').",
    )
    via_phase: float = Field(
        0.0, description="Phase (rad) on L2's waveform that puts its leak along the sensor's +Y (from mode='calibrate')."
    )
    tau_sw_ns: list[float] = Field(
        default_factory=lambda: [round(-3.0 + 0.25 * i, 6) for i in range(25)],
        description="Software shifts of L2 (ns): a list, 'start:stop:step' (inclusive) or 'a,b,c'.",
    )
    repetitions: list[int] = Field(
        default_factory=lambda: [2],
        description="Ping-pong pair counts N (scan) or pulse counts N (calibrate): list, 'a:b:step' or 'a,b'. "
        "Scan: the slope grows with N (chipQM 2026-10-19, k 0.05: -0.11 / -0.22 / -0.41 / -0.69 per ns at "
        "N 2 / 4 / 8 / 16), so a fine scan wants N 8,16 (300-500 averages, +-0.6 ns, fit_window_ns 0.3) and a "
        "coarse one N 1,2. Calibrate: e.g. 0:20:2, keeping N x rotation below 2 pi.",
    )
    include_background: bool = Field(
        True, description="Also run kflip = 0 (L1's plain x180 pairs, L2 silent): the sensor's own DRAG background."
    )
    pad_ns: int = Field(
        4, ge=1, le=16,
        description="Zeros (ns) on each side of each scan pulse; |tau_sw| must stay below it.",
    )
    tau_batch_size: int = Field(
        25, gt=0,
        description="tau_sw points per program (each program compiles and runs on its own). Keep one program "
        "under ~30 s (shots x points x shot time): cross-line errors drift within long programs.",
    )
    program_layout: Literal["loops", "unrolled"] = Field(
        "loops",
        description="Scan QUA layout. 'loops': N pairs as one QUA loop per element (the SQRB's gap-free pattern; "
        "compiles fast, N costs nothing). 'unrolled': every pulse unrolled in strict_timing_ (the first hardware "
        "version, kept for A/B).",
    )
    fit_window_ns: float = Field(
        1.5, gt=0.0,
        description="Half width (ns) around the zero crossing used for the linear fit of S. 1.5 ns: on chipQM "
        "(16 ns pulses, k 0.05, N 1-2) S stayed linear over +-1.5 ns, and 0.5 ns left only 4 points "
        "(2026-10-19: errors 0.23-0.81 ns vs 0.08-0.11 ns with the wider window).",
    )
    debug_timestamps: bool = Field(
        False,
        description="Scan diagnostic: stamp every play of both lines (timestamp_stream, 4 ns cycles) and store "
        "them in the dataset (ts_y90, ts_l1, ts_l2). Runs ONE program: the first tau_sw only, at most 10 shots.",
    )

    # ---- calibrate
    cal_own_scale: float = Field(
        0.05, gt=0.0, le=1.0,
        description="Scale of the sensor's x180 / y180 in the calibration pulse trains (9 deg per pulse at 0.05); "
        "keep N x rotation below 2 pi for both own and via conditions.",
    )
    cal_via_scale: float = Field(
        0.5, gt=0.0, le=MAX_VIA_AMPLITUDE,
        description="L2 amplitude in the calibration, in units of the sensor's x180 envelope.",
    )
    include_null: bool = Field(
        False,
        description="Calibrate: add the 'null' condition (the scan's L1 -Y part and L2's leak together, no x180) "
        "with the c_amp / via_phase given here, from a previous calibration. Net rotation ~0 confirms them.",
    )
    cal_null_k: float = Field(
        0.03, gt=0.0, le=0.2,
        description="k of the null condition. Small, so a flipped direction (2x the rate) stays below 2 pi at the "
        "largest N: 0.03 is 5.4 deg per pulse for one part alone.",
    )
    include_flip_check: bool = Field(
        False,
        description="Calibrate: add 'l1_flip' / 'l2_flip' - each line's Y part ALONE (L2 = the scan's own op at the "
        "tau_sw nearest 0, scan k), alternating + and - waveforms. ~0 rotation if the line's sign flips; "
        "k x the sensor's rate per pulse (flip_ratio ~1) if it does not.",
    )

    def cal_conditions(self) -> list[str]:
        return (list(CAL_CONDITIONS) + ([NULL_CONDITION] if self.include_null else [])
                + (list(FLIP_CONDITIONS) if self.include_flip_check else []))

    def kflips(self) -> list[int]:
        return [1, -1, 0] if self.include_background else [1, -1]

    def flip_tau_index(self) -> int:
        """The scan op the l2_flip condition plays: the tau_sw nearest 0."""
        return int(np.argmin(np.abs(np.asarray(self.tau_sw_ns, dtype=float))))

    @field_validator("tau_sw_ns", mode="before")
    @classmethod
    def _parse_taus(cls, val: Any) -> Any:
        return _parse_float_list(val)

    @field_validator("repetitions", mode="before")
    @classmethod
    def _parse_reps(cls, val: Any) -> Any:
        return _parse_int_list(val)

    @model_validator(mode="before")
    @classmethod
    def _resolve_targets(cls, data: Any) -> Any:
        if isinstance(data, dict):
            targets = data.get("targets")
            if isinstance(targets, str):
                targets = [t.strip() for t in targets.split(",") if t.strip()]
            if targets and len(targets) >= 2:
                data.setdefault("sensor_qubit", targets[0])
                data.setdefault("source_qubit", targets[1])
            elif targets:
                data.setdefault("sensor_qubit", targets[0])
                # one target named: pick a partner the way crosstalk_cancellation_tomography does
                data.setdefault("source_qubit", "q2" if targets[0] != "q2" else "q1")
            data["targets"] = [data.get("sensor_qubit", "q1"), data.get("source_qubit", "q3")]
        return data

    @model_validator(mode="after")
    def _check(self) -> "XySkewPingpongParameters":
        if self.sensor_qubit == self.source_qubit:
            raise ValueError("sensor_qubit and source_qubit must differ")
        if not self.repetitions or min(self.repetitions) < 0:
            raise ValueError("repetitions must be non-negative counts")
        if not self.tau_sw_ns:
            raise ValueError("tau_sw_ns is empty")
        worst = max(abs(t) for t in self.tau_sw_ns)
        if worst > self.pad_ns - 1:
            raise ValueError(f"|tau_sw| up to {worst} ns needs pad_ns >= {math.ceil(worst) + 1}")
        return self


class XySkewPingpongContract(DatasetContract):
    """target x (tau_sw_ns, repetitions, kflip) in scan mode, target x (condition, repetitions, basis) in calibrate."""

    def validate(self, ds: xr.Dataset) -> None:
        scan = {"tau_sw_ns", "repetitions", "kflip"}
        cal = {"condition", "repetitions", "basis"}
        dims = set(ds.dims)
        problems = []
        if "target" not in dims:
            problems.append("missing dimension 'target'")
        if not (scan <= dims or cal <= dims):
            problems.append(f"dims {sorted(dims)} carry neither {sorted(scan)} nor {sorted(cal)}")
        if not any(v in ds.data_vars for v in ("population", "I")):
            problems.append("missing data variable ('population' or 'I')")
        if problems:
            raise ContractError("; ".join(problems))


class XySkewPingpongResult(Result):
    """Output of xy_skew_pingpong."""

    fit: dict[str, dict[str, Any]] = Field(default_factory=dict, description="Per-sensor fit (mode-dependent).")


# ----------------------------------------------------------------------------- waveforms
def fractional_shift(x: np.ndarray, tau: float) -> np.ndarray:
    """x delayed by tau samples (band-limited: the shift of what the DAC reconstructs from x).

    Zero-padded FFT phase ramp; an integer tau is an exact sample shift. x should carry enough zeros
    at both ends for the shifted pulse (the band-limited tails beyond the window are cut).
    """
    x = np.asarray(x, dtype=complex)
    n = len(x)
    size = 1 << int(math.ceil(math.log2(8 * n)))
    f = np.fft.fftfreq(size)
    return np.fft.ifft(np.fft.fft(x, size) * np.exp(-2j * np.pi * f * float(tau)))[:n]


def axis_factor(x180: np.ndarray, y180: np.ndarray) -> complex:
    """Y = y180 / x180 as one unit complex number (+-i for a standard QUAM pair)."""
    z = complex(np.vdot(x180, y180))
    if abs(z) == 0:
        raise ValueError("x180 and y180 are orthogonal or empty")
    return z / abs(z)


def scan_width(n_samples: int, pad: int) -> int:
    """Samples per scan pulse: the pulse plus pad zeros each side, a multiple of 4 and at least 16."""
    return max(16, int(math.ceil((n_samples + 2 * pad) / 4.0)) * 4)


def scan_waveforms(
    x180: np.ndarray, y180: np.ndarray, *, k: float, c_amp: float, via_phase: float, tau_ns: float, pad: int,
    kflip: int, sign: int,
) -> tuple[np.ndarray, np.ndarray]:
    """(L1, L2) complex samples of C(sign) for one tau_sw and kflip (L2 is all zeros at kflip = 0)."""
    n = len(x180)
    width = scan_width(n, pad)
    zx = np.zeros(width, complex)
    zx[pad:pad + n] = x180
    ox = np.zeros(width, complex)
    ox[pad:pad + n] = np.real(x180)
    yax = axis_factor(x180, y180)
    l1 = sign * (zx - kflip * k * yax * ox)
    l2 = sign * kflip * (k / c_amp) * np.exp(1j * via_phase) * fractional_shift(ox, tau_ns)
    return l1, l2


def null_waveforms(
    x180: np.ndarray, y180: np.ndarray, *, k: float, c_amp: float, via_phase: float
) -> tuple[np.ndarray, np.ndarray]:
    """(L1, L2) of the null check: the scan's two Y parts at tau_sw = 0 without x180 (unpadded).

    L1 = -k Y Ox and L2 = (k / c_amp) e^{i via_phase} Ox; with the right c_amp / via_phase L2's leak at the
    sensor is +k Y Ox and the sum is zero.
    """
    ox = np.real(x180).astype(complex)
    return -k * axis_factor(x180, y180) * ox, (k / c_amp) * np.exp(1j * via_phase) * ox


# ----------------------------------------------------------------------------- offline model
def _sim_pulses(length: int = 16, alpha_ghz: float = -0.205):
    """DragCosine x180 / y180 at 1 GS/s for the offline model (first-order DRAG)."""
    t = np.arange(length)
    env = 0.5 * (1 - np.cos(2 * np.pi * t / length))
    denv = np.pi / length * np.sin(2 * np.pi * t / length)
    a0 = np.pi / env.sum()
    beta = -1 / (2 * np.pi * alpha_ghz)
    x180 = a0 * (env + 1j * beta * denv)
    return x180, 1j * x180


def _sim_evolve(segments: list[np.ndarray], alpha_ghz: float = -0.205, sub: int = 4) -> np.ndarray:
    """3-level transmon in the sensor frame driven by 1 GS/s envelopes (zero-order hold, `sub` steps/ns)."""
    from scipy.linalg import expm

    alpha = 2 * np.pi * alpha_ghz
    a = np.diag([1.0, np.sqrt(2.0)], 1)
    h0 = np.diag([0.0, 0.0, alpha])
    dt = 1.0 / sub
    psi = np.array([1, 0, 0], complex)
    cache: dict = {}
    for seg in segments:
        key = seg.tobytes()
        if key not in cache:
            u = np.eye(3, dtype=complex)
            for o in seg:
                step = expm(-1j * (h0 + 0.5 * (o * a.T + np.conj(o) * a)) * dt)
                u = np.linalg.matrix_power(step, sub) @ u
            cache[key] = u
        psi = cache[key] @ psi
    return psi


# ----------------------------------------------------------------------------- experiment
@register
class XySkewPingpong(Experiment):
    """Sub-ns XY line skew at a sensor qubit from a DRAG-like ping-pong."""

    name: ClassVar[str] = "xy_skew_pingpong"
    description: ClassVar[str] = (
        "Arrival-time skew between two xy lines at a sensor qubit: the source line plays at the sensor's "
        "frequency through crosstalk against an equal opposite Y part on the sensor's line; a ping-pong "
        "accumulates the skew's DRAG-like residual. mode='calibrate' measures the crosstalk first "
        "(optionally with null and per-line sign-flip checks)."
    )
    maturity: ClassVar[str] = "contrib"
    Parameters: ClassVar[type] = XySkewPingpongParameters
    Result: ClassVar[type] = XySkewPingpongResult
    Contract: ClassVar[DatasetContract] = XySkewPingpongContract(
        sweeps=("tau_sw_ns", "repetitions", "kflip"),
        sweep_units=("ns", "", ""),
        variables=("I", "Q"),
        alt_variables=(*POPULATION_ALT, ("I",)),
    )

    params: XySkewPingpongParameters

    def define_sweep(self) -> dict[str, np.ndarray]:
        p = self.params
        if p.mode == "calibrate":
            return {
                "condition": np.asarray(p.cal_conditions(), dtype=str),
                "repetitions": np.asarray(p.repetitions, dtype=int),
                "basis": np.asarray(BASES, dtype=str),
            }
        taus = p.tau_sw_ns[:1] if p.debug_timestamps else p.tau_sw_ns
        return {
            "tau_sw_ns": np.asarray(taus, dtype=float),
            "repetitions": np.asarray(p.repetitions, dtype=int),
            "kflip": np.asarray(p.kflips(), dtype=int),
        }

    def simulate(self, coords: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
        p = self.params
        rng = np.random.default_rng(stable_seed("xy_skew_pingpong", p.sensor_qubit, p.mode))
        x180, y180 = _sim_pulses()
        ox = np.real(x180).astype(complex)
        c_true = SIM_C_AMP * np.exp(1j * SIM_C_AXIS)
        if p.mode == "calibrate":
            pre = {"z": np.zeros(16, complex), "x": -0.5 * y180, "y": 0.5 * x180}
            n1, n2 = null_waveforms(x180, y180, k=p.cal_null_k, c_amp=p.c_amp, via_phase=p.via_phase)
            fl1, _ = null_waveforms(x180, y180, k=p.k, c_amp=p.c_amp, via_phase=p.via_phase)
            _, fl2 = scan_waveforms(x180, y180, k=p.k, c_amp=p.c_amp, via_phase=p.via_phase,
                                    tau_ns=p.tau_sw_ns[p.flip_tau_index()], pad=p.pad_ns, kflip=1, sign=1)
            trains = {
                "own_x": lambda n: [p.cal_own_scale * x180] * n,
                "own_y": lambda n: [p.cal_own_scale * y180] * n,
                "via_0": lambda n: [c_true * p.cal_via_scale * ox] * n,
                "via_90": lambda n: [c_true * p.cal_via_scale * 1j * ox] * n,
                NULL_CONDITION: lambda n: [n1 + c_true * n2] * n,
                "l1_flip": lambda n: [fl1 * (1 if j % 2 == 0 else -1) for j in range(n)],
                "l2_flip": lambda n: [c_true * fl2 * (1 if j % 2 == 0 else -1) for j in range(n)],
            }
            out = np.zeros((len(coords["condition"]), len(coords["repetitions"]), len(coords["basis"])))
            for ic, cond in enumerate(coords["condition"]):
                for ir, reps in enumerate(coords["repetitions"]):
                    for ib, b in enumerate(coords["basis"]):
                        psi = _sim_evolve(trains[str(cond)](int(reps)) + [pre[str(b)]])
                        out[ic, ir, ib] = abs(psi[1]) ** 2
        else:
            # truth: L2's leak arrives SIM_SKEW_NS late; c is known to 5% / 3 deg
            c_err = 1.05 * np.exp(1j * np.deg2rad(3.0))
            y90 = 0.5 * y180
            out = np.zeros((len(coords["tau_sw_ns"]), len(coords["repetitions"]), len(coords["kflip"])))
            for it, tau in enumerate(coords["tau_sw_ns"]):
                for ik, kflip in enumerate(coords["kflip"]):
                    segs = {}
                    for sign in (1, -1):
                        l1, _ = scan_waveforms(x180, y180, k=p.k, c_amp=1.0, via_phase=0.0, tau_ns=0.0, pad=p.pad_ns,
                                               kflip=int(kflip), sign=sign)
                        _, l2 = scan_waveforms(x180, y180, k=p.k, c_amp=1.0, via_phase=0.0,
                                               tau_ns=float(tau) + SIM_SKEW_NS, pad=p.pad_ns, kflip=int(kflip),
                                               sign=sign)
                        segs[sign] = l1 + c_err * 1j * l2
                    for ir, reps in enumerate(coords["repetitions"]):
                        psi = _sim_evolve([y90] + [segs[1], segs[-1]] * int(reps))
                        out[it, ir, ik] = abs(psi[1]) ** 2
        noisy = np.clip(out + rng.normal(0, 0.005, out.shape), 0.0, 1.0)
        dims = ("target", *coords.keys())
        data = np.repeat(noisy[None, ...], max(1, len(p.targets)), axis=0)
        if p.use_state_discrimination:
            return {"population": (dims, data)}
        return {"I": (dims, data), "Q": (dims, np.zeros_like(data))}

    def estimate(self) -> XySkewPingpongResult:
        assert self.dataset is not None, "run() populates self.dataset before estimate()"
        from scqat.estimators.xy_skew_pingpong import XySkewPingpongEstimator

        from .._scqat import per_qubit_results

        p = self.params
        sensor = p.sensor_qubit
        result = XySkewPingpongResult()
        ds = self.dataset
        if "population" not in ds.data_vars:
            result.fit[sensor] = {"success": False, "reason": "needs use_state_discrimination (population)"}
            result.outcomes[sensor] = Outcome.FAILED
            return result
        targets = [str(t) for t in ds["target"].values]
        ds = ds.sel(target=[sensor if sensor in targets else targets[0]])
        keep = [v for v in ("population",) if v in ds.data_vars]
        if p.mode == "calibrate":
            kwargs = dict(cal_own_scale=p.cal_own_scale, cal_via_scale=p.cal_via_scale, cal_null_k=p.cal_null_k,
                          c_amp_played=p.c_amp, via_phase_played=p.via_phase, flip_k=p.k)
        else:
            kwargs = dict(fit_window_ns=p.fit_window_ns)
        estimator = XySkewPingpongEstimator()
        fits = per_qubit_results(ds[keep], estimator, self.artifact_dir, **kwargs)
        for name, res in fits.items():
            meta = estimator.extract_metadata(res)
            meta["mode"] = p.mode
            ts = {v: [int(x) for x in self.dataset[v].values] for v in ("ts_y90", "ts_l1", "ts_l2")
                  if v in self.dataset.data_vars}
            if ts:
                meta["timestamps"] = ts
            result.fit[name] = meta
            result.outcomes[name] = Outcome.SUCCESSFUL if res.get("success") else Outcome.FAILED
        return result

    def probe(self):  # pragma: no cover - driver half
        raise NotImplementedError("a driver backend supplies probe()")
