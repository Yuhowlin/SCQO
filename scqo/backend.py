"""Backend — bridges an abstract experiment to a concrete instrument (or a simulator).

The backend owns the device model and knows how to *acquire* data for an experiment.
This is the only seam where vendor APIs (qm-qua, qblox-scheduler) appear.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

import xarray as xr

from .device import DeviceModel
from .fieldmap import OperatorCommand, Unrealized, VendorBinding, VendorOnly

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

    Two more optional provenance hooks, resolved the same way and with benign
    ``{}`` defaults below: ``vendor_config_snapshot()`` (the in-memory vendor
    config as files, stored per run under ``<device>/setup_snapshots/``) and
    ``versions()`` (the driver + vendor lib versions stamped into that
    snapshot's manifest). ``release_instruments()`` is the one optional hook
    that ACTS on the instrument rather than describing it.
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

    def vendor_config_snapshot(self) -> dict[str, str]:
        """The vendor config this backend holds IN MEMORY, as ``{canonical
        filename: file text}`` serialised exactly as its ``save()`` would write
        (same file split, indent and key order; LF newlines; trailing newline).

        ``Session.run`` takes it at run START and stores it, with the context's
        scqo values, content-addressed under ``<device>/setup_snapshots/`` — the
        restorable record of what the run executed against (``scqo restore``
        turns it back into a named setup). It is taken again after the run: a
        file whose text moved is reported as ``setup_snapshot.drift`` (a
        run-scoped mutation that missed its restore). Provenance only: never
        write to disk, never touch the instrument, never raise — degrade to
        ``{}``, the default here (the simulated backend has no vendor config).
        """
        return {}

    def release_instruments(self) -> list[str]:
        """Drop any live instrument connection; return what was released.

        Called before the CLI blocks on an interactive prompt: an operator who
        walks away from "apply which updates?" must not pin the instrument.
        Safe there because a decision only writes the in-memory config tree and
        the JSON stores — the values reach hardware at the next ``acquire()``,
        which reconnects.

        The default ``[]`` is right for a backend holding nothing between
        acquisitions — the simulated one, and QM, whose ``qm_session`` closes
        the QuantumMachine in its own ``finally``. Implement it where a
        connection OUTLIVES the run. Never raise, and never connect in order to
        disconnect: a backend that never dialled returns ``[]`` without dialling.
        """
        return []

    def versions(self) -> dict[str, str]:
        """``{distribution: version}`` of the driver and its vendor libraries,
        stamped into the setup snapshot's manifest so a later ``scqo restore``
        can warn when the environment differs (a QM state file names its QUAM
        classes by dotted path). Default ``{}``; never raise.
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

    def operator_commands(self) -> tuple[OperatorCommand, ...]:
        """This backend's vendor OPERATOR commands (:class:`scqo.fieldmap.OperatorCommand`).

        Inventory only — SCQO never runs these. They are the vendor-side jobs the
        neutral core deliberately does not own (writing a measured filter into
        the vendor config, releasing a cluster's locks, calibrating mixers), so
        ``scqo -h`` cannot list them and an operator would otherwise have to
        memorize them. Rendered by ``scqo state --fields`` (``--json`` for
        machines) beside :meth:`vendor_only`, the two halves of the same
        question.

        Zero-argument ON PURPOSE, unlike the per-target ``distortion_apply_command``
        hint hook: half the inventory takes no target, and ``--fields`` is a
        schema view with none in hand. Entries carry ``<placeholder>`` arguments
        instead. Pure data — no instrument, no vendor config, no failure mode.
        Default ``()``; never raise.
        """
        return ()
