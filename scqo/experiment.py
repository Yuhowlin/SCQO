"""Experiment — physics half + backend half, over the greenfield model.

Port of :mod:`scqo.experiment`: same lifecycle (``define_sweep`` ->
acquire -> contract -> ``estimate`` -> ``update``), same driver story
(a backend subclass adds only ``probe``). What changed is the DEVICE
SURFACE update() and anchors address:

* knobs live on channels — ``self.device.channel(q, "readout")`` is the
  target's default readout channel view; composites via ``component``;
* facts live on modes — ``self.device.component(self.device.resonator_of(q))``
  for the resonator satellites;
* gating is kind-based: ``target_kinds`` (mode kinds, or composite kinds)
  plus ``required_operations`` checked against the roster's derived (modes)
  or declared (composites) operation sets.

Parameters/Result/DatasetContract are the model-neutral survivors and are
imported unchanged.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, ClassVar

import numpy as np
import xarray as xr

from .contract import DatasetContract
from .parameters import Parameters
from .result import Result
from .catalog import QUBIT_LIKE
from .design import Design, seed_source

#: Which tier answered an :meth:`Experiment.fact_sourced` read. Only
#: ``FACT_MEASURED`` is a measurement of THIS chip; the other two are nominal, so
#: a caller may pin the first as a fit constant and must not pin the others.
FACT_MEASURED = "measured"   # stored in physical.json
FACT_DESIGN = "design"       # declared in design.toml (also tags the run "seeded:")
FACT_DEFAULT = "default"     # nothing known — the caller's code default


class Experiment(ABC):
    """Base class for every experiment (greenfield surface)."""

    name: ClassVar[str]
    description: ClassVar[str]
    Parameters: ClassVar[type[Parameters]]
    Result: ClassVar[type[Result]]
    Contract: ClassVar[DatasetContract]
    #: entity kinds the targets must have (roster-validated pre-probe):
    #: mode kinds for single-qubit experiments, composite kinds for pairs.
    target_kinds: ClassVar[tuple[str, ...]] = QUBIT_LIKE
    #: operations every target must carry — DERIVED from wiring for modes,
    #: DECLARED for composites.
    required_operations: ClassVar[tuple[str, ...]] = ()

    @classmethod
    def validate_targets(cls, roster, targets: list[str]) -> list[str]:
        """Extra per-experiment pre-probe gating, beyond kinds + operations
        (e.g. pair_zz_coupler requires a tracked coupler with a flux
        channel). Return problem strings; [] when clear. Runs inside the
        session's machine-readable gate — before any hardware."""
        return []
    #: attach the stored |0>/|1> readout blob centers (the readout channel's
    #: pos_* monitors) to the acquired dataset as per-target ``ref_pos_*``
    #: variables. Set True only by experiments whose estimator consumes them.
    attach_readout_positions: ClassVar[bool] = False

    def __init__(self, backend, params: Parameters) -> None:
        self.backend = backend
        self.params = params
        #: run tags appended by design-seeded anchors ("seeded:<entity>.<field>").
        self.seed_tags: list[str] = []
        #: device surface experiments read/write — the Session swaps in the
        #: RecordingDevice (live) or the SuggestionCapture (around update()).
        self.device = backend.device
        #: the datasheet for anchor/fact fallbacks (set by the Session).
        self.design: Design = Design({})
        #: the physical-fact store (physical.json), set by the Session; None when
        #: an experiment runs standalone (tests) — `fact()` then skips that tier.
        self.physical = None
        self.sweep_axes: dict[str, np.ndarray] = {}
        self.dataset: xr.Dataset | None = None
        self.result: Result | None = None
        #: where estimate() writes analysis artifacts (scqat metadata/figures).
        self.artifact_dir: Path | None = None

    # ------------------------------------------------------------------ physics
    @abstractmethod
    def define_sweep(self) -> dict[str, np.ndarray]:
        """Return the swept axes as ``{axis_name: 1d array}``."""

    @abstractmethod
    def estimate(self) -> Result:
        """Fit ``self.dataset`` and return a structured :class:`Result`."""

    def update(self) -> None:
        """Write fitted quantities back into ``self.device``. Default: no-op."""

    def simulate(self, coords: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
        """Synthetic ``{var: ndarray}`` of shape ``(n_targets, *sweep)``."""
        raise NotImplementedError(f"{type(self).__name__} provides no simulator.")

    # ------------------------------------------------------------------ anchors
    def anchor(self, name: str, field: str) -> float:
        """The sweep anchor for one knob: the STANDING value if set, else the
        DESIGN fallback via the field's ``design_source`` hop. ``name`` takes
        the qubit-closure sugar (``anchor("q1", "readout_freq_hz")`` reads
        q1_ro). A design-seeded anchor tags the run
        ``"seeded:<entity>.<field>"``. Raises when neither exists — a clear
        bring-up instruction beats a sweep around garbage."""
        roster = self.device.roster
        entity, _spec = roster.resolve_field(name, field)
        view = self.device.component(entity)
        try:
            value = getattr(view, field)
        except (KeyError, AttributeError):
            value = None
        if value is not None:
            return float(value)
        source = seed_source(roster, self.design, entity, field)
        seed = self.design.get(*source) if source is not None else None
        if seed is not None:
            tag = "seeded:{}.{}".format(*source)
            if tag not in self.seed_tags:
                self.seed_tags.append(tag)
            return float(seed)
        raise ValueError(
            f"{entity}.{field} has no standing value and no design fallback "
            f"— set it (scqo set {entity}.{field}=...) or declare a design "
            f"value in design.toml")

    def fact(self, name: str, field: str, default: float | None = None) -> float | None:
        """Read a physical FACT with the precedence stored value (physical.json)
        -> design.toml -> ``default``. See :meth:`fact_sourced` for where the
        value came from, which callers need whenever a MEASURED value may be
        trusted further than a nominal one."""
        return self.fact_sourced(name, field, default)[0]

    def fact_sourced(self, name: str, field: str,
                     default: float | None = None) -> tuple[float | None, str]:
        """:meth:`fact`, plus WHICH tier answered: ``FACT_MEASURED`` /
        ``FACT_DESIGN`` / ``FACT_DEFAULT``.

        The fact-side twin of :meth:`anchor` (which serves channel KNOBS and
        raises on facts): a MODE fact — a resonator's ``g_hz``, a transmon's
        ``ec_hz`` — is unreachable through the device views during ``estimate()``,
        so this reads the physical store the Session attached (``self.physical``)
        directly. ``name`` takes the qubit-closure sugar
        (``fact("q1", "g_hz")`` resolves to the attached resonator ``q1_res``;
        ``fact("q1", "ec_hz")`` to the qubit mode). Unlike ``anchor`` it never
        raises on a missing value — it degrades to ``default`` — because its
        callers have a physical code default to fall back on.

        The TIER matters, not just the number: a measured value may be pinned as
        a fit constant, while a datasheet value of the same field may only be
        seeded (see ``resonator_spectroscopy_flux``'s bare-frequency handling).

        A DESIGN-sourced value tags the run ``"seeded:<entity>.<field>"``, exactly
        as ``anchor`` does: the datasheet is a nominal number, not a measurement,
        and a fit that leaned on one must stay findable
        (``find_runs(tag="seeded:q1_res.f_bare_hz")``)."""
        roster = self.device.roster
        entity, _spec = roster.resolve_field(name, field)
        if self.physical is not None:
            stored = self.physical.get(entity, field)
            if stored is not None:
                return float(stored), FACT_MEASURED
        seed = self.design.get(entity, field)
        if seed is not None:
            tag = f"seeded:{entity}.{field}"
            if tag not in self.seed_tags:
                self.seed_tags.append(tag)
            return float(seed), FACT_DESIGN
        return default, FACT_DEFAULT

    # ------------------------------------------------------------------ backend
    @abstractmethod
    def probe(self) -> Any:
        """Compile ``params`` + device state into a native program."""

    def readout_coords(self) -> dict[str, Any]:
        """Labels for the READOUT dims this run's dataset carries (the
        contract's ``readout_dims`` — e.g. ``{"joint_state": [...]}``).

        Consumed by the simulated backend when it assembles ``simulate()``'s
        arrays into the dataset; a hardware driver labels its own output (its
        ``reduce_raw`` builds the coords via the shared capability helpers).
        Base: no readout dims."""
        return {}

    # ------------------------------------------------------------ orchestration
    #: (dataset variable, readout-channel monitor field) pairs.
    _POSITION_FIELDS: ClassVar[tuple[tuple[str, str], ...]] = (
        ("ref_pos_g_i", "pos_g_i"), ("ref_pos_g_q", "pos_g_q"),
        ("ref_pos_e_i", "pos_e_i"), ("ref_pos_e_q", "pos_e_q"),
    )

    def _attach_reference_positions(self) -> None:
        """Attach the stored readout blob centers (the readout CHANNEL's
        monitors) to the acquired dataset — an acquisition-time snapshot, so
        the persisted dataset.nc replays offline."""
        assert self.dataset is not None
        if not self.attach_readout_positions or "I" not in self.dataset.data_vars:
            return
        targets = [str(t) for t in self.dataset["target"].values]
        columns = {var: np.full(len(targets), np.nan)
                   for var, _ in self._POSITION_FIELDS}
        for k, name in enumerate(targets):
            try:
                view = self.device.channel(name, "readout")
            except Exception:
                continue
            vals = {}
            for var, field in self._POSITION_FIELDS:
                try:
                    value = getattr(view, field)
                except (KeyError, AttributeError):
                    value = None
                vals[var] = float(value) if value is not None else float("nan")
            if all(np.isfinite(v) for v in vals.values()):
                for var, _ in self._POSITION_FIELDS:
                    columns[var][k] = vals[var]
        if any(np.isfinite(col).any() for col in columns.values()):
            for var, col in columns.items():
                self.dataset[var] = ("target", col)

    def attach_acquisition_coords(self) -> None:
        """Hook: attach acquisition-time PROVENANCE coordinates to the acquired
        dataset. Base is a no-op; a capability overrides it.

        The point of the hook is that the values are snapshotted while the run's
        device state is still current, so ``dataset.nc`` stays self-describing
        after ``update()`` has moved the knobs (the discipline
        ``_attach_reference_positions`` above already follows).

        The body lives in the capability module, not here: importing
        ``scqo.experiments._capabilities`` from this module would execute
        ``scqo/experiments/__init__.py``, which imports every experiment module,
        which imports this one — a cycle. Same lazy-import rule ``_capabilities/
        flux.py`` states for ``session.py``.
        """

    def run(self) -> Result:
        """define_sweep -> acquire -> verify contract -> estimate."""
        self.sweep_axes = self.define_sweep()
        self.dataset = self.backend.acquire(self)
        self.Contract.validate(self.dataset)
        self._attach_reference_positions()
        self.attach_acquisition_coords()
        self.result = self.estimate()
        return self.result
