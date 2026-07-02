"""Pydantic models used by provider adapters."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class PullRequestReadiness(BaseModel):
    """Normalized pull request readiness state."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    mergeable: bool | None
    draft: bool
    has_conflicts: bool
