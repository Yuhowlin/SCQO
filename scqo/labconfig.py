"""Lab configuration — so students run scripts without editing any repo.

One tiny TOML file tells every command where data goes; everything ELSE follows the
DEVICE: which instrument a sample hangs on, and where its vendor config lives, is a
NAMED setup of the sample's ACTIVE cooldown cycle
(``<data_root>/<device>/cooldowns.toml`` — see ``scqo.datastore.load_cooldowns``;
users pick one with ``scqo user --setup``), and the scqo state + physics files are
per (cooldown, setup), pure convention (``scqo.datastore.setup_scqo_dir``:
``<data_root>/<device>/<cooldown>/<setup>/scqo/`` — a SIBLING of the setup's vendor
``instrument_config`` folder, never inside it, so the QM backend's QUAM load never
sweeps them up).
Resolution order for the config file:

1. an explicit path passed to :func:`load`;
2. the ``SCQO_CONFIG`` environment variable (machine-wide on shared servers);
3. ``~/.scqo/config.toml`` (per-user, dev machines);
4. built-in defaults (device-less simulated demo, no persistence) — everything still
   works, nothing is saved.

Example ``~/.scqo/config.toml``::

    [lab]
    data_root   = "D:/qpu_data"
    device      = "chipA"            # OPTIONAL lab-default sample (omit on multi-user servers)
    state_sync  = "pull"             # real backends; "simulated" always forces push
    default_tags = ["projX"]         # stamped on every run

**Standing per-experiment parameter defaults** live in a second, optional TOML file —
default ``~/.scqo/parameters.toml``, overridable with ``parameters_file`` in ``[lab]``
or per user. One top-level table per experiment; values sit between the code defaults
and whatever the caller passes — code defaults < this file < caller/CLI::

    [resonator_spectroscopy]
    frequency_span_hz = 15e6
    num_points = 201

A ``parameters_file`` that is named but missing raises (like a mistyped config path),
and an existing file that does not parse raises too — this file changes what gets
measured, so it never fails silently. Only the implicit default path may be absent
(code defaults apply). Tables for experiments not installed here (e.g. contrib) are
kept untouched; a typo'd KEY inside a table surfaces when that experiment runs.

**Per-user overlay** — on a multi-account server with one machine-wide shared config
(``$SCQO_CONFIG``), each account may keep ``~/.scqo/user.toml`` with PERSONAL keys
only (flat, no tables)::

    device = "chipA"                  # which SAMPLE I work on
    setup = "qblox_main"              # which setup of its ACTIVE cycle (scqo user --setup;
    #                                 # omit when the cycle has only one — it auto-selects)
    default_tags = ["projA"]          # appended to the shared tags, deduped
    # parameters_file = "~/projB.toml"     # OPTIONAL — only to use a DIFFERENT file;
    #                                      # ~/.scqo/parameters.toml applies automatically

Any other key is rejected loudly: machine wiring belongs to the shared lab config,
and the instrument follows the device's cooldown registry. The overlay applies only
on top of a FOUND base config — never to the built-in defaults. ``$SCQO_USER_CONFIG``
selects a different overlay file, or disables the overlay entirely with ``none``.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

from .backend import Backend
from .datastore import setup_state_path
from .session import Session

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - py3.10 fallback
    import tomli as tomllib

DEFAULT_PATH = Path.home() / ".scqo" / "config.toml"
PARAMS_DEFAULT_PATH = Path.home() / ".scqo" / "parameters.toml"
USER_DEFAULT_PATH = Path.home() / ".scqo" / "user.toml"
ENV_VAR = "SCQO_CONFIG"
USER_ENV_VAR = "SCQO_USER_CONFIG"
#: The only keys a per-user overlay may set. Machine wiring (data_root, state_sync)
#: belongs to the shared lab config; the instrument follows the DEVICE via the
#: selected setup of its ACTIVE cooldown cycle — a user names the sample they work
#: on and (when the cycle has several) which of its setups they measure with.
USER_ALLOWED_KEYS = ("device", "setup", "default_tags", "parameters_file")


def _load_parameter_defaults(path_setting: str | None) -> tuple[dict[str, dict], Path | None]:
    """Parse the optional per-experiment parameter-defaults TOML (see module docstring).

    A ``parameters_file`` that is named but missing raises; only the implicit
    ``PARAMS_DEFAULT_PATH`` may be absent (code defaults apply, nothing to merge).
    A file that exists but does not parse — or whose top level is not experiment
    tables — raises as well: this file changes what gets measured, so it must never
    fail silently (deliberately NOT the warn-and-ignore devices.toml convention).
    """
    if path_setting is not None:
        path = Path(path_setting).expanduser()
        if not path.is_file():
            raise FileNotFoundError(f"parameters_file not found: {path}")
    else:
        path = PARAMS_DEFAULT_PATH
        if not path.is_file():
            return {}, None
    try:  # utf-8-sig: tolerate a PowerShell-written UTF-8 BOM
        raw = tomllib.loads(path.read_text(encoding="utf-8-sig"))
    except tomllib.TOMLDecodeError as err:
        raise ValueError(f"invalid parameter-defaults file {path}: {err}") from None
    for name, table in raw.items():
        if not isinstance(table, dict):
            raise ValueError(
                f"{path}: top-level keys must be experiment tables like [resonator_spectroscopy]; "
                f"{name!r} belongs inside one"
            )
    return raw, path


def _load_user_overlay() -> tuple[dict, Path | None]:
    """Resolve + parse the per-user overlay (flat top-level keys, no tables).

    ``$SCQO_USER_CONFIG``: unset -> the default ``~/.scqo/user.toml`` (absent =
    silently no overlay); ``"none"``/empty -> disabled (subprocess-test hermeticity);
    a path -> that file, and a missing file raises (an explicitly named overlay never
    fails silently). Malformed TOML, a key outside :data:`USER_ALLOWED_KEYS`, or a
    wrongly typed value raise ValueError naming the file — this file changes which
    instrument a run lands on.
    """
    env = os.environ.get(USER_ENV_VAR)
    if env is not None:
        if env.strip().lower() in ("", "none"):
            return {}, None
        path = Path(env).expanduser()
        if not path.is_file():
            raise FileNotFoundError(f"${USER_ENV_VAR} points to a missing user overlay: {env}")
    else:
        path = USER_DEFAULT_PATH
        if not path.is_file():
            return {}, None
    try:  # utf-8-sig: tolerate a PowerShell-written UTF-8 BOM
        raw = tomllib.loads(path.read_text(encoding="utf-8-sig"))
    except tomllib.TOMLDecodeError as err:
        raise ValueError(f"invalid user overlay {path}: {err}") from None
    unknown = sorted(set(raw) - set(USER_ALLOWED_KEYS))
    if unknown:
        raise ValueError(
            f"{path}: key(s) {', '.join(map(repr, unknown))} are not allowed in a user overlay "
            f"(allowed: {', '.join(USER_ALLOWED_KEYS)}). Machine wiring belongs to the shared "
            f"lab config; the instrument follows the device's cooldown registry."
        )
    for key in ("device", "setup", "parameters_file"):
        if key in raw and not isinstance(raw[key], str):
            raise ValueError(f"{path}: {key!r} must be a string, got {type(raw[key]).__name__}")
    tags = raw.get("default_tags")
    if tags is not None and not (isinstance(tags, list) and all(isinstance(t, str) for t in tags)):
        raise ValueError(f"{path}: 'default_tags' must be a list of strings")
    return raw, path


@dataclass(frozen=True)
class LabConfig:
    """Parsed lab configuration (see module docstring for the file format)."""

    data_root: Path | None = None
    device: str | None = None  # the resolved SAMPLE (user overlay > [lab]); None = demo fallback
    # The user's setup selection within the device's ACTIVE cycle (user overlay ONLY —
    # a shared [lab] default setup would silently steer every account's instrument;
    # single-setup cycles auto-select, so the common case needs no selection at all).
    setup: str | None = None
    state_sync: str = "pull"
    default_tags: list[str] = field(default_factory=list)
    parameter_defaults: dict[str, dict] = field(default_factory=dict)  # [experiment] tables from parameters.toml
    source: Path | None = None  # which file was loaded (None = built-in defaults)
    parameters_source: Path | None = None  # which parameters file was loaded (None = none found)
    user_source: Path | None = None  # which per-user overlay was applied (None = none)


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
            # utf-8-sig: tolerate a PowerShell-written UTF-8 BOM
            raw = tomllib.loads(candidate.read_text(encoding="utf-8-sig"))
            lab = raw.pop("lab", {})
            # Per-user overlay (personal keys only; applies only ON TOP of a found base
            # config). The user names the SAMPLE they work on; which instrument that
            # sample hangs on comes from its cooldown registry's current setup.
            user, user_source = _load_user_overlay()
            device = user.get("device", lab.get("device"))
            setup = user.get("setup")  # deliberately NOT read from [lab] — see LabConfig
            # Standing parameter defaults: user's own file beats the [lab] one.
            params_file = user.get("parameters_file", lab.get("parameters_file"))
            parameter_defaults, parameters_source = _load_parameter_defaults(params_file)
            # Tags: shared (lab-wide) first, the user's project tags appended, deduped.
            default_tags = list(lab.get("default_tags", []))
            if user.get("default_tags"):
                default_tags = list(dict.fromkeys([*default_tags, *user["default_tags"]]))
            # expanduser: lets a config say data_root = "~/qpu_data" (macOS/Linux idiom)
            return LabConfig(
                data_root=Path(lab["data_root"]).expanduser() if lab.get("data_root") else None,
                device=device,
                setup=setup,
                state_sync=lab.get("state_sync", "pull"),
                default_tags=default_tags,
                parameter_defaults=parameter_defaults,
                source=candidate,
                parameters_source=parameters_source,
                user_source=user_source,
            )
    # No config.toml at all: the per-user parameters file still applies — standing
    # experiment preferences are independent of the machine wiring.
    parameter_defaults, parameters_source = _load_parameter_defaults(None)
    return LabConfig(parameter_defaults=parameter_defaults, parameters_source=parameters_source)


def make_session(backend: Backend, cfg: LabConfig, roster, *, backend_label: str,
                 setup_name: str = "", cooldown_id: str = "") -> Session:
    """Build a Session wired to the lab config (datastore, state file, default tags).

    ``backend_label`` is the RESOLVED setup's backend; ``setup_name`` + ``cooldown_id``
    are the RESOLVED era (named setup + its cycle) stamped verbatim on every run. The
    scqo state + physics files live in that context's ``scqo/`` folder
    (:func:`scqo.datastore.setup_scqo_dir`: ``<device>/<cooldown>/<setup>/scqo/``) — so
    a persisted session REQUIRES both a setup name AND a cooldown id (notebook sessions
    that want a free-form location pass ``state_path`` to :class:`~scqo.session.Session`
    directly). Persistence needs BOTH a data_root and a device; the device-less demo
    fallback saves nothing. ``simulated`` forces ``state_sync="push"``: an in-memory
    demo device has no vendor truth to pull, so without push its calibrations would
    silently reset every session.
    """
    saved = cfg.data_root is not None and cfg.device is not None
    if saved and (not setup_name or not cooldown_id):
        raise ValueError(
            f"make_session: persisting device {cfg.device!r} requires its resolved setup "
            "name AND cooldown id (the scqo/ folder is <device>/<cooldown>/<setup>/scqo/). "
            "Resolve the setup first (scqo.cli.build_session does) or pass state_path to "
            "Session directly.")
    state_path = (str(setup_state_path(cfg.data_root, cfg.device, cooldown_id, setup_name))
                  if saved else None)
    return Session(
        backend,
        roster,
        state_path=state_path,
        data_root=cfg.data_root if saved else None,
        device_name=cfg.device or "device",
        state_sync="push" if backend_label == "simulated" else cfg.state_sync,  # type: ignore[arg-type]
        default_tags=cfg.default_tags,
        parameter_defaults=cfg.parameter_defaults,
        parameter_defaults_source=str(cfg.parameters_source) if cfg.parameters_source else None,
        backend_label=backend_label,
        setup_name=setup_name,
        cooldown_id=cooldown_id,
    )
