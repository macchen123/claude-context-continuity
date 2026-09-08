"""Run the public continuity command-line interface as a Python module."""

from __future__ import annotations

import sys

from .continuity import main


if __name__ == "__main__":
    sys.exit(main())
