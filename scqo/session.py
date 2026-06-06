"""Session — the single entry point used identically by humans and AI agents.

Everything crosses this boundary as plain JSON-able Python (dicts / lists), so the
same three calls drive a manual notebook *and* an LLM tool-use loop:

    sess = Session(backend)            # backend = QbloxBackend() or QMBackend()
    sess.catalog()                     # what can I measure? (with parameter schemas)
    sess.run("ramsey", {...})          # measure -> structured result
    sess.device_state()                # current calibration (loop memory)

No vendor/hardware object is ever exposed here.
"""

from __future__ import annotations

from typing import Any

from . import registry
from .backend import Backend


class Session:
    """Bind a backend and expose the protocol-level API."""

    def __init__(self, backend: Backend) -> None:
        self.backend = backend

    def catalog(self) -> list[dict]:
        """List available measurements with their JSON parameter schemas."""
        return registry.catalog()

    def run(self, protocol: str, params: dict[str, Any], update: bool = True) -> dict:
        """Run a protocol by name; return its structured result as a dict.

        If ``update`` and the run succeeded, fitted values are written back into the
        device model (mirrors the manual analyze -> post_run / update_state step).
        """
        cls = registry.get(protocol)
        proto = cls(self.backend, cls.Parameters(**params))
        result = proto.run()
        if update and result.success:
            proto.update()
        return result.model_dump(mode="json")

    def device_state(self) -> dict:
        """Return a JSON snapshot of all qubit calibration state."""
        return self.backend.device.snapshot()
