"""Lab configuration — so students run scripts without editing any repo.

One small TOML file tells every script where data goes, which device this is, and
which backend to use. Resolution order:

1. an explicit path passed to :func:`load`;
2. the ``SCQO_CONFIG`` environment variable;
3. ``~/.scqo/config.toml`` (per-user; the lab installs a canonical copy);
4. built-in defaults (simulated backend, no persistence) — everything still works,
   nothing is saved.

Example ``~/.scqo/config.toml``::

    [lab]
    data_root   = "D:/qpu_data"
    device_name = "SQ4B_v3"          # the SAMPLE (physical chip) name — never the instrument
    state_path  = "D:/qpu_data/SQ4B_v3/scqo_state.json"
    backend     = "simulated"        # "qblox" / "qm" on the control PC
    state_sync  = "pull"             # "push" only for devices SCQO fully owns
    default_tags = ["cooldown7"]     # stamped on every run; edit once per cooldown

Driver-specific keys live in their own tables (e.g. ``[qblox]``) and are passed
through untouched in :attr:`LabConfig.extras`.

**Two instruments carrying two different samples?** Each vendor table may override
``device_name`` / ``state_path`` with the sample mounted on *that* instrument —
switching ``backend`` then switches the device automatically (no second config file,
no way to write runs under the wrong sample)::

    [qblox]
    config_dir  = "D:/qpu_data/chipA/qblox_state"
    device_name = "chipA"
    state_path  = "D:/qpu_data/chipA/scqo_state.json"

    [qm]
    state_dir   = "D:/qpu_data/chipB/qm_state"
    device_name = "chipB"
    state_path  = "D:/qpu_data/chipB/scqo_state.json"
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

from .backend import Backend
from .session import Session

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - py3.10 fallback
    import tomli as tomllib

DEFAULT_PATH = Path.home() / ".scqo" / "config.toml"
ENV_VAR = "SCQO_CONFIG"


def _backend_family(backend: str) -> str | None:
    """The vendor table a backend reads overrides from ("qblox_sim" -> "qblox")."""
    for family in ("qblox", "qm"):
        if backend == family or backend == f"{family}_sim":
            return family
    return None


@dataclass(frozen=True)
class LabConfig:
    """Parsed lab configuration (see module docstring for the file format)."""

    data_root: Path | None = None
    device_name: str = "device"
    state_path: Path | None = None
    backend: str = "simulated"  # scripts dispatch on this: simulated | qblox | qm
    state_sync: str = "pull"
    default_tags: list[str] = field(default_factory=list)
    extras: dict = field(default_factory=dict)  # non-[lab] tables, passed through
    source: Path | None = None  # which file was loaded (None = built-in defaults)


def load(path: str | Path | None = None) -> LabConfig:
    """Load the lab config (explicit path -> $SCQO_CONFIG -> ~/.scqo/config.toml -> defaults).

    An explicitly named config (argument or ``$SCQO_CONFIG``) that does not exist is an
    error: silently falling back to "simulated, save nothing" would discard a day of data
    over a typo. Only the implicit ``~/.scqo/config.toml`` may be absent.
    """
    if path is not None and not Path(path).is_file():
        raise FileNotFoundError(f"lab config not found: {path}")
    env = os.environ.get(ENV_VAR)
    if path is None and env and not Path(env).is_file():
        raise FileNotFoundError(f"${ENV_VAR} points to a missing lab config: {env}")
    candidates = [Path(p) for p in (path, env, DEFAULT_PATH) if p]
    for candidate in candidates:
        if candidate.is_file():
            with open(candidate, "rb") as f:
                raw = tomllib.load(f)
            lab = raw.pop("lab", {})
            backend = lab.get("backend", "simulated")
            # Per-backend overrides: the vendor table of the ACTIVE backend may name
            # the sample mounted on that instrument (device = the physical sample),
            # so switching backend switches device — see the module docstring.
            family = _backend_family(backend)
            vendor = raw.get(family, {}) if family else {}
            device_name = vendor.get("device_name", lab.get("device_name", "device"))
            state_path = vendor.get("state_path", lab.get("state_path"))
            # expanduser: lets a config say data_root = "~/qpu_data" (macOS/Linux idiom)
            return LabConfig(
                data_root=Path(lab["data_root"]).expanduser() if lab.get("data_root") else None,
                device_name=device_name,
                state_path=Path(state_path).expanduser() if state_path else None,
                backend=backend,
                state_sync=lab.get("state_sync", "pull"),
                default_tags=list(lab.get("default_tags", [])),
                extras=raw,
                source=candidate,
            )
    return LabConfig()


def make_session(backend: Backend, cfg: LabConfig) -> Session:
    """Build a Session wired to the lab config (datastore, state file, default tags)."""
    return Session(
        backend,
        state_path=str(cfg.state_path) if cfg.state_path else None,
        data_root=cfg.data_root,
        device_name=cfg.device_name,
        state_sync=cfg.state_sync,  # type: ignore[arg-type]
        default_tags=cfg.default_tags,
        backend_label=cfg.backend,  # provenance: "qblox_sim" vs "qblox" vs "simulated" ...
    )
