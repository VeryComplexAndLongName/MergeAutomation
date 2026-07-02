"""Pydantic models for application runtime configuration."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator


class AppConfig(BaseModel):
    """Runtime configuration for the automation flow."""

    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    provider_name: Literal["gitea", "github", "gitlab"] = "gitea"
    base_url: str = Field(min_length=1)
    owner: str = Field(min_length=1)
    repo: str = Field(min_length=1)
    repo_path: Path
    remote: str = Field(min_length=1)
    main_branch: str = Field(min_length=1)
    poll_interval: int = Field(ge=1)
    timeout_seconds: int = Field(ge=1)
    merge_style: Literal["merge", "rebase", "rebase-merge", "squash", "fast-forward-only"]
    pr_title_prefix: str = Field(min_length=1)
    dry_run: bool = False
    watch_branches: bool = True
    branch_scan_interval: int = Field(ge=1)
    log_mode: Literal["basic", "extended"] = "extended"
    show_check_details: bool = True
    strict_status: bool = False
    merge_405_retry_threshold: int = Field(ge=1)
    merge_refresh_cycles: int = Field(ge=1, le=3)

    @field_validator("repo_path")
    @classmethod
    def validate_repo_path_exists(cls, value: Path) -> Path:
        """Ensure local repository path exists on filesystem."""
        if not value.exists():
            raise ValueError(f"Local repo path does not exist: {value}")
        if not value.is_dir():
            raise ValueError(f"Local repo path must be a directory: {value}")
        if not (value / ".git").exists():
            raise ValueError(f"Local repo path is not a git repository: {value}")
        return value

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        """Ensure base_url is a valid HTTP(S) URL and keep string type."""
        validated = TypeAdapter(AnyHttpUrl).validate_python(value)
        return str(validated).rstrip("/")

    @model_validator(mode="after")
    def validate_provider_merge_style(self) -> "AppConfig":
        """Validate merge style compatibility for selected provider."""
        allowed_by_provider = {
            "gitea": {"merge", "rebase", "rebase-merge", "squash", "fast-forward-only"},
            "github": {"merge", "rebase", "squash"},
            "gitlab": {"merge", "squash"},
        }
        allowed = allowed_by_provider[self.provider_name]
        if self.merge_style not in allowed:
            raise ValueError(
                "merge_style is not supported for provider "
                f"'{self.provider_name}': '{self.merge_style}'"
            )
        return self
