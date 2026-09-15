"""WebM → GIF converter with a small native macOS interface."""

from __future__ import annotations

__version__ = "1.1.0"

APP_NAME = "WebM2GIF"
APP_DISPLAY_NAME = "WebM → GIF"
BUNDLE_IDENTIFIER = "com.local.webm2gif"

WEBM_SUFFIXES = (".webm",)

__all__ = ["__version__", "APP_NAME", "APP_DISPLAY_NAME", "BUNDLE_IDENTIFIER", "WEBM_SUFFIXES"]
