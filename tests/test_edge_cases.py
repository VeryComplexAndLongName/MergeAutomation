"""Edge-case tests for config and parser behavior."""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from gitea_automation.cli import (
    AppConfig,
    GiteaAutomationError,
    infer_owner_repo_from_remote,
    parse_owner_repo_from_remote_url,
    parse_repo_from_remote_url,
    infer_owner_from_remote,
    parse_owner_from_remote_url,
    resolve_config,
)


def make_args() -> argparse.Namespace:
    """Build argparse-like namespace for resolve_config tests."""
    return argparse.Namespace(
        config="no-such-config.json",
        provider="gitea",
        base_url="http://example",
        gitea_url="http://example",
        owner="owner",
        repo="repo",
        repo_path=None,
        remote="origin",
        main_branch="main",
        target_branch=None,
        poll_interval=10,
        branch_scan_interval=1,
        timeout_seconds=1,
        merge_style="merge",
        pr_title_prefix="x",
        token="token",
        log_file="logs/app.log",
        log_mode="extended",
        dry_run=False,
        watch_branches=False,
        show_check_details=True,
        strict_status=False,
        merge_405_retry_threshold=8,
        merge_refresh_cycles=3,
    )


def test_resolve_config_fails_for_missing_repo_path(tmp_path: Path) -> None:
    """Should fail when repo path does not exist."""
    args = make_args()
    args.repo_path = str(tmp_path / "missing")
    with pytest.raises(GiteaAutomationError):
        resolve_config(args)


def test_app_config_strict_status_field_present(tmp_path: Path) -> None:
    """AppConfig should expose strict_status toggle."""
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    (repo_path / ".git").mkdir()
    config = AppConfig(
        provider_name="gitea",
        base_url="http://example",
        owner="owner",
        repo="repo",
        repo_path=repo_path,
        remote="origin",
        main_branch="main",
        poll_interval=10,
        timeout_seconds=10,
        merge_style="merge",
        pr_title_prefix="auto",
        dry_run=False,
        watch_branches=False,
        branch_scan_interval=1,
        show_check_details=True,
        strict_status=True,
        merge_405_retry_threshold=8,
        merge_refresh_cycles=3,
    )
    assert config.strict_status is True


def test_parse_owner_from_remote_url_github_https() -> None:
    """Should parse owner from HTTPS remote URL."""
    owner = parse_owner_from_remote_url("https://github.com/my-user/my-repo.git")
    assert owner == "my-user"


def test_parse_owner_from_remote_url_gitlab_group_path() -> None:
    """Should parse nested owner/group path from SSH URL."""
    owner = parse_owner_from_remote_url("git@gitlab.com:group/subgroup/project.git")
    assert owner == "group/subgroup"


def test_parse_repo_from_remote_url_github_https() -> None:
    """Should parse repository name from HTTPS remote URL."""
    repo = parse_repo_from_remote_url("https://github.com/my-user/my-repo.git")
    assert repo == "my-repo"


def test_parse_owner_repo_from_remote_url_github_https() -> None:
    """Should parse owner and repo from HTTPS remote URL."""
    parsed = parse_owner_repo_from_remote_url("https://github.com/my-user/my-repo.git")
    assert parsed == ("my-user", "my-repo")


def test_infer_owner_from_remote_returns_none_for_missing_remote(tmp_path: Path) -> None:
    """Inference should return None when remote URL cannot be read."""
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    (repo_path / ".git").mkdir()
    assert infer_owner_from_remote(repo_path, "origin") is None


def test_infer_owner_repo_from_remote_returns_none_for_missing_remote(tmp_path: Path) -> None:
    """Inference should return None when owner/repo cannot be read from remote URL."""
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    (repo_path / ".git").mkdir()
    assert infer_owner_repo_from_remote(repo_path, "origin") is None
