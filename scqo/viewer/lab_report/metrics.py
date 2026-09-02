"""Per-qubit numbers for the lab report — GENERIC: no lab conventions here.

Consumes the setup page's row dicts (``app._param_rows``) plus campaign
statistics (:mod:`scqo.campaign_query`) and returns one flat dict per qubit.
Nothing in this module knows a sheet name, a column order or a language; that
is :mod:`~scqo.viewer.lab_report.field_dictionary` data, and keeping the two
apart is the point of the split.

Two rules this module holds, both of which the first draft got wrong and both
of which change what a reader believes about the chip:

* **A missing quantity is missing.** No value stands in for another. In
  particular ``f_01_hz`` is NOT backfilled from ``f_q_max_hz``: the catalog is
  explicit that the first is the frequency at the CURRENT idle bias and the
  second is the sweet-spot arch top (``catalog.py``, mode kind
  ``flux_transmon``). They coincide only when the qubit is parked at its sweet
  spot, and every derived quantity below — g, kappa, the dispersive shift,
  EJ/EC, f02/2, the effective temperature — is computed FROM the qubit
  frequency, so one silent substitution moves all of them.
* **A qubit is a qubit.** Channel and resonator names normalise onto their
  target (``q1_xy`` -> ``q1``), but a COMPOSITE (``q1_q2``) is a different
  entity and never folds into its first member — see
  :func:`scqo.campaign_query.normalize_target_name`.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from scqo.campaign_query import normalize_target_name, query_campaign_statistics

#: h / k_B in mK per GHz — the effective-temperature constant.
_MK_PER_GHZ = 47.9924


def _num(value: Any) -> float | None:
    """``value`` when it is a real number, else None. Booleans are NOT numbers
    (``isinstance(True, int)`` is True, which is how a flag becomes a frequency)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return None if math.isnan(value) or math.isinf(value) else float(value)


def _first(*values: Any) -> Any:
    """The first value that is not None — an explicit test, so a legitimate
    ``0.0`` counts as present. ``a or b`` reads zero as missing, which turned a
    perfectly cold qubit's thermal population into a blank cell."""
    for v in values:
        if v is not None:
            return v
    return None


def _ghz(hz: Any) -> float | None:
    v = _num(hz)
    return v / 1e9 if v is not None else None


def _us(seconds: Any) -> float | None:
    v = _num(seconds)
    return v * 1e6 if v is not None else None


def _campaign(stats: dict, *names: str) -> dict | None:
    """The first campaign statistic present under any of ``names``."""
    for n in names:
        got = stats.get(n)
        if isinstance(got, dict):
            return got
    return None


def _stat_us(stat: dict | None) -> tuple[float | None, float | None, int | None]:
    """``(value, error, n)`` in microseconds from a seconds-valued statistic."""
    if stat is None:
        return None, None, None
    return _us(stat.get("value")), _us(stat.get("error")), stat.get("n")


def qubit_sort_key(name: str):
    """``q2`` before ``q10``; un-numbered names last, alphabetically."""
    digits = "".join(c for c in name if c.isdigit())
    return (0, int(digits), "") if digits else (1, 0, name)


def discover_qubits(rows: list[dict]) -> list[str]:
    """The qubit-like targets these rows describe, sorted.

    A row's entity is normalised onto its target, so ``q1``, ``q1_xy`` and
    ``q1_res`` all name qubit ``q1``. A composite normalises to itself and is
    NOT a qubit, so ``q1_q2`` contributes nothing here rather than silently
    counting as ``q1``. An empty result stays empty: a context with no data
    reports no qubits, never an invented one.
    """
    seen: list[str] = []
    for row in rows:
        base = normalize_target_name(row.get("entity", ""))
        if base and base not in seen and base.startswith("q") and "_" not in base:
            seen.append(base)
    return sorted(seen, key=qubit_sort_key)


def effective_temperature_mk(n_th: Any, f_q_ghz: float | None,
                             n_th_err: Any = None) -> tuple[float | None, float | None]:
    """``(T_eff, sigma_T)`` in mK from a thermal population and f01.

    ``T = h f / (k_B ln(1 + 1/n))``; propagating n gives
    ``sigma_T = T sigma_n / (n (1 + n) ln(1 + 1/n))``.
    """
    n = _num(n_th)
    if n is None or n <= 0 or not f_q_ghz:
        return None, None
    denom = math.log1p(1.0 / n)
    if denom <= 0:
        return None, None
    t_mk = (_MK_PER_GHZ * f_q_ghz) / denom
    sigma = _num(n_th_err)
    t_err = t_mk * (sigma / (n * (1.0 + n) * denom)) if sigma and sigma > 0 else None
    return t_mk, t_err


