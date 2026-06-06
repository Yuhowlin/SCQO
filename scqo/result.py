"""Result schemas — the *extraction surface*.

Analysis returns a structured, machine-readable ``Result`` (not just a figure), so a
human or an AI agent can read the extracted quantities and the per-qubit pass/fail
outcome and decide what to do next. Figures, if any, are referenced by path elsewhere
and are intentionally kept out of this schema.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class Outcome(str, Enum):
    """Per-qubit verdict for an experiment."""

    SUCCESSFUL = "successful"
    FAILED = "failed"
    NO_DATA = "no_data"


class Result(BaseModel):
    """Base class for all experiment results."""

    model_config = ConfigDict(extra="forbid")

    outcomes: dict[str, Outcome] = Field(
        default_factory=dict, description="Per-qubit pass/fail verdict, keyed by qubit name."
    )
    fit: dict[str, dict[str, float]] = Field(
        default_factory=dict, description="Per-qubit extracted quantities, keyed by qubit name."
    )

    @property
    def success(self) -> bool:
        """True only if every measured qubit succeeded."""
        return bool(self.outcomes) and all(o is Outcome.SUCCESSFUL for o in self.outcomes.values())
