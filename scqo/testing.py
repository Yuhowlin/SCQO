"""In-memory device + simulated backend + the demo device — greenfield.

These let the whole model run end-to-end with no instrument and no vendor
library — unit tests, demos, AI dry-runs. A driver's real backend is a
drop-in replacement for :class:`SimulatedBackend`; a real vendor tree is a
drop-in for :class:`InMemoryDevice`.

The demo device is generated as REAL components.toml / design.toml text and
parsed through the real loaders — every test that uses it exercises rider
expansion, design validation, and seeding, not hand-built entity objects.
"""

from __future__ import annotations

import xarray as xr

from .design import Design, parse_design
from .device import CompositeView, DeviceModel, EntityView
from .entities import Composite
from .roster import Roster, parse_components

#: Demo design targets grow with the qubit index — the LATER qubit is the
#: pair's design-nominal "high" (the roles are bound against these).
_F01 = 3.8e9
_F01_STEP = 0.15e9
_FR = 5.95e9
_FR_STEP = 0.1e9


def demo_components(qubits: tuple[str, ...] = ("q0", "q1"), *,
                    pair: bool = True, tunable: bool = False) -> Roster:
    """The chipT-shaped demo roster in the greenfield schema: one
    multiplexed feedline, a dedicated drive wire each, and (default) a
    qubit_pair over the first two qubits — so every core test exercises
    rider minting, multiplexed readout, and pair plumbing.

    ``tunable=True`` is the flux-demo variant: flux_transmon qubits with a
    z wire each, plus a tracked coupler mode with its own flux wire on the
    pair — the substrate for the flux/pair experiment tests."""
    kind = "flux_transmon" if tunable else "transmon"
    lines = [f"[modes.{q}]\nkind = \"{kind}\"" for q in qubits]
    if pair and len(qubits) >= 2:
        a, b = sorted(qubits[:2])
        coupler = ""
        if tunable:
            lines.append(f"[modes.{a}_{b}_c]\nkind = \"flux_transmon\"")
            coupler = f"coupler    = \"{a}_{b}_c\"\n"
        lines.append(
            f"[composites.{a}_{b}]\n"
            f"kind       = \"qubit_pair\"\n"
            f"high       = \"{qubits[1]}\"\n"     # design f grows with index
            f"low        = \"{qubits[0]}\"\n"
            + coupler +
            f"operations = [\"iswap\"]")
        if tunable:
            lines.append(f"[lines.zc]\nflux = [\"{a}_{b}_c\"]")
    readout = ", ".join(f'"{q}"' for q in qubits)
    lines.append(f"[lines.fl]\nreadout = [{readout}]")
    lines.extend(f"[lines.{q}_xyl]\ndrive = [\"{q}\"]" for q in qubits)
    if tunable:
        lines.extend(f"[lines.{q}_zl]\nflux = [\"{q}\"]" for q in qubits)
    return parse_components("schema = 3\n" + "\n".join(lines))


def demo_design(roster: Roster,
                qubits: tuple[str, ...] = ("q0", "q1")) -> Design:
    """Design targets matching :func:`demo_components` — context-free per
    kind (f_01 for fixed transmons, f_q_max for flux-tunables), f_r per
    minted resonator — validated through the real loader."""
    blocks = ["schema = 1"]
    for i, q in enumerate(qubits):
        field = ("f_q_max_hz" if roster.entities[q].kind == "flux_transmon"
                 else "f_01_hz")
        blocks.append(f"[{q}]\n{field} = {_F01 + i * _F01_STEP:.6g}")
        blocks.append(f"[{q}_res]\nf_r_hz = {_FR + i * _FR_STEP:.6g}")
    return parse_design("\n".join(blocks), roster)


def demo_vendor_state(roster: Roster, design: Design) -> dict:
    """A plausible vendor tree for the demo device: channel knobs seeded
    from the design (the shape a real instrument config would carry)."""
    from .design import seed_value

    state: dict[str, dict] = {}
    for name, e in roster.channels().items():
        fields: dict = {}
        for f, spec in roster.fields_of(name).items():
            if spec.role != "knob":
                continue
            seed = seed_value(roster, design, name, f)
            if seed is not None:
                fields[f] = seed
        if e.kind == "drive":
            fields.setdefault("pi_amp", 0.1)
            # seeded at half the pi amplitude, which is where a real config starts
            # before qubit_deterministic_benchmarking calibrates the pi/2 in its own
            # right — the knob is INDEPENDENT, not derived, so it needs a real value
            fields.setdefault("pi_amp_x90", 0.05)
            fields.setdefault("drive_amp", 0.05)
            fields.setdefault("drive_power_dbm", -13.0)
            fields.setdefault("thermalization_time_s", 200e-6)
            # x180 length — the triangle half-width qubit_xyz_delay's fit needs
            fields.setdefault("pi_duration_s", 40e-9)
        elif e.kind == "readout":
            fields.setdefault("readout_amp", 0.08)
            fields.setdefault("readout_power_dbm", -30.0)
            fields.setdefault("readout_duration_s", 8.0e-7)
            fields.setdefault("readout_integration_s", 8.0e-7)
        elif e.kind == "flux":
            fields.setdefault("idle_flux", 0.0)
            fields.setdefault("flux_delay_s", 0.0)
        state[name] = fields
    for name, e in roster.composites().items():
        state[name] = {}
    return state


