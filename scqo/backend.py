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


class Backend(ABC):
    """An instrument adapter."""

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
        """Raw per-qubit output-chain values behind ``readout_power_dbm`` at run end.

        Vendor-specific keys (QM: full_scale_power_dbm + readout amplitude +
        readout LO; Qblox: output_att + pulse_amp + the nominal full scale +
        readout LO), stamped into each run record as PROVENANCE ONLY — never
        re-applied. The default is ``{}``: the simulated backend has no output
        chain.
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
