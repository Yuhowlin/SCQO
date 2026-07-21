"""Cross-backend field catalog — driver-declared, pure-data metadata.

The knowledge "which vendor parameter realizes which neutral field, in what unit,
converted how" is declared by each DRIVER as data: one :class:`VendorBinding` per
pushed field, plus a :class:`VendorOnly` inventory for the calibration-relevant
vendor parameters that have no neutral counterpart (yet). The EXECUTABLE
conversion is deliberately NOT here — it stays in the driver's QubitView property
setters, which are the hardware-tested single source of truth (e.g. the
``readout_power_dbm`` chain solves); ``convert`` is a description for humans and
the AI loop, never evaluated.

Rendered live by ``scqo state --fields`` (``--json`` for machines) through
:meth:`scqo.backend.Backend.field_bindings` / :meth:`~scqo.backend.Backend.vendor_only`.
Each driver's test suite asserts its bindings cover exactly
:data:`scqo.config.PUSHED_FIELDS`, so the catalog cannot silently drift from the
implementation the way the old docstring tables could. The vendor-only inventory
doubles as the visible backlog of neutral-field candidates (the readout pulse
length sat there until ``readout_duration_s`` was promoted; the pi-pulse length
sits there today).

All strings here reach lab consoles via the CLI table — keep them ASCII.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class VendorBinding:
    """Where (and how) ONE neutral field lives on ONE backend's vendor config."""

    #: vendor location, human-readable (e.g. ``"q.resonator.RF_frequency"``).
    path: str
    #: vendor-side unit (``"Hz"``, ``"ns"``, ``"dBm + amp"``, ``""`` = dimensionless).
    unit: str
    #: neutral -> vendor conversion, described; ``""`` = direct, same unit.
    convert: str = ""
    #: neutral fields that move as side effects of writing this one (the
    #: ChangeRecord ``coupled_to`` mechanism records them at write time).
    coupled: tuple[str, ...] = ()
    #: quantization / range constraints / caveats.
    note: str = ""


#: Valid :attr:`VendorOnly.kind` values (the vendor tier of the placement rule).
VENDOR_ONLY_KINDS = ("realizer", "candidate", "vendor", "unique")


@dataclass(frozen=True)
class Unrealized:
    """A pushed neutral field THIS backend cannot realize (declared, not silent).

    The per-category successor of the bindings==pushed invariant: driver tests
    pin ``bindings(cat) | unrealized(cat) == pushed_fields(cat)``, and pushes of
    an unrealized field are skipped with the reason available to doctor and the
    catalog view (e.g. idle_flux_v before any flux-tunable device exists)."""

    category: str
    field: str
    reason: str


@dataclass(frozen=True)
class VendorOnly:
    """A calibration-relevant vendor parameter operators may need to locate directly.

    Inventory only — SCQO's catalog never reads or writes these; they live in the
    vendor config. Listing them makes backend-unique knobs visible
    (`scqo state --fields`) instead of implicit in the vendor JSON. ``kind`` names
    the entry's tier under the placement rule (`scqo state --rule`; TUTORIAL
    "Where does a value live?"):

    * ``"realizer"``  — realizes a TRACKED neutral field: a direct edit silently
      de-calibrates it; the governed write is ``scqo set QUBIT.<neutral>=...``
      (the ``doc`` names it).
    * ``"candidate"`` — a shared concept awaiting promotion to a neutral field;
      the entry pre-declares the neutral convention so promotion is mechanical.
    * ``"vendor"``    — permanently vendor-owned for a stated reason (gauge /
      port-shared, no declarable reference plane, or a physics-derived policy).
    * ``"unique"``    — exists on THIS backend only: any experiment touching it
      is LOCKED to this instrument (``doc`` says "no <other> counterpart").
    """

    path: str
    unit: str
    #: what it is + its status (why untracked, or "neutral-field candidate").
    doc: str
    #: taxonomy tier — one of :data:`VENDOR_ONLY_KINDS`.
    kind: str = "vendor"
