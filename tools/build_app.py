#!/usr/bin/env python3
"""Assemble a double-clickable ``WebM2GIF.app`` bundle.

    .venv/bin/python tools/build_app.py            # lightweight bundle
    .venv/bin/python tools/build_app.py --standalone   # needs PyInstaller

The lightweight bundle holds the ``Info.plist``, the icon and a tiny launcher
that runs the project's virtual environment. The ``--standalone`` variant uses
PyInstaller so the application carries its own Python and ffmpeg.
"""

from __future__ import annotations

import argparse
import plistlib
import shutil
import stat
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from webm2gif import APP_NAME, BUNDLE_IDENTIFIER, __version__  # noqa: E402
from webm2gif.ffmpeg import find_ffmpeg  # noqa: E402

LAUNCHER = """#!/bin/sh
# WebM2GIF launcher: runs the project's virtual environment.
set -eu

HERE="$(cd "$(dirname "$0")" && pwd)"
PROJECT="$(cd "${HERE}/../../.." && pwd)"

# A PyInstaller build puts a self-contained binary next to this script.
if [ -x "${HERE}/WebM2GIF.bin" ]; then
    exec "${HERE}/WebM2GIF.bin" "$@"
fi

# Use the ffmpeg that ships inside this bundle, when there is one.
BUNDLED_FFMPEG="${HERE}/../Resources/bin/ffmpeg"
if [ -x "${BUNDLED_FFMPEG}" ]; then
    WEBM2GIF_FFMPEG="${BUNDLED_FFMPEG}"
    export WEBM2GIF_FFMPEG
fi

PYTHON="${PROJECT}/.venv/bin/python"
if [ ! -x "${PYTHON}" ]; then
    PYTHON="$(command -v python3 || true)"
fi
if [ -z "${PYTHON}" ] || [ ! -x "${PYTHON}" ]; then
    /usr/bin/osascript -e 'display alert "WebM2GIF" message "未找到 Python 运行环境，请先在项目目录执行 scripts/setup.sh。"' || true
    exit 1
fi

cd "${PROJECT}"
exec "${PYTHON}" -m webm2gif "$@"
"""

INFO_PLIST = {
    "CFBundleName": APP_NAME,
    "CFBundleDisplayName": "WebM → GIF",
    "CFBundleExecutable": APP_NAME,
    "CFBundleIdentifier": BUNDLE_IDENTIFIER,
    "CFBundleIconFile": "AppIcon",
    "CFBundleInfoDictionaryVersion": "6.0",
    "CFBundlePackageType": "APPL",
    "CFBundleShortVersionString": __version__,
    "CFBundleVersion": __version__,
    "LSMinimumSystemVersion": "12.0",
    "LSApplicationCategoryType": "public.app-category.video",
    "NSHighResolutionCapable": True,
    "NSHumanReadableCopyright": "",
    "NSSupportsAutomaticGraphicsSwitching": True,
    "CFBundleDocumentTypes": [
        {
            "CFBundleTypeName": "WebM 视频",
            "CFBundleTypeRole": "Viewer",
            "LSHandlerRank": "Alternate",
            "LSItemContentTypes": ["org.webmproject.webm"],
            "CFBundleTypeExtensions": ["webm"],
        }
    ],
}


def write_launcher(bundle: Path) -> None:
    target = bundle / "Contents" / "MacOS" / APP_NAME
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(LAUNCHER, encoding="utf-8")
    target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def write_info_plist(bundle: Path) -> None:
    contents = bundle / "Contents"
    contents.mkdir(parents=True, exist_ok=True)
    (contents / "PkgInfo").write_text("APPL????", encoding="ascii")
    with (contents / "Info.plist").open("wb") as handle:
        plistlib.dump(INFO_PLIST, handle)


MAKE_ICON = PROJECT_ROOT / "tools" / "make_icon.py"
#: Anything that changes the icon; used to spot a stale ``build/AppIcon.icns``.
ICON_INPUTS = (MAKE_ICON, PROJECT_ROOT / "packaging" / "AppIcon.png")


def icon_needs_rebuild(icon: Path) -> bool:
    """True when the icns is missing or older than the artwork/generator."""
    if not icon.exists():
        return True
    newest_input = max((path.stat().st_mtime for path in ICON_INPUTS if path.exists()), default=0.0)
    return icon.stat().st_mtime < newest_input