def extract_chip_metrics(ctx: dict, store: Any = None, data_root: Path | None = None,
                         min_repeats: int = 2, estimator: str = "mean",
                         tags: Any = None, design: Any = None,
                         predicted_f_q: dict[str, float] | None = None) -> dict[str, Any]:
    """Per-qubit metrics for one setup (or a unified cooldown) context.

    Both optional inputs are INJECTED rather than read here, so this module
    holds no file-format knowledge — :mod:`~scqo.viewer.lab_report.sources`
    loads them:

    * ``design`` — a :class:`scqo.design.Design`, the chip datasheet, supplying
      the as-DESIGNED bare resonator frequency the report plots against the
      measured one. None simply leaves those cells empty.
    * ``predicted_f_q`` — ``{qubit: GHz}`` predicted from junction resistance.
    """
    device = ctx.get("device", "")
    cooldown = ctx.get("cooldown", "")
    state_rows = ctx.get("state_rows") or []
    phys_rows = ctx.get("physical_rows") or []

    state = {(r["entity"], r["field"]): r["value"] for r in state_rows}
    phys = {(r["entity"], r["field"]): r["value"] for r in phys_rows}
    qubits = discover_qubits(phys_rows + state_rows)

    campaign_stats = query_campaign_statistics(
        store, device=device, cooldown=cooldown or None, min_repeats=min_repeats,
        status=("complete", "running"), estimator=estimator, tags=tags,
    )

    per_qubit: dict[str, dict[str, Any]] = {}
    for q in qubits:
        res, ro, xy, z = f"{q}_res", f"{q}_ro", f"{q}_xy", f"{q}_z"
        camp = campaign_stats.get(q, {})

        f_dress_ghz = _ghz(_first(phys.get((res, "f_dress0_hz")),
                                  state.get((ro, "readout_freq_hz"))))
        f_bare_ghz = _ghz(phys.get((res, "f_bare_hz")))
        # f_01_hz ONLY — see the module docstring. drive_freq_hz is the
        # instrument twin of the same idle-point quantity, so it is a legal
        # stand-in; f_q_max_hz is a different quantity and is not.
        f_q_ghz = _ghz(_first(phys.get((q, "f_01_hz")), state.get((xy, "drive_freq_hz"))))
        f_q_max_ghz = _ghz(phys.get((q, "f_q_max_hz")))

        # Tunable = the qubit HAS a flux idle point, by value. Key presence is
        # not evidence: _param_rows emits a row for every observed field, so an
        # unset knob on an existing z channel would otherwise read TRUE.
        tunable = _num(state.get((z, "idle_flux"))) is not None or f_q_max_ghz is not None

        t1_single_us = _us(phys.get((q, "t1_s")))
        t1_us, t1_err_us, t1_n = _stat_us(_campaign(camp, "t1_s", "t1"))
        ram_single_us = _us(phys.get((q, "t2_star_s")))
        ram_us, ram_err_us, ram_n = _stat_us(_campaign(camp, "t2_star_s", "t2_star"))
        echo_single_us = _us(phys.get((q, "t2_echo_s")))
        echo_us, echo_err_us, echo_n = _stat_us(_campaign(camp, "t2_echo_s", "t2_echo"))

        eff_t1 = _first(t1_us, t1_single_us)
        eff_ram = _first(ram_us, ram_single_us)
        eff_echo = _first(echo_us, echo_single_us)
        ratio_ram = eff_ram / eff_t1 if (eff_t1 and eff_ram is not None) else None
        ratio_echo = eff_echo / eff_t1 if (eff_t1 and eff_echo is not None) else None

        fid_g, fid_e = _num(state.get((ro, "fidelity_g"))), _num(state.get((ro, "fidelity_e")))
        if fid_g is not None and fid_e is not None:
            ro_single = (fid_g + fid_e) / 2.0
        else:
            ro_single = _first(fid_g, fid_e, _num(state.get((ro, "readout_fidelity"))))
        ro_camp = _campaign(camp, "readout_fidelity", "fidelity_g")

        nth_camp = _campaign(camp, "n_th", "pop_e_prep_g")
        nth = _first(nth_camp.get("value") if nth_camp is not None else None,
                     phys.get((q, "n_th")))
        nth_err = nth_camp.get("error") if nth_camp is not None else None
        temp_mk, temp_err_mk = effective_temperature_mk(nth, f_q_ghz, nth_err)

        rb_camp = _campaign(camp, "fidelity", "rb_fidelity")
        rb_single = _first(phys.get((q, "rb_fidelity")), state.get((q, "rb_fidelity")))

        anharm_mhz = _num(phys.get((q, "anharmonicity_hz")))
        anharm_mhz = anharm_mhz / 1e6 if anharm_mhz is not None else None
        q_c, q_i = _num(phys.get((res, "q_c"))), _num(phys.get((res, "q_i")))

        g_mhz = None
        if None not in (f_dress_ghz, f_bare_ghz, f_q_ghz):
            g_mhz = 1000.0 * math.sqrt(
                abs((f_dress_ghz - f_bare_ghz) * (f_dress_ghz - f_q_ghz)))

        kappa_mhz = None
        if f_dress_ghz is not None:
            inv = (1.0 / q_c if q_c and q_c > 0 else 0.0) + (1.0 / q_i if q_i and q_i > 0 else 0.0)
            kappa_mhz = 1000.0 * f_dress_ghz * inv if inv > 0 else None

        delta_fr_mhz = None
        if None not in (f_q_ghz, f_bare_ghz, g_mhz, anharm_mhz) and anharm_mhz:
            delta = (f_q_ghz - f_bare_ghz) * 1000.0
            den = delta * (delta + anharm_mhz)
            delta_fr_mhz = (2.0 * g_mhz ** 2 * anharm_mhz) / den if den else None

        ej_ec = None
        if f_q_ghz is not None and anharm_mhz is not None and anharm_mhz < 0:
            ej_ec = ((1000.0 * f_q_ghz / -anharm_mhz) + 1.0) ** 2 / 8.0

        f02_half_ghz = (f_q_ghz + anharm_mhz / 2000.0) if (
            f_q_ghz is not None and anharm_mhz is not None) else None

        per_qubit[q] = {
            "design_f_bare_ghz": _ghz(design.get(res, "f_bare_hz")) if design else None,
            "f_dress_ghz": f_dress_ghz, "f_bare_ghz": f_bare_ghz,
            "f_q_ghz": f_q_ghz, "f_q_max_ghz": f_q_max_ghz,
            "f02_half_ghz": f02_half_ghz, "tunable": tunable,
            "t1_single_us": t1_single_us, "t1_mean_us": t1_us,
            "t1_std_us": t1_err_us, "t1_n": t1_n,
            "t2_ramsey_single_us": ram_single_us, "t2_ramsey_mean_us": ram_us,
            "t2_ramsey_std_us": ram_err_us, "t2_ramsey_n": ram_n,
            "t2_echo_single_us": echo_single_us, "t2_echo_mean_us": echo_us,
            "t2_echo_std_us": echo_err_us, "t2_echo_n": echo_n,
            "t2_star_over_t1": ratio_ram, "t2_echo_over_t1": ratio_echo,
            "readout_fidelity_single": ro_single,
            "readout_fidelity_mean": ro_camp.get("value") if ro_camp else None,
            "readout_fidelity_std": ro_camp.get("error") if ro_camp else None,
            "thermal_population_mean": nth,
            "thermal_population_std": nth_err,
            "temperature_mk": temp_mk, "temperature_err_mk": temp_err_mk,
            "rb_fidelity_single": rb_single,
            "rb_fidelity_mean": rb_camp.get("value") if rb_camp else None,
            "rb_fidelity_std": rb_camp.get("error") if rb_camp else None,
            "anharmonicity_mhz": anharm_mhz, "q_c": q_c, "q_i": q_i,
            "g_mhz": g_mhz, "kappa_mhz": kappa_mhz,
            "delta_fr_mhz": delta_fr_mhz, "ej_ec": ej_ec,
            "r_to_fq": _num((predicted_f_q or {}).get(q)),
        }

    return {
        "device": device, "cooldown": cooldown,
        "setup_name": ctx.get("setup_name", ""), "cycle": ctx.get("cycle") or {},
        "qubits": qubits, "per_qubit": per_qubit,
    }
