#!/usr/bin/env python3
"""Render the WebM2GIF window to a PNG without opening it on screen.

    .venv/bin/python tools/preview_ui.py --demo docs/preview.png

The result is committed, so the screenshot in the README always matches the
current interface.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("WEBM2GIF_CONFIG_DIR", "/tmp/webm2gif-preview-config")

#: Demo data only: fictional paths, so a rendered preview can never leak the
#: home directory of whoever generated it.
DEMO_FOLDER = Path("/示例/素材")
DEMO_OUTPUT = Path("/示例/输出")


class DemoFFmpeg:
    """Stand-in for the local ffmpeg, so the screenshot stays reproducible."""

    path = "/示例/ffmpeg"
    source = "应用内置"

    def version(self) -> str:
        return "ffmpeg version 7.1 Copyright (c) 2000-2024 the FFmpeg developers"


def demo_items():
    from webm2gif.converter import DONE, PENDING, RUNNING, ConversionItem
    from webm2gif.ffmpeg import MediaInfo

    samples = [
        ("clip-intro.webm", MediaInfo(12.5, 1920, 1080, "vp9"), DONE, 1.0, ""),
        ("screen-recording.webm", MediaInfo(48.2, 1280, 720, "vp8"), RUNNING, 0.42, ""),
        ("表情包素材.webm", MediaInfo(3.1, 640, 480, "vp9"), PENDING, 0.0, ""),
        ("broken-file.webm", MediaInfo(0.0, 0, 0, ""), "failed", 0.0, "Invalid data found when processing input"),
    ]
    items = []
    for name, info, status, progress, message in samples:
        item = ConversionItem(source=DEMO_FOLDER / name, output=DEMO_OUTPUT / (name + ".gif"))
        item.info = info
        item.status = status
        item.progress = progress
        item.message = message
        items.append(item)
    return items


def main() -> int:
    parser = argparse.ArgumentParser(description="Render the UI to a PNG")
    parser.add_argument("destination", nargs="?", default="docs/preview.png")
    parser.add_argument("--demo", action="store_true", help="fill the table with sample rows")
    parser.add_argument("--scale", type=float, default=2.0)
    args = parser.parse_args()

    import AppKit

    from webm2gif.ui.main_window import MainWindowController

    app = AppKit.NSApplication.sharedApplication()
    app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)

    controller = MainWindowController.alloc().init()
    if args.demo:
        from webm2gif.hardware import HardwareCapabilities

        # Detected but not measured yet: the preview must not depend on the
        # machine that renders it.
        controller.capabilities = HardwareCapabilities(
            hwaccels=("videotoolbox",), decoders=("vp9", "vp8"), filters=("scale_vt",), detected=True
        )
        controller.items = demo_items()
        controller.ffmpeg = DemoFFmpeg()
        controller.ffmpeg_version_text = ""
        controller.refresh_ffmpeg_status()
        controller.last_output_dir = str(DEMO_OUTPUT)
        controller.table.reloadData()
        controller.refresh_controls()
        controller.refresh_hardware_label()
        controller.progress.setDoubleValue_(0.42)
        controller.progress_label.setStringValue_("1 / 4")
        controller.status_label.setStringValue_("正在转换：screen-recording.webm（42%）")
        controller.window.layoutIfNeeded()

    destination = Path(args.destination)
    controller.write_snapshot(destination)
    print(f"已写入 {destination} ({destination.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
