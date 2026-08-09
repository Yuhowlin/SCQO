"""Backend — bridges an abstract experiment to a concrete instrument (or a simulator).

The backend owns the device model and knows how to *acquire* data for an experiment.
This is the only seam where vendor APIs (qm-qua, qblox-scheduler) appear.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

import xarray as xr

from .device import DeviceModel
from .fieldmap import Unrealized, VendorBinding, VendorOnly

if TYPE_CHECKING:
    from .experiment import Experiment


class PreviewWarning(UserWarning):
    """Non-fatal degradation during a backend's ``preview`` (e.g. one
    best-effort artifact failed to render). ``Session.preview`` collects
    these into the result's ``warnings`` list; anything fatal raises
    instead and fails the preview."""


class Backend(ABC):
    """An instrument adapter.

    Optional duck-typed hook —
    ``preview(experiment, out_dir, **options) -> list[Path]``:
    render the experiment's vendor-native sequence to files in ``out_dir``
    without executing it on the qubits (no state writes, nothing saved; a
    backend MAY consult vendor tooling such as a gateway simulator, degrading
    to its offline artifacts with a :class:`PreviewWarning` when that tooling
    is unreachable). Deliberately NOT declared on this ABC: backends need not
    subclass it (``Session`` resolves the hook via ``getattr``, exactly like
    ``power_context``), and preview has no benign empty default — a backend
    that cannot render must REFUSE BY NAME with a ``ValueError``. Contract:
    the caller has already set ``experiment.sweep_axes``; the backend builds
    the sequence exactly as ``acquire`` would, creates ``out_dir`` only after
    its refusal checks, and returns the written paths in display order.
    ``options`` are backend-specific keywords (the QM backend's
    ``simulate_ns``/``no_simulate``); accept ``**options`` and refuse an
    option you cannot honour by name — never silently. Best-effort artifacts
    signal failure with :class:`PreviewWarning`.
    """

    @property
    @abstractmethod
    def device(self) -> DeviceModel:
        """The device model whose state experiments read and update."""

    @abstractmethod
    def acquire(self, experiment: "Experiment") -> xr.Dataset:
        """Realize and execute ``experiment`` on this backend, returning labelled data.

        Hardware backends call ``experiment.probe()`` to produce a native program
        (a QUA program or a Qblox ``Schedule``), run it, and return the result as an
        ``xarray.Dataset`` with a ``qubit`` dimension plus the experiment's sweep axes.
        The simulated backend ignores ``probe`` and calls ``experiment.simulate`` instead.
        """

    def power_context(self, qubits: list[str]) -> dict:
        """Raw per-qubit READOUT and DRIVE chain values at run end.

        Vendor-specific keys behind ``readout_power_dbm`` (QM:
        full_scale_power_dbm + readout amplitude + readout LO; Qblox:
        output_att + pulse_amp + the nominal full scale + readout LO) and
        ``drive_power_dbm`` (QM: drive_full_scale_power_dbm + saturation_amp +
        drive LO; Qblox: drive_output_att_db + spec_amp + drive LO), stamped
        into each run record as PROVENANCE ONLY — never re-applied. The default
        is ``{}``: the simulated backend has no output chain.
        """
        return {}

    def field_bindings(self) -> dict[str, dict[str, VendorBinding]]:
        """This backend's declared field -> vendor-parameter catalog, PER CATEGORY.

        ``{category: {field: VendorBinding}}`` — pure metadata
        (:mod:`scqo.fieldmap`): where each pushed field lives on the vendor
        config, in what unit, converted how — as a DESCRIPTION; the executable
        conversion is the driver's component view setter. Rendered by
        ``scqo state --fields``; driver tests pin, per category,
        ``bindings | unrealized == pushed_fields(category)``. Default ``{}``.
        """
        return {}

    def unrealized(self) -> dict[str, dict[str, "Unrealized"]]:
        """Pushed neutral fields THIS backend cannot realize, per category
        (:class:`scqo.fieldmap.Unrealized`) — declared, never silent. Default ``{}``.
        """
        return {}

    def vendor_only(self) -> dict[str, VendorOnly]:
        """Calibration-relevant vendor parameters with NO neutral counterpart (yet).

        Inventory only — SCQO never reads or writes these; they stay vendor-owned.
        Rendered by ``scqo state --fields`` so backend-unique knobs are visible,
        and doubling as the backlog of neutral-field candidates. Default ``{}``.
        """
        return {}
