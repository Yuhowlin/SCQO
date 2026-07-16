"""Session building for the CLI: device -> cycle -> named setup -> backend.

Real instruments are served by DRIVER packages that register a factory under the
``scqo.backends`` entry-point group (name = the backend family)::

    [project.entry-points."scqo.backends"]
    qblox = "lchqb.scqo_backend:build_backend"

A factory is ``build_backend(cfg: LabConfig, setup: dict) -> Backend`` — ``setup``
is the device's SELECTED named setup record from its cooldown registry (``backend``,
optional ``note``, plus the DERIVED ``instrument_config`` vendor folder injected by
``load_cooldowns`` — ``<device>/<cooldown>/<setup>/backend_config``, never typed).
``simulated`` (demo qubits, synthetic data) is built in here, so query commands,
practice runs and CI need no driver at all.
"""

from __future__ import annotations

from importlib.metadata import entry_points
from pathlib import Path

from scqo import LabConfig, Session, load_lab_config, make_session
from scqo.backend import Backend

#: Demo device for the built-in simulated backend (unified across the lab — the QM
#: repo's old q1/q2 demo names were retired with the CLI consolidation).
DEMO_QUBITS = {
    "q0": {"readout_freq": 5.95e9, "drive_freq": 3.87e9, "pi_amp": 0.20, "readout_amp": 0.25,
           "readout_power_dbm": -25.0},
    "q1": {"readout_freq": 6.05e9, "drive_freq": 4.01e9, "pi_amp": 0.18, "readout_amp": 0.22,
           "readout_power_dbm": -27.0},
}

#: backend family -> (what provides it, which venv on the lab machines)
SERVED_BY = {
    "qblox": ("LCHQBDriver", ".venv-qblox"),
    "qm": ("LCHQMDriver", ".venv-qm"),
    "simulated": ("scqo built-in", "any venv"),
}


def default_qubits(sess: Session) -> list[str]:
    """Measurable qubits for 'run on everything' defaults.

    The device tree may also contain couplers (lab convention: ``c*``, e.g. ``c12``)
    modeled as transmon elements without a usable readout port — measuring one fails
    on hardware. Only ``q*`` elements are measurement targets by default; pass
    --qubits to override explicitly.
    """
    return [q for q in sess.device_state() if q.startswith("q")]


def ensure_demo_experiments() -> None:
    """Make the simulated backend usable with NO driver installed.

    scqo core registers nothing (its experiment classes are abstract — no probe);
    the catalog normally fills via the drivers' ``scqo.experiments`` entry points.
    For pure-simulated use (the view venv, SCQO CI) this registers a probe-less
    subclass for every core experiment — but only for names still ABSENT from the
    catalog, so a driver's registration is never shadowed.
    """
    from scqo import catalog, register
    from scqo import experiments as _exp
    from scqo.experiment import Experiment

    registered = {entry["name"] for entry in catalog()}  # triggers entry-point discovery
    for attr in _exp.__all__:
        cls = getattr(_exp, attr)
        if isinstance(cls, type) and issubclass(cls, Experiment) and getattr(cls, "name", None):
            if cls.name not in registered:
                register(type(f"Sim{cls.__name__}", (cls,), {"probe": lambda self: None,
                                                             "__doc__": cls.__doc__}))


def _demo_session(cfg: LabConfig, setup_name: str = "",
                  cooldown_id: str = "") -> tuple[Session, LabConfig]:
    from scqo.testing import InMemoryDevice, SimulatedBackend

    ensure_demo_experiments()
    backend: Backend = SimulatedBackend(InMemoryDevice(DEMO_QUBITS))
    return make_session(backend, cfg, backend_label="simulated",
                        setup_name=setup_name, cooldown_id=cooldown_id), cfg


