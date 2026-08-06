"""Canonical dataset contract per probing method.

Each probing method declares the dataset its probe must emit (and its estimator
consumes): the ``target`` dimension (its values are TARGET names — qubit or pair
components), the swept axis (name + unit), and the required data variables. This is the explicit API between *driving* (any instrument's probe)
and *analysis* (the one shared estimator).

It also makes "support" a single, testable property: an instrument **supports** a
method exactly when its probe emits a dataset that conforms here — at which point the
shared estimator is guaranteed to apply. Drivers can therefore certify a probe with one
call (``Experiment.Contract.validate(probe_output)``); SCQO itself enforces it at
runtime in :meth:`Experiment.run`.

The contract is deliberately SCQO-neutral (e.g. ``idle_time_ns`` in ns), independent of
any estimator's internal coordinate names; the estimator-specific renaming lives in one
place (each ``estimate()``), so probe authors never depend on SCQAT's naming.

READOUT DIMS (the unified readout schema — TUTORIAL "The readout schema"):
``sweeps`` are the PHYSICS axes. A readout form may add its own dims on top —
``joint_state`` (the joint-population label axis), ``member`` (per-member data of
a multi-qubit target), ``shot_idx`` (per-shot acquisition). Each candidate
variable set declares the readout dims its variables carry via ``readout_dims``
(primary set) / ``alt_readout_dims`` (parallel to ``alt_variables``), so one
experiment can accept e.g. an averaged ``joint_population @ (target, joint_state,
*sweeps)`` OR a per-shot ``state @ (target, member, *sweeps, shot_idx)`` — the
runtime ``readout_mode`` selects which form the probe emits.
"""

from __future__ import annotations

from dataclasses import dataclass

import xarray as xr


class ContractError(ValueError):
    """Raised when a dataset does not conform to a method's canonical contract."""


@dataclass(frozen=True)
class DatasetContract:
    """The canonical dataset a probing method's probe must emit (1..N sweep axes).

    Attributes:
        sweeps: names of the swept PHYSICS dimensions/coordinates, in canonical
            order (e.g. ``("idle_time_ns",)`` or ``("power_dbm", "detuning_hz")``).
        sweep_units: documentary units per sweep axis (e.g. ``("dBm", "Hz")``);
            not enforced (xarray coords carry no units here).
        variables: data variables every conforming dataset must contain.
        readout_dims: extra READOUT dims (beyond ``(target_dim, *sweeps)``) that
            every variable of the primary set carries — e.g. ``("joint_state",)``
            for the joint-population form. Checked with the same rigor as sweeps,
            but only when the primary set is the accepted one.
        alt_variables: alternative acceptable variable sets — the dataset conforms
            when it carries ``variables`` OR any one of these sets in full (e.g.
            ``(("population",),)`` for a probe that returns the FPGA-discriminated
            averaged population instead of I/Q). Every set is held to the same
            rigor: each named variable must exist and span exactly the set's dims.
        alt_readout_dims: readout dims per ``alt_variables`` set, parallel by
            position; shorter than ``alt_variables`` means the remaining sets
            carry no readout dims.
        target_dim: the per-target dimension/coordinate name (default ``"target"``;
            renamed from ``qubit`` at the pair cutover — the axis carries TARGET
            names, which may be qubits or pairs).
    """

    sweeps: tuple[str, ...]
    sweep_units: tuple[str, ...]
    variables: tuple[str, ...]
    readout_dims: tuple[str, ...] = ()
    alt_variables: tuple[tuple[str, ...], ...] = ()
    alt_readout_dims: tuple[tuple[str, ...], ...] = ()
    target_dim: str = "target"

    @property
    def dims(self) -> tuple[str, ...]:
        """The PHYSICS dimensions every conforming dataset must carry:
        ``(target_dim, *sweeps)``. A candidate variable set's variables span
        these plus that set's readout dims."""
        return (self.target_dim, *self.sweeps)

    def _candidate_forms(self) -> tuple[tuple[tuple[str, ...], tuple[str, ...]], ...]:
        """``((variables, readout_dims), ...)`` — primary first, then each alt
        set paired with its readout dims (missing entries mean no extra dims)."""
        alts = tuple(
            (vs, self.alt_readout_dims[i] if i < len(self.alt_readout_dims) else ())
            for i, vs in enumerate(self.alt_variables)
        )
        return ((self.variables, self.readout_dims), *alts)

    def _variable_set_problems(
        self, ds: xr.Dataset, variables: tuple[str, ...], readout_dims: tuple[str, ...]
    ) -> list[str]:
        """Problems of one candidate form: each named variable must exist and
        span exactly ``dims + readout_dims``, and each readout dim must be
        present as both a dimension and a coordinate — the same rigor for every
        alternative."""
        want = set(self.dims) | set(readout_dims)
        problems: list[str] = []
        for dim in readout_dims:
            if dim not in ds.dims:
                problems.append(f"missing readout dimension {dim!r}")
            if dim not in ds.coords:
                problems.append(f"missing readout coordinate {dim!r}")
        for var in variables:
            if var not in ds.data_vars:
                problems.append(f"missing variable {var!r}")
                continue
            if set(ds[var].dims) != want:
                problems.append(
                    f"variable {var!r} has dims {tuple(ds[var].dims)}, "
                    f"expected {(*self.dims, *readout_dims)}"
                )
        return problems

    def validate(self, ds: xr.Dataset) -> None:
        """Raise :class:`ContractError` if ``ds`` does not conform.

        Checks that ``target_dim`` and every sweep axis are present as both a dimension
        and a coordinate, and that the dataset fully carries ``variables`` OR any one
        ``alt_variables`` set — each candidate variable must exist and span exactly that
        form's dimension set (physics dims + the form's readout dims). Extra
        variables/coordinates are allowed (e.g. a probe may also emit ``Q`` for a
        method whose estimator only reads ``I``).
        """
        problems: list[str] = []
        for dim in self.dims:
            if dim not in ds.dims:
                problems.append(f"missing dimension {dim!r}")
            if dim not in ds.coords:
                problems.append(f"missing coordinate {dim!r}")

        forms = self._candidate_forms()
        set_problems = [self._variable_set_problems(ds, vs, rd) for vs, rd in forms]
        if all(set_problems):  # no candidate form fully conforms
            if len(forms) == 1:
                problems.extend(set_problems[0])
            else:
                accepted = " OR ".join(repr(vs) for vs, _ in forms)
                problems.append(
                    f"no accepted variable set conforms (accepted: {accepted}; "
                    f"found data_vars {tuple(ds.data_vars)!r}): "
                    + " | ".join(
                        f"{vs!r}: " + "; ".join(ps)
                        for (vs, _), ps in zip(forms, set_problems)
                    )
                )
        if problems:
            raise ContractError(
                f"dataset does not conform to contract (sweeps={self.sweeps!r}, "
                f"variables={self.variables}): " + "; ".join(problems)
            )

    def conforms(self, ds: xr.Dataset) -> bool:
        """Return ``True`` iff ``ds`` conforms (non-raising form of :meth:`validate`)."""
        try:
            self.validate(ds)
            return True
        except ContractError:
            return False
