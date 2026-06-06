"""Parameter schemas — the *decision surface*.

Every protocol declares its inputs as a pydantic model subclassing ``Parameters``.
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


class QubitSelection(Parameters):
    """Mixin: which qubits an experiment runs on."""

    qubits: list[str] = Field(..., description="Names of the qubits to measure, e.g. ['q0', 'q1'].")


class AveragingParameters(Parameters):
    """Mixin: shot averaging shared by most experiments."""

    num_averages: int = Field(100, gt=0, description="Number of shots to average per sweep point.")
