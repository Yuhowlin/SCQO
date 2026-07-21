"""The lab command — ``scqo <subcommand>`` (engine formerly mirrored in the driver repos).

One implementation of the Tier-1 script surface, living where the common API lives.
Backends are discovered through the ``scqo.backends`` entry-point group (exactly like
experiments through ``scqo.experiments``): install LCHQBDriver and ``backend =
"qblox"`` works, install LCHQMDriver and ``backend = "qm"`` works; ``simulated`` is
built in. The driver repos keep thin ``scripts/`` wrappers for backward compatibility.
"""

import os

# The CLI is headless (figures are saved to run folders, never shown): pin the
# non-interactive matplotlib backend BEFORE anything imports pyplot. On Windows the
# default TkAgg intermittently fails mid-session (TclError "Can't find a usable
# tk.tcl") and the artifact fallback would silently drop the figure PNGs.
os.environ.setdefault("MPLBACKEND", "Agg")

from ._backends import build_session, default_targets  # noqa: E402,F401  (driver-wrapper API)
from ._engine import run_experiment_cli  # noqa: E402,F401
