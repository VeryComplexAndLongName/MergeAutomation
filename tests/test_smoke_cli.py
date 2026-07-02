"""Smoke tests for CLI parser and startup behavior."""

from __future__ import annotations

import argparse

from gitea_automation.cli import build_parser


def test_parser_builds() -> None:
    """Parser should be created without raising exceptions."""
    parser = build_parser()
    assert isinstance(parser, argparse.ArgumentParser)


def test_parser_accepts_strict_status_flags() -> None:
    """Strict status flags should be recognized by parser."""
    parser = build_parser()
    args = parser.parse_args(["--strict-status"])
    assert args.strict_status is True


def test_parser_accepts_provider_flag() -> None:
    """Provider flag should be recognized by parser."""
    parser = build_parser()
    args = parser.parse_args(["--provider", "github"])
    assert args.provider == "github"


def test_parser_accepts_main_branch_flag() -> None:
    """Main branch flag should be recognized by parser."""
    parser = build_parser()
    args = parser.parse_args(["--main-branch", "develop"])
    assert args.main_branch == "develop"


def test_parser_accepts_log_mode_flag() -> None:
    """Log mode flag should be recognized by parser."""
    parser = build_parser()
    args = parser.parse_args(["--log-mode", "basic"])
    assert args.log_mode == "basic"
