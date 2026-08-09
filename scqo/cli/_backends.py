"""Session building for the CLI: device -> cycle -> named setup -> backend.

Real instruments are served by DRIVER packages that register a factory under the
``scqo.backends`` entry-point group (name = the backend family)::

    [project.entry-points."scqo.backends"]
    qblox = "scqo_qblox.scqo_backend:build_backend"

A factory is ``build_backend(cfg: LabConfig, setup: dict, roster: Roster) ->
Backend`` — ``setup`` is the device's SELECTED named setup record from its cooldown
registry (``backend``, optional ``note``, plus the DERIVED ``instrument_config``
vendor folder injected by ``load_cooldowns`` —
``<device>/<cooldown>/<setup>/backend_config``, never typed), and ``roster`` is the
device's authority on which entities exist: a driver serves views BY ENTITY NAME
(``q1_xy`` -> its drive view over the vendor's q1 element), so it must resolve
each channel's target and kind through the roster rather than parsing names.
``simulated`` (demo qubits, synthetic data) is built in here, so query commands,
practice runs and CI need no driver at all.
"""

from __future__ import annotations

from importlib.metadata import entry_points
from pathlib import Path

from scqo import LabConfig, Session, load_lab_config, make_session
from scqo.backend import Backend

#: backend family -> (what provides it, which venv on the lab machines)
SERVED_BY = {
    "qblox": ("scqo-qblox", ".venv-qblox"),
    "qm": ("scqo-qm", ".venv-qm"),
    "simulated": ("scqo built-in", "any venv"),
}


def default_targets(sess: Session, experiment: str | None = None) -> list[str]:
    """Entities for 'run on everything' defaults: every roster entity whose
    KIND the experiment targets (qubit-like modes for single-qubit
    experiments, composite kinds for pair ones; qubit-like when no
    experiment is named). Pass --targets to override."""
    from scqo.catalog import QUBIT_LIKE

    kinds = set(QUBIT_LIKE)
    if experiment is not None:
        from scqo.experiments import get as _get_experiment

        try:
            kinds = set(_get_experiment(experiment).target_kinds)
        except KeyError:
            pass  # unknown name fails later with the catalog's own message
    return [name for name, e in sess.roster.entities.items()
            if e.kind in kinds and not e.retired]


def ensure_demo_experiments() -> None:
    """Make the simulated backend usable with NO driver installed.

    Since the model cutover the core experiments register THEMSELVES at
    import (their ``probe()`` raises; the simulated backend never calls it),
    so this only has to trigger the import — driver entry points still win
    for their own names. Kept as the named seam the CLI and doctor call.
    """
    from scqo import catalog

    catalog()  # imports the core package + discovers driver entry points


def _load_device(cfg: LabConfig):
    """The selected device's roster + datasheet (components.toml is REQUIRED;
    design.toml is optional — a missing datasheet is an empty one).

    A missing/invalid roster fails loudly with the minimal template — writing
    it is the one-time step that brings a device into the model."""
    from scqo.design import load_design
    from scqo.roster import load_components

    device_dir = Path(cfg.data_root) / cfg.device
    try:
        roster = load_components(device_dir)
        design = load_design(device_dir, roster)
    except FileNotFoundError as err:
        raise SystemExit(str(err)) from None
    except ValueError as err:  # a wrong roster/datasheet must never half-load
        raise SystemExit(str(err)) from None
    return roster, design


def _demo_session(cfg: LabConfig, setup_name: str = "",
                  cooldown_id: str = "") -> tuple[Session, LabConfig]:
    from scqo.testing import (
        InMemoryDevice,
        SimulatedBackend,
        demo_components,
        demo_design,
        demo_vendor_state,
    )

    ensure_demo_experiments()
    # Device-less demo: the built-in roster. A CONFIGURED device on a
    # simulated setup still uses its own components.toml (the chipT
    # prototyping path).
    if cfg.device is not None and cfg.data_root is not None:
        roster, design = _load_device(cfg)
    else:
        roster = demo_components(tunable=True)
        design = demo_design(roster)
    backend: Backend = SimulatedBackend(
        InMemoryDevice(roster, demo_vendor_state(roster, design)))
    return make_session(backend, cfg, roster, backend_label="simulated",
                        setup_name=setup_name, cooldown_id=cooldown_id,
                        design=design), cfg


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
    roster, design = _load_device(cfg)
    for ep in entry_points(group="scqo.backends"):
        if ep.name == family:
            # a factory ImportError propagates with its traceback
            backend = ep.load()(cfg, setup, roster)
            return make_session(backend, cfg, roster, backend_label=family,
                                setup_name=name, cooldown_id=cid,
                                design=design), cfg
    provider, venv = SERVED_BY[family]
    raise SystemExit(
        f"device {cfg.device!r} is on backend {family!r} (cycle {cid}, setup {name!r}), "
        f"and that driver is not registered in this environment.\n"
        f"- wrong venv? activate D:\\github\\{venv} (the one that has {provider})\n"
        f"- already in {venv}? then {provider} was never (re)installed here: entry points\n"
        f"  register at INSTALL time — re-run its `uv pip install -e` line (INSTALL §1/§5)"
    )
