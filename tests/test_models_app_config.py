"""Unit tests for pydantic validation of AppConfig model."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from gitea_automation.models import AppConfig


def make_git_repo(path: Path) -> Path:
    """Create minimal local git repository structure for validation tests."""
    path.mkdir()
    (path / ".git").mkdir()
    return path


def make_config_data(repo_path: Path) -> dict[str, object]:
    """Build minimal valid config payload for AppConfig validation tests."""
    return {
        "provider_name": "gitea",
        "base_url": "http://example.local",
        "owner": "owner",
        "repo": "repo",
        "repo_path": repo_path,
        "remote": "origin",
        "main_branch": "main",
        "poll_interval": 10,
        "timeout_seconds": 300,
        "merge_style": "merge",
        "pr_title_prefix": "Auto merge",
        "dry_run": False,
        "watch_branches": True,
        "branch_scan_interval": 5,
        "show_check_details": True,
        "strict_status": False,
        "merge_405_retry_threshold": 8,
        "merge_refresh_cycles": 3,
    }


def test_app_config_validation_accepts_valid_payload(tmp_path: Path) -> None:
    """Model validation should succeed for valid config payload."""
    repo_path = make_git_repo(tmp_path / "repo")

    model = AppConfig.model_validate(make_config_data(repo_path))

    assert model.provider_name == "gitea"
    assert model.repo_path == repo_path
    assert model.main_branch == "main"


def test_app_config_validation_fails_for_missing_repo_path(tmp_path: Path) -> None:
    """Model validation should fail when repository path does not exist."""
    missing_repo_path = tmp_path / "missing-repo"

    with pytest.raises(ValidationError):
        AppConfig.model_validate(make_config_data(missing_repo_path))


def test_app_config_validation_fails_for_invalid_merge_refresh_cycles(
    tmp_path: Path,
) -> None:
    """Model validation should fail when merge_refresh_cycles is out of allowed range."""
    repo_path = make_git_repo(tmp_path / "repo")
    payload = make_config_data(repo_path)
    payload["merge_refresh_cycles"] = 4

    with pytest.raises(ValidationError):
        AppConfig.model_validate(payload)


def test_app_config_validation_fails_for_non_positive_branch_scan_interval(
    tmp_path: Path,
) -> None:
    """Model validation should fail when branch_scan_interval is not positive."""
    repo_path = make_git_repo(tmp_path / "repo")
    payload = make_config_data(repo_path)
    payload["branch_scan_interval"] = 0

    with pytest.raises(ValidationError):
        AppConfig.model_validate(payload)


def test_app_config_validation_fails_for_repo_path_file(tmp_path: Path) -> None:
    """Model validation should fail when repo_path points to a file, not directory."""
    repo_file = tmp_path / "repo.txt"
    repo_file.write_text("not a repo", encoding="utf-8")

    with pytest.raises(ValidationError):
        AppConfig.model_validate(make_config_data(repo_file))


def test_app_config_validation_fails_for_directory_without_git(tmp_path: Path) -> None:
    """Model validation should fail when repo_path is directory without .git."""
    repo_dir = tmp_path / "repo-no-git"
    repo_dir.mkdir()

    with pytest.raises(ValidationError):
        AppConfig.model_validate(make_config_data(repo_dir))


def test_app_config_validation_fails_for_invalid_base_url(tmp_path: Path) -> None:
    """Model validation should fail when base_url is not a valid HTTP URL."""
    repo_path = make_git_repo(tmp_path / "repo")
    payload = make_config_data(repo_path)
    payload["base_url"] = "not-a-url"

    with pytest.raises(ValidationError):
        AppConfig.model_validate(payload)


def test_app_config_validation_fails_for_unsupported_merge_style_github(
    tmp_path: Path,
) -> None:
    """Model validation should fail for provider-specific unsupported merge style."""
    repo_path = make_git_repo(tmp_path / "repo")
    payload = make_config_data(repo_path)
    payload["provider_name"] = "github"
    payload["merge_style"] = "rebase-merge"

    with pytest.raises(ValidationError):
        AppConfig.model_validate(payload)


def test_app_config_validation_accepts_supported_merge_style_github(
    tmp_path: Path,
) -> None:
    """Model validation should pass for provider-specific supported merge style."""
    repo_path = make_git_repo(tmp_path / "repo")
    payload = make_config_data(repo_path)
    payload["provider_name"] = "github"
    payload["merge_style"] = "squash"

    model = AppConfig.model_validate(payload)
    assert model.provider_name == "github"
    assert model.merge_style == "squash"
