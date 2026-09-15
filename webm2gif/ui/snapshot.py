"""Render AppKit views to PNG without showing them on screen.

Used by the test-suite and ``tools/preview_ui.py`` so the interface can be
inspected (and reviewed) from a terminal session.
"""

from __future__ import annotations

import os
from pathlib import Path


def snapshot_view(view, destination: str | os.PathLike[str], scale: float = 2.0) -> Path:
    """Write a PNG of ``view`` (including subviews) to ``destination``."""
    import AppKit

    bounds = view.bounds()
    representation = view.bitmapImageRepForCachingDisplayInRect_(bounds)
    if representation is None:
        raise RuntimeError("无法为该视图创建位图缓存")
    view.cacheDisplayInRect_toBitmapImageRep_(bounds, representation)

    data = representation.representationUsingType_properties_(AppKit.NSBitmapImageFileTypePNG, {})
    if data is None:
        raise RuntimeError("PNG 编码失败")

    target = Path(destination)
    target.parent.mkdir(parents=True, exist_ok=True)
    if not data.writeToFile_atomically_(str(target), True):
        raise RuntimeError(f"无法写入 {target}")
    return target
