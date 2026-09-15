"""Frozen-application entry point (used by packaging/webm2gif.spec)."""

from __future__ import annotations

from webm2gif.__main__ import main

if __name__ == "__main__":
    raise SystemExit(main())
