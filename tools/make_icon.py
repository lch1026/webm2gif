#!/usr/bin/env python3
"""Build the application icon (``AppIcon.icns``) from the source artwork.

    .venv/bin/python tools/make_icon.py                     # packaging/AppIcon.png
    .venv/bin/python tools/make_icon.py --image other.png   # 换一张图
    .venv/bin/python tools/make_icon.py --full-bleed        # 不套 macOS 圆角

The artwork is centre-cropped to a square, scaled with high-quality
interpolation into every size macOS asks for, and clipped to the rounded
"squircle" macOS uses for app icons, so it lines up with the system icons in
the Dock and in Finder.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

#: Default artwork; ``--image`` picks another file.
DEFAULT_SOURCE = PROJECT_ROOT / "packaging" / "AppIcon.png"

#: ``.icns`` element types (https://en.wikipedia.org/wiki/Apple_Icon_Image_format)
ICNS_TYPES = (
    (b"icp4", 16),
    (b"icp5", 32),
    (b"icp6", 64),
    (b"ic07", 128),
    (b"ic08", 256),
    (b"ic09", 512),
    (b"ic10", 1024),
)

#: Sizes also written as loose PNGs, handy for documentation.
PREVIEW_SIZES = (16, 32, 128, 256, 512, 1024)

#: The Objective-C initialiser, kept as a string so the line stays readable.
BITMAP_INIT_SELECTOR = (
    "initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_"
    "hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_"
)

#: Transparent margin and corner radius, as fractions of the icon size.
SQUIRCLE_MARGIN = 0.06
SQUIRCLE_RADIUS = 0.225


def load_image(path: Path):
    """The artwork as an ``NSImage``; exits with a readable error otherwise."""
    import AppKit

    if not path.is_file():
        raise SystemExit(f"找不到图标素材：{path}\n可以用 --image 指定另一张图片。")
    image = AppKit.NSImage.alloc().initWithContentsOfFile_(str(path))
    if image is None or not image.isValid():
        raise SystemExit(f"无法读取图片（不是 macOS 支持的格式？）：{path}")
    return image


def square_crop(size):
    """Centred square of ``size``, so any aspect ratio fills without distortion."""
    from Foundation import NSMakeRect

    side = min(size.width, size.height)
    return NSMakeRect((size.width - side) / 2.0, (size.height - side) / 2.0, side, side)


def icon_body(size: int, full_bleed: bool):
    """The rectangle the artwork is drawn into, already clipped to its shape."""
    import AppKit
    from Foundation import NSMakeRect

    if full_bleed:
        return NSMakeRect(0, 0, size, size)
    inset = size * SQUIRCLE_MARGIN
    body = NSMakeRect(inset, inset, size - 2 * inset, size - 2 * inset)
    radius = body.size.width * SQUIRCLE_RADIUS
    AppKit.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(body, radius, radius).addClip()
    return body


def png_bytes(size: int, image, full_bleed: bool = False) -> bytes:
    """Render one icon size in memory."""
    import AppKit

    rep = AppKit.NSBitmapImageRep.alloc()
    rep = getattr(rep, BITMAP_INIT_SELECTOR)(
        None, size, size, 8, 4, True, False, AppKit.NSCalibratedRGBColorSpace, 0, 0
    )
    rep.setSize_((size, size))
    context = AppKit.NSGraphicsContext.graphicsContextWithBitmapImageRep_(rep)
    AppKit.NSGraphicsContext.saveGraphicsState()
    AppKit.NSGraphicsContext.setCurrentContext_(context)
    context.setImageInterpolation_(AppKit.NSImageInterpolationHigh)
    try:
        body = icon_body(size, full_bleed)
        image.drawInRect_fromRect_operation_fraction_(
            body, square_crop(image.size()), AppKit.NSCompositingOperationSourceOver, 1.0
        )
    finally:
        AppKit.NSGraphicsContext.restoreGraphicsState()
    data = rep.representationUsingType_properties_(AppKit.NSBitmapImageFileTypePNG, {})
    return bytes(data)


def write_icns(entries, destination: Path) -> Path:
    """Assemble an ``.icns`` file from ``(type, png_bytes)`` pairs.

    Written by hand because ``iconutil`` rejects even valid iconsets on some
    macOS builds, and the container format is trivial. Each element's size field
    counts the 8 header bytes as well ("the length of the data, including the
    type and length fields"), otherwise every element after the first one is
    unreadable — macOS then falls back to the 16×16 image.
    """
    chunks = []
    for element_type, payload in entries:
        chunks.append(element_type + (len(payload) + 8).to_bytes(4, "big") + payload)
    body = b"".join(chunks)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(b"icns" + (len(body) + 8).to_bytes(4, "big") + body)
    return destination


def build_icns(output_dir: Path, source: Path, full_bleed: bool = False) -> Path:
    """Write ``AppIcon.icns`` plus loose PNG previews into ``output_dir``."""
    image = load_image(source)
    legacy = output_dir / "AppIcon.iconset"
    if legacy.is_dir():
        shutil.rmtree(legacy, ignore_errors=True)
    entries = [(element_type, png_bytes(size, image, full_bleed)) for element_type, size in ICNS_TYPES]
    icns_path = write_icns(entries, output_dir / "AppIcon.icns")
    for size in PREVIEW_SIZES:
        (output_dir / f"AppIcon-{size}.png").write_bytes(png_bytes(size, image, full_bleed))
    return icns_path


def main() -> int:
    parser = argparse.ArgumentParser(description="从图片生成应用图标")
    parser.add_argument("--output", default="build", help="输出目录（默认 build/）")
    parser.add_argument("--image", default=str(DEFAULT_SOURCE), help="图标素材（默认 packaging/AppIcon.png）")
    parser.add_argument("--full-bleed", action="store_true", help="不套 macOS 圆角与留白，图片铺满整个画布")
    args = parser.parse_args()

    output_dir = Path(args.output)
    icns = build_icns(output_dir, Path(args.image).expanduser(), args.full_bleed)
    print(f"素材: {Path(args.image).expanduser()}")
    print(f"已生成 {icns} ({icns.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
