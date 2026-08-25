"""Doctor witnesses over the greenfield model — structured, renderer-free.

Every check returns :class:`Check` triples ``(status, topic, message)`` in
the CLI's existing shape, so ``scqo doctor`` stays a renderer and the same
witnesses are callable from tests, the viewer, and an AI loop.

The witnesses are the ones docs/greenfield-schema.md section 7 names, plus
the roster-internal ones the loader cannot enforce (it validates structure;
these judge FITNESS — an unreachable mode loads fine and is still a lab
problem). Vendor witnessing takes the driver's realized inventory and its
line->port annotation as data; the drivers supply both at the Phase-6
cutover, and every check degrades to a clear WARN when they are absent.

:func:`profile_residency_checks` is the one ENVIRONMENT witness (the
multi-account-server trap, INSTALL §1) — kept here so every doctor row
shares this renderer-free home.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import NamedTuple

from .design import Design
from .entities import Channel, Composite, Mode
from .lock import LockError, additions, verify
from .roster import Roster

OK, WARN, FAIL = "OK", "WARN", "FAIL"


class Check(NamedTuple):
    status: str
    topic: str
    message: str


# ------------------------------------------------------------------ roster

def roster_checks(roster: Roster) -> list[Check]:
    """Structure summary plus fitness warnings the loader cannot raise."""
    out: list[Check] = []
    modes = roster.modes()
    composites = roster.composites()
    channels = roster.channels()
    out.append(Check(
        OK, "roster",
        f"{len(modes)} mode(s), {len(composites)} composite(s), "
        f"{len(roster.lines())} line(s), {len(channels)} channel(s)"))

    # A mode no channel addresses cannot be driven, read, or biased: it
    # exists in the stores but no experiment can target it.
    unreachable = sorted(
        name for name, m in modes.items()
        if m.kind != "resonator" and not roster.channels_of(name))
    if unreachable:
        out.append(Check(
            WARN, "wiring",
            f"mode(s) {unreachable} carry no channel - nothing can address "
            f"them (add a rider on the line that serves them)"))

    # A tunable-coupler pair whose coupler cannot be biased is a pair whose
    # gates cannot be tuned.
    for name, c in composites.items():
        for coupler in c.roles.get("coupler", ()):
            if (coupler, "flux") not in roster.defaults:
                out.append(Check(
                    WARN, "wiring",
                    f"{name}: coupler {coupler!r} has no flux channel - its "
                    f"bias points cannot be set"))

    # Declared operations that carry no knob values are fine (they gate
    # experiments); an operation nobody declared but experiments need is
    # caught by the run gate. Retired names are reported for visibility.
    retired = sorted(n for n, e in roster.entities.items() if e.retired)
    if retired:
        out.append(Check(OK, "roster",
                         f"retired (values still resolve): {retired}"))
    return out


# ------------------------------------------------------------------ design

def design_checks(roster: Roster, design: Design,
                  physical: dict | None = None) -> list[Check]:
    """Datasheet coverage, and the design-vs-measured column when measured
    facts are supplied."""
    out: list[Check] = []
    declared = sum(len(fields) for fields in design.values.values())
    if not declared:
        return [Check(WARN, "design",
                      "design.toml declares nothing - bring-up sweeps have "
                      "no anchor and the design-vs-measured column is empty")]
    out.append(Check(OK, "design",
                     f"{declared} target(s) over {len(design.values)} "
                     f"entit(y|ies)"))
    # Qubit-like modes with no datasheet entry at all: their first sweeps
    # have nothing to center on.
    bare = sorted(
        name for name, m in roster.modes().items()
        if m.kind != "resonator" and not design.values.get(name))
    if bare:
        out.append(Check(WARN, "design",
                         f"no design targets for {bare} - bring-up sweeps "
                         f"there need a standing value first"))
    if physical:
        rows = design.compare(physical)
        measured = [(e, f, d, m) for e, f, d, m in rows if m is not None]
        out.append(Check(OK, "design",
                         f"{len(measured)}/{len(rows)} design target(s) "
                         f"measured"))
        # A frequency an order of magnitude off design is a chip-vs-roster
        # mismatch, not a tuning question.
        for entity, field, designed, actual in measured:
            if designed and abs(actual - designed) > 0.5 * abs(designed):
                out.append(Check(
                    WARN, "design",
                    f"{entity}.{field}: measured {actual:.6g} is >50% off "
                    f"the design target {designed:.6g} - check the roster "
                    f"names against the chip"))
    return out


# -------------------------------------------------------------------- lock

def lock_checks(roster: Roster, device_dir) -> list[Check]:
    """The production-cut witness: drift refuses, appends are reported."""
    try:
        drift = verify(roster, device_dir)
        grown = additions(roster, device_dir)
    except LockError as err:
        return [Check(FAIL, "lock", str(err))]
    from .lock import load

    if load(device_dir) is None:
        return [Check(OK, "lock",
                      "device is in the TRIAL phase (no components.lock) - "
                      "names are freely editable; `scqo device freeze` cuts "
                      "to append-only")]
    out = [Check(FAIL, "lock", str(d)) for d in drift]
    if not drift:
        out.append(Check(OK, "lock", "frozen roster intact"))
    if grown:
        out.append(Check(OK, "lock", f"appended since the cut: {grown}"))
    return out


# ------------------------------------------------------------------ vendor

def vendor_checks(roster: Roster, inventory: dict | None) -> list[Check]:
    """Roster-vs-vendor witness: which knob-carrying entities the backend
    actually realizes. ``inventory`` is the driver's ``components()`` map
    (name -> ComponentInfo); None = the backend declares none."""
    if inventory is None:
        return [Check(WARN, "vendor",
                      "backend declares no inventory - roster names cannot "
                      "be cross-checked against the instrument")]
    expected = {name for name, e in roster.entities.items()
                if isinstance(e, (Channel, Composite))}
    realized = set(inventory)
    out: list[Check] = []
    missing = sorted(expected - realized)
    if missing:
        out.append(Check(
            WARN, "vendor",
            f"roster entit(y|ies) the backend does not realize: {missing} - "
            f"their knobs cannot be pushed (wiring gap, or roster names "
            f"that do not match the vendor config)"))
    extra = sorted(realized - expected)
    if extra:
        out.append(Check(
            WARN, "vendor",
            f"backend realizes {extra}, absent from the roster - the roster "
            f"is the authority; declare them or ignore them"))
    # Kind disagreement is a hard mismatch: the same name meaning different
    # things on the two sides corrupts pushes.
    for name in sorted(expected & realized):
        info = inventory[name]
        vendor_kind = getattr(info, "kind", None)
        if vendor_kind and vendor_kind != roster.entities[name].kind:
            out.append(Check(
                FAIL, "vendor",
                f"{name}: roster says {roster.entities[name].kind!r}, "
                f"backend says {vendor_kind!r}"))
    if not out:
        out.append(Check(OK, "vendor",
                         f"{len(expected)} knob-carrying entit(y|ies) "
                         f"realized by the backend"))
    return out


def wiring_checks(roster: Roster,
                  ports: dict[str, dict] | None) -> list[Check]:
    """The line->port witness of doc section 7. ``ports`` is the setup's
    vendor wiring annotation, ``{line: {"outputs": [...], ...}}``; None =
    not supplied (the drivers add it at the Phase-6 cutover)."""
    if ports is None:
        return [Check(WARN, "wiring",
                      "setup declares no line->port annotation - shared-line "
                      "claims cannot be witnessed against the instrument")]
    out: list[Check] = []
    unknown = sorted(set(ports) - set(roster.lines()))
    if unknown:
        out.append(Check(WARN, "wiring",
                         f"port annotation names non-roster line(s): "
                         f"{unknown}"))
    for line_name in sorted(roster.lines()):
        riders = [c for c in roster.channels().values()
                  if c.line == line_name]
        entry = ports.get(line_name)
        if entry is None:
            if riders:
                out.append(Check(
                    WARN, "wiring",
                    f"line {line_name!r} carries "
                    f"{[c.name for c in riders]} but the setup declares no "
                    f"ports for it"))
            continue
        outputs = list(entry.get("outputs", []))
        kinds = {c.kind for c in riders}
        # A combined wire must be fed by enough distinct outputs to carry
        # every function riding it (MW + LF at the bias tee).
        if len(kinds) > 1 and len(outputs) < len(kinds):
            out.append(Check(
                WARN, "wiring",
                f"line {line_name!r} carries {sorted(kinds)} channels but "
                f"declares {len(outputs)} output(s) - a combined wire needs "
                f"one per function"))
        # Same-kind channels sharing a wire are multiplexed: one output,
        # distinct intermediate frequencies.
        for kind in sorted(kinds):
            same = [c.name for c in riders if c.kind == kind]
            if len(same) > 1 and len(outputs) > 1:
                out.append(Check(
                    WARN, "wiring",
                    f"line {line_name!r} multiplexes {same} but declares "
                    f"{len(outputs)} outputs - multiplexed channels share "
                    f"ONE output with distinct IFs"))
    if not out:
        out.append(Check(OK, "wiring",
                         f"{len(roster.lines())} line(s) agree with the "
                         f"setup's port annotation"))
    return out


def capability_checks(roster: Roster, inventory: dict | None) -> list[Check]:
    """Capability-by-construction, witnessed the other way round: a fixed
    transmon must have NO flux realization anywhere (the chipA regression)."""
    if inventory is None:
        return []
    out: list[Check] = []
    for name, info in sorted(inventory.items()):
        if getattr(info, "kind", None) != "flux":
            continue
        for target in getattr(info, "target", ()) or ():
            mode = roster.entities.get(target)
            if isinstance(mode, Mode) and (target, "flux") not in roster.defaults:
                out.append(Check(
                    FAIL, "capability",
                    f"backend realizes flux channel {name!r} for "
                    f"{target!r}, which the roster gives no flux channel - "
                    f"a fixed-frequency qubit with a live z element"))
    return out


def all_checks(roster: Roster, *, design: Design | None = None,
               physical: dict | None = None, device_dir=None,
               inventory: dict | None = None,
               ports: dict[str, dict] | None = None) -> list[Check]:
    """Every model-side witness, in report order."""
    out = list(roster_checks(roster))
    if design is not None:
        out += design_checks(roster, design, physical)
    if device_dir is not None:
        out += lock_checks(roster, device_dir)
    out += vendor_checks(roster, inventory)
    out += capability_checks(roster, inventory)
    out += wiring_checks(roster, ports)
    return out


# ------------------------------------------------------------- environment

_SSH_NOTE = ("INSTALL section 1 SSH-server note (shared UV_PYTHON_INSTALL_DIR + "
             "explicit patch-dir interpreter)")


def _venv_base() -> tuple[str, str]:
    """The interpreter this environment actually spawns: the running venv's
    pyvenv.cfg ``home`` (the path a uv trampoline / venv launcher
    re-executes), else ``sys._base_executable`` (== ``sys.executable``
    outside a venv)."""
    pyvenv = Path(sys.prefix) / "pyvenv.cfg"
    try:
        for line in pyvenv.read_text(encoding="utf-8", errors="replace").splitlines():
            key, sep, value = line.partition("=")
            if sep and key.strip().lower() == "home" and value.strip():
                return "pyvenv.cfg home", value.strip()
    except OSError:
        pass
    return "base interpreter", str(getattr(sys, "_base_executable", None) or sys.executable)


def _profile_owner(path: str | Path | None, home: Path) -> str | None:
    """The account whose user profile contains *path* — the path component
    right after *home*'s parent (``C:\\Users\\<owner>\\...``) — else None.
    Deriving the profiles directory from *home* keeps it OS-neutral; a home
    directly under the filesystem root (``/root``-style) witnesses nothing,
    since every absolute path would then look profile-resident."""
    if path is None:
        return None
    profiles = home.parent
    if profiles == profiles.parent:
        return None
    parts, root = Path(path).parts, profiles.parts
    norm = os.path.normcase  # case-insensitive on Windows, identity elsewhere
    if len(parts) <= len(root) or tuple(map(norm, parts[:len(root)])) != tuple(map(norm, root)):
        return None
    return parts[len(root)]


def profile_residency_checks(*, base: str | Path | None = None,
                             home: Path | None = None,
                             config_source: str | Path | None = None,
                             data_root: str | Path | None = None) -> list[Check]:
    """The multi-account-server witness (BLUEFORSAS2 incident, 2026-08-11).

    A venv whose base interpreter lives inside a user profile works for the
    installer and dies for every OTHER account before Python even starts
    ("uv trampoline failed to spawn Python child process / entity not found
    (os error 2)"), because foreign profiles are not traversable. Doctor
    necessarily runs as an account the venv works for, so the resident path
    itself is the only visible signal: ANY profile-resident base warns.
    The lab config and data_root warn only under a FOREIGN profile — the
    same one-account failure class, but a student's own ``~/.scqo`` is the
    normal dev-machine layout.

    All inputs are injectable for tests; the defaults resolve the running
    interpreter (:func:`_venv_base`) and ``Path.home()``.
    """
    home = Path.home() if home is None else home
    me = home.name
    if base is None:
        src, base = _venv_base()
    else:
        src = "base interpreter"
    out: list[Check] = []
    owner = _profile_owner(base, home)
    if owner is None:
        out.append(Check(OK, "venv base", f"{src}: {base}"))
    else:
        who = (f"this account's profile ({owner})"
               if os.path.normcase(owner) == os.path.normcase(me)
               else f"{owner}'s profile (you are {me})")
        out.append(Check(
            WARN, "venv base",
            f"{src}: {base} - inside {who}: every account that cannot traverse "
            f"that profile dies before Python starts ('uv trampoline failed to "
            f"spawn Python child process'); fix per {_SSH_NOTE}"))
    for topic, p in (("lab config", config_source), ("data_root", data_root)):
        owner = _profile_owner(p, home)
        if owner is not None and os.path.normcase(owner) != os.path.normcase(me):
            out.append(Check(
                WARN, topic,
                f"{p} lives in {owner}'s profile (you are {me}) - one-account "
                f"wiring: it resolves only while that profile is traversable; "
                f"move it outside user profiles ({_SSH_NOTE})"))
    return out
