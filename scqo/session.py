"""Session — the single entry point used identically by humans and AI agents.

Everything crosses this boundary as plain JSON-able Python (dicts / lists), so the
same calls drive a manual notebook *and* an LLM tool-use loop:

    sess = Session(backend, state_path="scqo_state.json")
    sess.catalog()                     # what can I measure? (with parameter schemas)
    sess.run("ramsey", {...})          # measure -> structured result
    sess.device_state()                # current calibration (the SCQO config)
    sess.history()                     # every recorded change (the loop's memory)

No vendor/hardware object is ever exposed here. The Session owns the **authoritative
SCQO config + change history** (a :class:`~scqo.config.RecordingDevice` wrapping the
backend's vendor device): runs are recorded, and the config is the source of truth.
"""

from __future__ import annotations

from typing import Any

from . import registry
from .backend import Backend
from .config import RecordingDevice


class Session:
    """Bind a backend and expose the experiment-level API over an SCQO-owned config."""

    def __init__(self, backend: Backend, *, state_path: str | None = None) -> None:
        self.backend = backend
        self._persist = state_path is not None
        #: authoritative SCQO config + history over the backend's vendor device. Seeded
        #: from the vendor on first use; if ``state_path`` exists it is loaded and pushed
        #: to the vendor instead (SCQO wins for the tracked calibration fields).
        self.device = RecordingDevice(backend.device, state_path=state_path)

    def catalog(self) -> list[dict]:
        """List available measurements with their JSON parameter schemas."""
        return registry.catalog()

    def run(self, experiment: str, params: dict[str, Any], update: bool = True) -> dict:
        """Run an experiment by name; return its structured result as a dict.

        If ``update`` and the run succeeded, fitted values are written back through the
        SCQO config — recorded into the change history and pushed to the vendor device —
        and the SCQO state is persisted (when a ``state_path`` was given).
        """
        cls = registry.get(experiment)
        exp = cls(self.backend, cls.Parameters(**params))
        exp.device = self.device  # route reads/writes through the recording config
        self.device.set_experiment(experiment)
        try:
            result = exp.run()
            if update and result.success:
                exp.update()
                if self._persist:
                    self.device.save()
        finally:
            self.device.set_experiment(None)
        return result.model_dump(mode="json")

    def device_state(self) -> dict:
        """Return a JSON snapshot of the authoritative SCQO calibration config."""
        return self.device.snapshot()

    def history(self) -> list[dict]:
        """Return the recorded change history (JSON-able): the loop's memory."""
        return [record.as_dict() for record in self.device.history()]