def resolve_device_setup(cfg: LabConfig) -> tuple[str, str, dict] | None:
    """Resolve cfg's device -> ACTIVE cycle -> named setup, touching NO instrument.

    Returns ``(cycle_id, setup_name, setup)``, or None for the device-less demo
    fallback. Raises SystemExit with the canonical refusal text on every missing
    link — ``build_session`` runs, and ``scqo user`` displays, the SAME messages.
    """
    if cfg.device is None:
        if cfg.setup:
            raise SystemExit(
                f"setup {cfg.setup!r} is selected in {cfg.user_source or 'the user overlay'} "
                "but no device is — a setup belongs to a device's cooldown cycle. Select the "
                "device first:\n  scqo user --device <name> [--setup <name>]"
            )
        return None
    if cfg.data_root is None:
        raise SystemExit(
            f"device {cfg.device!r} is selected but no data_root is configured in "
            f"{cfg.source or 'the lab config'} — the device's cooldown registry lives under it"
        )

    from scqo.datastore import (
        COOLDOWNS_FILE,
        SetupResolutionError,
        active_cooldown,
        load_cooldowns,
        resolve_setup,
    )

    try:
        cycles = load_cooldowns(cfg.data_root, cfg.device)
    except ValueError as err:  # broken registry: still loud, still the same text everywhere
        raise SystemExit(str(err)) from None
    registry = Path(cfg.data_root) / cfg.device / COOLDOWNS_FILE
    start_fix = (f"scqo device cooldown start cd1 [--fridge <name> --packaging <text>]"
                 f"   (device {cfg.device!r})")
    if not cycles:
        raise SystemExit(
            f"device {cfg.device!r} has no cooldown registry yet — the manager runs:\n"
            f"  {start_fix}\n"
            f"then hand-adds a [cd1.setup.<name>] block (backend [+ note]) to\n"
            f"  {registry}"
        )
    active = active_cooldown(cycles)
    if active is None:
        raise SystemExit(
            f"device {cfg.device!r} has no ACTIVE cooldown cycle — start the next one:\n  {start_fix}"
        )
    cid, cycle = active
    try:
        name, setup = resolve_setup(cycle, cfg.setup or None)
    except SetupResolutionError as err:
        if err.reason == "none":
            raise SystemExit(
                f"cycle {cid!r} of device {cfg.device!r} has no setups yet — runs need one.\n"
                f"Hand-add a named setup block to {registry}:\n\n"
                f"  [{cid}.setup.<name>]\n"
                f'  backend = "qblox"                # qblox | qm | simulated\n\n'
                f"(for real backends the manager then creates the DERIVED vendor folder\n"
                f"<device>/{cid}/<name>/backend_config/ and copies the vendor config files in\n"
                f"under canonical names; simulated needs no folder)"
            ) from None
        if err.reason == "ambiguous":
            raise SystemExit(
                f"cycle {cid!r} of device {cfg.device!r} has {len(err.available)} setups and "
                f"none is selected: {', '.join(err.available)}\n"
                f"pick the one you measure with (written to your user.toml):\n"
                f"  scqo user --setup <name>"
            ) from None
        raise SystemExit(  # reason == "unknown": a stale/mistyped selection
            f"setup {cfg.setup!r} (selected in {cfg.user_source or 'the user overlay'}) does "
            f"not exist in the ACTIVE cycle {cid!r} of device {cfg.device!r} — available: "
            f"{', '.join(err.available) or 'none'}\n"
            f"fix:  scqo user --setup <name>    (or scqo user --clear-setup for auto-selection)"
        ) from None
    return cid, name, setup


def build_session(config_path: str | None = None) -> tuple[Session, LabConfig]:
    """Resolve device -> active cycle -> named setup -> backend; return a Session.

    The user names the SAMPLE (``device`` in user.toml, else the [lab] default) and,
    when the sample's ACTIVE cycle has several setups, WHICH one they measure with
    (``setup`` in user.toml, ``scqo user --setup``); a single-setup cycle auto-selects.
    The selected setup says which instrument carries the sample right now and where
    that instrument's vendor config lives. No device anywhere = the built-in simulated
    demo (nothing saved). Every missing link fails loudly naming the exact fix.
    """
    cfg = load_lab_config(config_path)
    resolved = resolve_device_setup(cfg)
    if resolved is None:
        return _demo_session(cfg)
    cid, name, setup = resolved

    family = setup["backend"]
    if family == "simulated":
        return _demo_session(cfg, setup_name=name, cooldown_id=cid)
    for ep in entry_points(group="scqo.backends"):
        if ep.name == family:
            backend = ep.load()(cfg, setup)  # a factory ImportError propagates with its traceback
            return make_session(backend, cfg, backend_label=family,
                                setup_name=name, cooldown_id=cid), cfg
    provider, venv = SERVED_BY[family]
    raise SystemExit(
        f"device {cfg.device!r} is on backend {family!r} (cycle {cid}, setup {name!r}), "
        f"and that driver is not registered in this environment.\n"
        f"- wrong venv? activate D:\\github\\{venv} (the one that has {provider})\n"
        f"- already in {venv}? then {provider} was never (re)installed here: entry points\n"
        f"  register at INSTALL time — re-run its `uv pip install -e` line (INSTALL §1/§5)"
    )
