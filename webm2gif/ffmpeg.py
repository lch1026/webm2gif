"""Locating the ``ffmpeg`` binary and reading media metadata.

The application never assumes that ``ffmpeg`` is installed on ``PATH``: a copy
shipped inside the application bundle (or provided by the ``imageio-ffmpeg``
wheel) is preferred so that the converter also works on a clean Mac.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator, Optional

ENV_OVERRIDE = "WEBM2GIF_FFMPEG"
ENV_EXTRA_PATHS = "WEBM2GIF_FFMPEG_PATHS"

_SYSTEM_CANDIDATES: tuple[tuple[str, str], ...] = (
    ("/opt/homebrew/bin/ffmpeg", "Homebrew"),
    ("/usr/local/bin/ffmpeg", "Homebrew"),
    ("/opt/local/bin/ffmpeg", "MacPorts"),
    ("/usr/bin/ffmpeg", "系统"),
)

_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d{2}):(\d{2}(?:\.\d+)?)")
_DIMENSIONS_RE = re.compile(r"(\d{2,5})x(\d{2,5})")
_CODEC_RE = re.compile(r"Video:\s*([A-Za-z0-9_.\-]+)")


@dataclass(frozen=True)
class FFmpegBinary:
    """A usable ``ffmpeg`` executable plus a human readable origin."""

    path: str
    source: str

    def version(self, timeout: float = 10.0) -> str:
        """Return the first ``ffmpeg -version`` line, or an empty string."""
        try:
            completed = subprocess.run(
                [self.path, "-version"],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except (OSError, subprocess.SubprocessError):
            return ""
        first_line = (completed.stdout or "").splitlines()
        return first_line[0].strip() if first_line else ""


@dataclass(frozen=True)
class MediaInfo:
    """Metadata read from the input file by ``ffmpeg -i``."""

    duration: float = 0.0
    width: int = 0
    height: int = 0
    codec: str = ""

    @property
    def resolution_text(self) -> str:
        if self.width and self.height:
            return f"{self.width}×{self.height}"
        return "—"

    @property
    def duration_text(self) -> str:
        if self.duration <= 0:
            return "—"
        minutes, seconds = divmod(self.duration, 60)
        return f"{int(minutes)}:{seconds:04.1f}"


def resource_root() -> Path:
    """Directory that contains resources shipped with the application."""
    bundle_dir = getattr(sys, "_MEIPASS", None)
    if bundle_dir:
        return Path(bundle_dir)
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent.parent / "Resources"
    return Path(__file__).resolve().parent.parent


def bundled_candidates() -> list[Path]:
    """Paths where a self-contained ``ffmpeg`` may live."""
    root = resource_root()
    candidates = [
        root / "bin" / "ffmpeg",
        root / "Resources" / "bin" / "ffmpeg",
    ]
    if getattr(sys, "frozen", False):
        # .app 里 sys._MEIPASS 指向 Contents/Frameworks，而打包脚本把 ffmpeg 放在
        # Contents/Resources/bin，两个目录并不重合，所以再按可执行文件的位置找一遍。
        contents = Path(sys.executable).resolve().parent.parent
        candidates.append(contents / "Resources" / "bin" / "ffmpeg")
        candidates.append(contents / "Frameworks" / "bin" / "ffmpeg")
    vendor = root / "vendor"
    if vendor.is_dir():
        candidates.extend(sorted(p for p in vendor.glob("ffmpeg*") if p.is_file()))
    return candidates


def _is_executable(path: str | os.PathLike[str]) -> bool:
    try:
        return os.path.isfile(path) and os.access(path, os.X_OK)
    except OSError:
        return False


def _imageio_ffmpeg_path() -> Optional[str]:
    try:
        import imageio_ffmpeg  # type: ignore import-not-found
    except Exception:
        return None
    try:
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def _extra_paths() -> Iterator[str]:
    raw = os.environ.get(ENV_EXTRA_PATHS, "")
    for chunk in raw.split(os.pathsep):
        chunk = chunk.strip()
        if chunk:
            yield chunk


def iter_candidates(
    preferred: Optional[str] = None,
    extra_paths: Iterable[str] = (),
) -> Iterator[tuple[str, str]]:
    """Yield ``(path, source)`` pairs in order of preference."""
    seen: set[str] = set()

    def emit(path: str | os.PathLike[str] | None, source: str) -> Iterator[tuple[str, str]]:
        if not path:
            return
        text = os.fspath(path)
        key = os.path.abspath(text)
        if key in seen:
            return
        seen.add(key)
        yield text, source

    yield from emit(preferred, "自定义")
    yield from emit(os.environ.get(ENV_OVERRIDE), "环境变量")
    for candidate in bundled_candidates():
        yield from emit(candidate, "内置")
    yield from emit(_imageio_ffmpeg_path(), "imageio-ffmpeg")
    yield from emit(shutil.which("ffmpeg"), "系统 PATH")
    for candidate, source in _SYSTEM_CANDIDATES:
        yield from emit(candidate, source)
    for candidate in extra_paths:
        yield from emit(candidate, "附加路径")
    for candidate in _extra_paths():
        yield from emit(candidate, "环境变量 PATH")


def find_ffmpeg(
    preferred: Optional[str] = None,
    extra_paths: Iterable[str] = (),
) -> Optional[FFmpegBinary]:
    """Return the first usable ``ffmpeg`` binary, or ``None``."""
    for path, source in iter_candidates(preferred, extra_paths):
        if _is_executable(path):
            return FFmpegBinary(path=os.path.abspath(path), source=source)
    return None


def probe_media(ffmpeg_path: str, path: str | os.PathLike[str], timeout: float = 30.0) -> MediaInfo:
    """Read duration/size/codec of ``path`` by parsing ``ffmpeg -i`` output."""
    try:
        completed = subprocess.run(
            [ffmpeg_path, "-hide_banner", "-nostdin", "-i", os.fspath(path)],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        output = f"{completed.stdout or ''}\n{completed.stderr or ''}"
    except (OSError, subprocess.SubprocessError):
        return MediaInfo()

    duration = 0.0
    match = _DURATION_RE.search(output)
    if match:
        hours, minutes, seconds = match.groups()
        duration = int(hours) * 3600 + int(minutes) * 60 + float(seconds)

    width = height = 0
    codec = ""
    for line in output.splitlines():
        if "Video:" not in line:
            continue
        codec_match = _CODEC_RE.search(line)
        if codec_match:
            codec = codec_match.group(1)
        dimensions = _DIMENSIONS_RE.search(line)
        if dimensions:
            width, height = int(dimensions.group(1)), int(dimensions.group(2))
        if width and codec:
            break

    return MediaInfo(duration=duration, width=width, height=height, codec=codec)