def demo_device(qubits: tuple[str, ...] = ("q0", "q1"), *, pair: bool = True,
                tunable: bool = False):
    """The whole offline device in one call: ``(roster, design, vendor)``
    with the vendor tree seeded from the datasheet — what a test needs to
    build a Session against the simulated backend."""
    roster = demo_components(qubits, pair=pair, tunable=tunable)
    design = demo_design(roster, qubits)
    return roster, design, InMemoryDevice(roster,
                                          demo_vendor_state(roster, design))


class _InMemoryChannel(EntityView):
    """A channel view backed by a plain dict — tolerates ANY field key so
    the vendor stand-in stays schema-agnostic (the RecordingDevice above it
    enforces the catalog)."""

    def __init__(self, name: str, kind: str, state: dict) -> None:
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "_state", state)

    def __getattr__(self, field: str):
        state = object.__getattribute__(self, "_state")
        if field in state:
            return state[field]
        raise AttributeError(field)

    def __setattr__(self, field: str, value) -> None:
        # Deliberately permissive: the simulated vendor accepts whatever the
        # neutral layer pushes (power knobs stay uncoupled from amps here —
        # no output chain exists).
        self._state[field] = ([float(v) for v in value]
                              if isinstance(value, list) else float(value))


class _InMemoryComposite(CompositeView):
    def __init__(self, name: str, kind: str, state: dict) -> None:
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "_state", state)

    def read_knob(self, field: str):
        return self._state.get(field)

    def write_knob(self, field: str, value) -> None:
        self._state[field] = ([float(v) for v in value]
                              if isinstance(value, list) else float(value))


class InMemoryDevice(DeviceModel):
    """A DeviceModel held entirely in memory (no vendor files).

    ``roster`` tells it which entities are composites (generic knob surface)
    versus channels (attribute surface); entities absent from ``state`` are
    unrealized (KeyError — exactly a real vendor's behavior for unwired
    roster entries)."""

    def __init__(self, roster: Roster, state: dict) -> None:
        self._roster = roster
        self._state = {name: dict(fields) for name, fields in state.items()}

    def component(self, name: str) -> EntityView:
        state = self._state[name]  # KeyError = vendor does not realize it
        e = self._roster.entities[name]
        if isinstance(e, Composite):
            return _InMemoryComposite(name, e.kind, state)
        return _InMemoryChannel(name, e.kind, state)

    def save(self) -> None:  # nothing to persist
        pass

    def snapshot(self) -> dict:
        return {name: dict(fields) for name, fields in self._state.items()}


class SimulatedBackend:
    """Backend that fabricates data from ``experiment.simulate`` (never
    calls ``probe``) — the greenfield twin of the proven simulated backend;
    the acquire contract is model-neutral and unchanged."""

    def __init__(self, device) -> None:
        self._device = device

    @property
    def device(self):
        return self._device

    def acquire(self, experiment) -> xr.Dataset:
        sweep = experiment.sweep_axes
        raw = experiment.simulate(sweep)
        targets = experiment.params.targets
        default_dims = ["target", *sweep.keys()]
        # readout_coords(): labels for the READOUT dims the contract adds on
        # top of the physics sweeps (joint_state / member / shot_idx) — the
        # simulated twin of a driver labeling its own reduce_raw output.
        # getattr, not a direct call: duck-typed experiment stubs (tests) need
        # not carry the hook — same tolerance as the drivers' reduce_raw.
        readout = getattr(experiment, "readout_coords", None)
        coords = {"target": list(targets), **sweep,
                  **(readout() if readout is not None else {})}
        # A simulate() var is EITHER a bare ndarray spanning
        # (target, *sweeps) — the common case — OR a (dims_tuple, ndarray)
        # when the var spans only a SUBSET of the axes or carries readout dims.
        data_vars = {}
        for var, val in raw.items():
            if (isinstance(val, tuple) and len(val) == 2
                    and isinstance(val[0], tuple)):
                data_vars[var] = val
            else:
                data_vars[var] = (default_dims, val)
        return xr.Dataset(data_vars, coords=coords)
