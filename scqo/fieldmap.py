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
    catalog view (e.g. idle_flux before any flux-tunable device exists)."""

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
      (``edit`` names it).
    * ``"candidate"`` — a shared concept awaiting promotion to a neutral field;
      the entry pre-declares the neutral convention so promotion is mechanical.
    * ``"vendor"``    — permanently vendor-owned for a stated reason (gauge /
      port-shared, no declarable reference plane, or a physics-derived policy).
    * ``"unique"``    — exists on THIS backend only: any experiment touching it
      is LOCKED to this instrument. That claim lives in ``doc`` ("no <other>
      counterpart") because the driver glue tests read it out of ``doc``; a
      unique entry therefore leaves ``counterpart`` empty rather than repeating.

    ``doc`` is the only attribute a reader may assume is present. The three
    optional ones carry the OPERATIONAL half — what an operator must know before
    touching the value — which used to sit as prose mid-sentence inside ``doc``.
    Structuring it is what lets ``scqo state --fields`` answer "may I edit this,
    and what else moves if I do?" without the reader parsing English.
    """

    path: str
    unit: str
    #: what it is + its status (why untracked, or "neutral-field candidate").
    doc: str
    #: taxonomy tier — one of :data:`VENDOR_ONLY_KINDS`.
    kind: str = "vendor"
    #: names that must move, or be re-measured, in the SAME operation — the
    #: operator's checklist, since an edit ignoring them is a half-edit. Each is
    #: another key of THIS backend's inventory or a neutral catalog field; never
    #: prose, never this entry's own name. Narrower than it looks: a "realizes"
    #: relation is ``kind`` plus ``edit``, and a RANGE constraint against another
    #: value ("keep IF = RF - LO in range") stays in ``doc``. Distinct from
    #: :attr:`VendorBinding.coupled`, which names neutral fields a governed write
    #: MOVES and the ChangeRecord records; this one moves nothing.
    coupled: tuple[str, ...] = ()
    #: how to change it safely. For a ``realizer``, the governed write to use
    #: INSTEAD of a direct edit. Otherwise the direct edit: which file, which
    #: tool, and the precondition (no live session, restart kernels, re-seed
    #: after, both ports of a hardware pair). ``""`` = nothing beyond the
    #: backend's blanket rule in its own module docstring.
    edit: str = ""
    #: the other backend's equivalent — a path or name, with its unit when the
    #: unit differs. OPTIONAL and it must STAY optional: entries with no
    #: counterpart prose today are legitimate, so never test for its presence.
    #: NOT a second declaration of the ``unique`` tier — a ``vendor`` entry may
    #: state it has none without being backend-locked, and nothing derives
    #: ``kind`` from it.
    counterpart: str = ""


@dataclass(frozen=True)
class OperatorCommand:
    """One vendor OPERATOR command a backend ships — inventory only.

    The commands that do the vendor-side jobs scqo deliberately does not own:
    writing a measured filter into the vendor config, releasing a cluster whose
    locks a dead session still holds, calibrating mixers. They are NOT ``scqo``
    subcommands and never will be (``scqo run <name>`` is the single entry point,
    and a vendor-specific verb could only be refused on every other backend), so
    ``scqo -h`` cannot show them and this inventory is the only place an operator
    discovers them instead of memorizing them.

    Pure data: building the list touches no instrument and reads no vendor
    config, which is what lets ``scqo state --fields`` render it beside the
    vendor-only field inventory — the two halves of "what can I reach on THIS
    instrument that is not a scqo command".
    """

    #: short slug, unique within one backend (``"apply_distortion"``).
    name: str
    #: the exact command line, invocation form included, with ``<placeholder>``
    #: for arguments the operator must fill in. Copy-and-EDIT, not copy-and-run:
    #: the inventory has no target in hand.
    command: str
    #: what it does and WHEN you need it — the "which command do I want?" answer.
    doc: str
    #: the flags that matter, one line; ``""`` = none worth naming (``--help`` is
    #: always the full list).
    options: str = ""
    #: destructive behavior or a hard precondition; ``""`` = neither. Rendered
    #: LAST and prefixed ``CAUTION:``, because it is what must not be missed.
    caution: str = ""
