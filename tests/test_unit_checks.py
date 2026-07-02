"""Unit tests for status check processing helpers."""

from __future__ import annotations

from typing import Any

from gitea_automation.cli import checks_signature, get_status_checks
from gitea_automation.cli import _format_authors


def test_get_status_checks_filters_non_dicts() -> None:
    """Only dict items from statuses list should be returned."""
    status = {"statuses": [{"context": "ci"}, "bad", 1, {"context": "lint"}]}
    checks = get_status_checks(status)
    assert len(checks) == 2


def test_checks_signature_is_stable() -> None:
    """Signature should be order-independent for the same data."""
    first = [
        {"context": "lint", "status": "success", "description": "ok", "target_url": "x"},
        {"context": "test", "status": "pending", "description": "run", "target_url": "y"},
    ]
    second = list(reversed(first))
    assert checks_signature(first) == checks_signature(second)


def test_format_authors_formats_name_and_email() -> None:
    """Authors formatter should render PEP 621 author entries."""
    authors: list[dict[str, Any]] = [
        {"name": "Alice", "email": "alice@example.com"},
        {"name": "Bob"},
    ]
    assert _format_authors(authors) == "Alice <alice@example.com>, Bob"


def test_format_authors_returns_unknown_for_invalid_input() -> None:
    """Authors formatter should handle missing/invalid values safely."""
    assert _format_authors(None) == "unknown"