def copy_icon(bundle: Path, build_dir: Path) -> Path:
    resources = bundle / "Contents" / "Resources"
    resources.mkdir(parents=True, exist_ok=True)
    icon = build_dir / "AppIcon.icns"
    if icon_needs_rebuild(icon):
        subprocess.run(
            [sys.executable, str(MAKE_ICON), "--output", str(build_dir)],
            check=True,
        )
    shutil.copy2(icon, resources / "AppIcon.icns")
    return resources / "AppIcon.icns"


def copy_ffmpeg(bundle: Path, source: Path | None) -> Path | None:
    if source is None:
        return None
    target = bundle / "Contents" / "Resources" / "bin" / "ffmpeg"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return target


def ad_hoc_sign(bundle: Path) -> bool:
    """Ad-hoc sign the bundle; harmless if it fails."""
    completed = subprocess.run(
        ["codesign", "--force", "--deep", "--sign", "-", str(bundle)],
        capture_output=True,
        text=True,
    )
    return completed.returncode == 0


def build_lightweight(bundle: Path, build_dir: Path, ffmpeg: Path | None) -> None:
    if bundle.exists():
        shutil.rmtree(bundle)
    write_info_plist(bundle)
    write_launcher(bundle)
    copy_icon(bundle, build_dir)
    copy_ffmpeg(bundle, ffmpeg)


def build_standalone(bundle: Path, build_dir: Path, ffmpeg: Path | None) -> None:
    """Freeze the application so it no longer needs the virtual environment."""
    spec = PROJECT_ROOT / "packaging" / "webm2gif.spec"
    if not spec.exists():
        raise SystemExit("缺少 packaging/webm2gif.spec，无法构建独立版本")
    subprocess.run(
        [sys.executable, "-m", "PyInstaller", "--noconfirm", "--distpath", str(build_dir / "dist"),
         "--workpath", str(build_dir / "pyinstaller"), str(spec)],
        check=True,
        cwd=str(PROJECT_ROOT),
    )
    built = build_dir / "dist" / f"{APP_NAME}.app"
    if not built.exists():
        raise SystemExit(f"PyInstaller 未生成 {built}")
    if bundle.exists():
        shutil.rmtree(bundle)
    shutil.move(str(built), str(bundle))
    if ffmpeg is not None:
        copy_ffmpeg(bundle, ffmpeg)


def main() -> int:
    parser = argparse.ArgumentParser(description="构建 WebM2GIF.app")
    parser.add_argument("--output", default=str(PROJECT_ROOT / f"{APP_NAME}.app"), help=".app 输出路径")
    parser.add_argument("--build-dir", default=str(PROJECT_ROOT / "build"), help="中间产物目录")
    parser.add_argument("--ffmpeg", help="要打包进应用的 ffmpeg 路径")
    parser.add_argument("--no-ffmpeg", action="store_true", help="不要把 ffmpeg 复制进应用")
    parser.add_argument("--standalone", action="store_true", help="使用 PyInstaller 构建完全独立的版本")
    parser.add_argument("--no-sign", action="store_true", help="跳过 ad-hoc 签名")
    args = parser.parse_args()

    bundle = Path(args.output)
    build_dir = Path(args.build_dir)
    build_dir.mkdir(parents=True, exist_ok=True)

    ffmpeg_path = Path(args.ffmpeg) if args.ffmpeg else None
    if ffmpeg_path is None and not args.no_ffmpeg:
        found = find_ffmpeg()
        ffmpeg_path = Path(found.path) if found else None

    if args.standalone:
        build_standalone(bundle, build_dir, ffmpeg_path)
    else:
        build_lightweight(bundle, build_dir, ffmpeg_path)

    signed = False if args.no_sign else ad_hoc_sign(bundle)
    print(f"已生成 {bundle}")
    print(f"  ffmpeg: {'已内置 ' + str(ffmpeg_path) if ffmpeg_path else '未内置（运行时自动查找）'}")
    print(f"  签名  : {'ad-hoc 已签名' if signed else '未签名（本机使用不受影响）'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
