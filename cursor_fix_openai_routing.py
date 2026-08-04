#!/usr/bin/env python3
"""Patch Cursor workbench bundles for per-model BYOK routing (macOS and Windows)."""

from __future__ import annotations

from cursor_openai_routing import *  # noqa: F403
from cursor_openai_routing import main

if __name__ == "__main__":
    raise SystemExit(main())
