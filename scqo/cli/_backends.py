"""Session building for the CLI: built-in simulated backend + driver discovery.

Real instruments are served by DRIVER packages that register a factory under the
``scqo.backends`` entry-point group (name = the backend family)::

    [project.entry-points."scqo.backends"]
    qblox = "lchqb.scqo_backend:build_backend"

A factory is ``build_backend(cfg: LabConfig) -> Backend`` and serves both the real
and the ``_sim`` (virtual twin) mode of its family. ``simulated`` (demo qubits,
synthetic data) is built in here, so query commands and CI need no driver at all.
"""

from __future__ import annotations

from importlib.metadata import entry_points

from scqo import LabConfig, Session, load_lab_config, make_session
from scqo.backend import Backend

#: Demo device for the built-in simulated backend (unified across the lab — the QM
#: repo's old q1/q2 demo names were retired with the CLI consolidation).
DEMO_QUBITS = {
    "q0": {"readout_freq": 5.95e9, "drive_freq": 3.87e9, "pi_amp": 0.20, "readout_amp": 0.25},
    "q1": {"readout_freq": 6.05e9, "drive_freq": 4.01e9, "pi_amp": 0.18, "readout_amp": 0.22},
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


def build_session(config_path: str | None = None) -> tuple[Session, LabConfig]:
    """Load the lab config and return a wired Session (datastore, state file, tags).

    ``simulated`` runs on the built-in demo device; any other backend resolves its
    family's factory from the ``scqo.backends`` entry-point group — a missing driver
    fails loudly naming the repo and venv that provide it.
    """
    cfg = load_lab_config(config_path)
    if cfg.backend == "simulated":
        from scqo.testing import InMemoryDevice, SimulatedBackend

        ensure_demo_experiments()
        backend: Backend = SimulatedBackend(InMemoryDevice(DEMO_QUBITS))
        return make_session(backend, cfg), cfg

    from scqo.labconfig import _backend_family

    family = _backend_family(cfg.backend)
    if family is None:
        raise SystemExit(
            f"unsupported backend {cfg.backend!r} in {cfg.source or 'defaults'} "
            "(known: simulated; qblox/qblox_sim — LCHQBDriver; qm/qm_sim — LCHQMDriver)"
        )
    for ep in entry_points(group="scqo.backends"):
        if ep.name == family:
            backend = ep.load()(cfg)  # a factory ImportError propagates with its real traceback
            return make_session(backend, cfg), cfg
    provider, venv = SERVED_BY[family]
    raise SystemExit(
        f"backend {cfg.backend!r} needs the {family!r} driver, which is not registered in this "
        f"environment.\n"
        f"- wrong venv? activate D:\\github\\{venv} (the one that has {provider})\n"
        f"- already in {venv}? then {provider} predates v0.4.0 or was never reinstalled: entry\n"
        f"  points register at INSTALL time — update the repo and re-run its "
        f"`uv pip install -e` line (INSTALL §1/§5)"
    )
