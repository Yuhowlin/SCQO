"""Parameter schemas — the *decision surface*.

Every experiment declares its inputs as a pydantic model subclassing ``Parameters``.
Because they are plain pydantic models, ``MyParameters.model_json_schema()`` yields
a complete, typed, range-validated JSON schema. That schema is exactly what a human
form *or* an AI agent reads to know which knobs exist and what values are legal.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class Parameters(BaseModel):
    """Base class for all experiment parameters.

    ``extra="forbid"`` makes typos fail loudly (important when an AI fills params).
    ``validate_assignment`` keeps the model valid if fields are tweaked after init.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class TargetSelection(Parameters):
    """Mixin: which components an experiment runs on.

    Targets are roster component names whose INSTRUMENT category matches the
    experiment's ``target_category`` (validated pre-probe by the Session).
    """

    targets: list[str] = Field(..., description="Component names to measure, e.g. ['q0', 'q1'].")


class AveragingParameters(Parameters):
    """Mixin: shot averaging shared by most experiments."""

    num_averages: int = Field(100, gt=0, description="Number of shots to average per sweep point.")
