"""Compatibility wrapper for src-layout package entrypoint."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import Callable, cast


ROOT = Path(__file__).resolve().parent
SRC_DIR = ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))


def main() -> int:
    """Compatibility entrypoint delegating to package CLI main."""
    module = importlib.import_module("gitea_automation.cli")
    package_main = cast(Callable[[], int], getattr(module, "main"))

    return package_main()


if __name__ == "__main__":
    exit_code = main()
    if sys.gettrace() is None:
        raise SystemExit(exit_code)
