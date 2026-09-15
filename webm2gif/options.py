"""Conversion options: quality presets and the ffmpeg command they produce."""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Optional

from .ffmpeg import MediaInfo
from .hardware import HARDWARE_AUTO, HARDWARE_MODES, GPU_SCALE_FILTER


@dataclass(frozen=True)
class GifPreset:
    """A named bundle of GIF encoder settings."""

    key: str
    label: str
    fps: int
    max_width: int
    max_colors: int
    dither: str
    summary: str
    bayer_scale: int = 5


PRESETS: tuple[GifPreset, ...] = (
    GifPreset(
        key="high",
        label="高质量",
        fps=20,
        max_width=720,
        max_colors=256,
        dither="sierra2_4a",
        summary="高质量：20 fps · 720 px · 256 色 · 平滑抖动",
        bayer_scale=3,
    ),
    GifPreset(
        key="balanced",
        label="均衡（推荐）",
        fps=15,
        max_width=540,
        max_colors=256,
        dither="bayer",
        summary="均衡：15 fps · 540 px · 256 色（推荐）",
    ),
    GifPreset(
        key="small",
        label="小体积",
        fps=10,
        max_width=360,
        max_colors=128,
        dither="bayer",
        summary="小体积：10 fps · 360 px · 128 色",
    ),
)

PRESET_BY_KEY: dict[str, GifPreset] = {preset.key: preset for preset in PRESETS}
DEFAULT_PRESET_KEY = "balanced"

FPS_CHOICES: tuple[int, ...] = (10, 12, 15, 20, 24, 25, 30)

#: ``(label, width)``; a width of ``0`` means "keep the source resolution".
WIDTH_CHOICES: tuple[tuple[str, int], ...] = (
    ("跟随预设", -1),
    ("原始尺寸", 0),
    ("1080 px", 1080),
    ("720 px", 720),
    ("540 px", 540),
    ("480 px", 480),
    ("360 px", 360),
    ("240 px", 240),
)


def preset_for(key: str) -> GifPreset:
    """Look up a preset, falling back to the default one."""
    return PRESET_BY_KEY.get(key, PRESET_BY_KEY[DEFAULT_PRESET_KEY])


@dataclass(frozen=True)
class GifOptions:
    """User selectable conversion settings."""

    preset_key: str = DEFAULT_PRESET_KEY
    #: ``None`` keeps the preset value, ``0`` keeps the source frame rate/size.
    fps: Optional[int] = None
    width: Optional[int] = None
    loop: bool = True
    #: ``HARDWARE_OFF``/``HARDWARE_AUTO``/``HARDWARE_VIDEOTOOLBOX``.
    hardware: str = HARDWARE_AUTO
    #: Run the scaling filter on the GPU (``scale_vt``) instead of the CPU.
    gpu_scale: bool = False

    @property
    def preset(self) -> GifPreset:
        return preset_for(self.preset_key)

    def effective_fps(self) -> int:
        if self.fps is None:
            return self.preset.fps
        return self.fps if self.fps > 0 else self.preset.fps

    def target_width(self, source_width: int = 0) -> int:
        """Width to scale to; ``0`` means "do not scale"."""
        if self.width is None or self.width < 0:
            width = self.preset.max_width
        else:
            width = self.width
        if width <= 0:
            return 0
        if source_width and width >= source_width:
            return 0
        return width

    def describe(self, source_width: int = 0) -> str:
        fps = self.effective_fps()
        width = self.target_width(source_width)
        size = f"{width} px" if width else "原始尺寸"
        loop = "循环" if self.loop else "播放一次"
        return f"{fps} fps · {size} · {self.preset.max_colors} 色 · {loop}"

    @property
    def hardware_label(self) -> str:
        for key, label, _ in HARDWARE_MODES:
            if key == self.hardware:
                return label
        return self.hardware

    def gpu_scale_height(self, source_width: int, source_height: int) -> int:
        """Even output height for a given target width (``scale_vt`` needs it)."""
        if not source_width or not source_height:
            return 0
        height = int(round(source_height * self.target_width(source_width) / source_width))
        return max(2, height + height % 2)

    def can_gpu_scale(self, source_width: int, source_height: int) -> bool:
        """``scale_vt`` cannot infer the height, so both dimensions are required."""
        return bool(self.target_width(source_width)) and bool(self.gpu_scale_height(source_width, source_height))

    def filter_graph(
        self,
        source_width: int = 0,
        gpu_scale: bool = False,
        source_height: int = 0,
        hardware_frames: bool = False,
    ) -> str:
        """Build the ``palettegen``/``paletteuse`` filter graph.

        ``hardware_frames`` means the frames arrive from a dedicated
        VideoToolbox decoder (e.g. ``-c:v vp9_videotoolbox``) and therefore live
        in a hardware surface; ``hwdownload`` brings them back to memory before
        the software filters run. With ``-hwaccel videotoolbox`` ffmpeg already
        hands over software frames, so nothing has to be downloaded.

        ``gpu_scale`` moves the *scaling* step to the GPU (``scale_vt``); the
        palette filters and the GIF encoder always stay on the CPU. Software
        frames are uploaded for it (``hwupload``), and the result is downloaded
        before ``fps``.
        """
        preset = self.preset
        width = self.target_width(source_width)
        fps = f"fps={self.effective_fps()}"

        chain: list[str] = []
        if gpu_scale and self.can_gpu_scale(source_width, source_height):
            height = self.gpu_scale_height(source_width, source_height)
            if not hardware_frames:
                chain.append("hwupload")
            chain += [f"{GPU_SCALE_FILTER}=w={width}:h={height}", "hwdownload", "format=yuv420p"]
        else:
            if hardware_frames:
                chain.append("hwdownload")
            if width:
                chain.append(f"scale={width}:-1:flags=lanczos")
            elif hardware_frames:
                # Nothing else would convert the downloaded frame for palettegen.
                chain.append("format=yuv420p")
        chain.append(fps)
        chain.append("split[s0][s1]")

        dither = preset.dither
        if dither == "bayer":
            dither = f"bayer:bayer_scale={preset.bayer_scale}"

        return (
            ",".join(chain)
            + f";[s0]palettegen=max_colors={preset.max_colors}:stats_mode=diff[p]"
            + f";[s1][p]paletteuse=dither={dither}:diff_mode=rectangle"
        )

    def build_command(
        self,
        ffmpeg_path: str,
        source: str | os.PathLike[str],
        destination: str | os.PathLike[str],
        info: Optional[MediaInfo] = None,
        overwrite: bool = True,
        decoder: Optional[str] = None,
        gpu_scale: bool = False,
        hwaccel: Optional[str] = None,
    ) -> list[str]:
        """Full ``ffmpeg`` argv for one conversion.

        Both ``decoder`` (a dedicated VideoToolbox decoder such as
        ``vp9_videotoolbox``) and ``hwaccel`` (``videotoolbox``) are *input*
        options: they must come before ``-i`` to be used at all.
        """
        source_width = info.width if info else 0
        source_height = info.height if info else 0
        command = [
            ffmpeg_path,
            "-hide_banner",
            "-nostdin",
            "-loglevel",
            "error",
            "-progress",
            "pipe:1",
        ]
        if overwrite:
            command.append("-y")
        if gpu_scale:
            # scale_vt needs an actual device, otherwise hwupload has nothing
            # to upload to ("A hardware device reference is required").
            command += ["-init_hw_device", "videotoolbox=vt", "-filter_hw_device", "vt"]
        if hwaccel:
            command += ["-hwaccel", hwaccel]
        if decoder:
            command += ["-c:v", decoder]
        command += [
            "-i",
            os.fspath(source),
            "-filter_complex",
            self.filter_graph(
                source_width,
                gpu_scale=gpu_scale,
                source_height=source_height,
                hardware_frames=bool(decoder),
            ),
            "-loop",
            "0" if self.loop else "-1",
            os.fspath(destination),
        ]
        return command

    def with_changes(self, **changes) -> "GifOptions":
        return replace(self, **changes)


def unique_output_path(path: str | os.PathLike[str]) -> Path:
    """Return ``path`` or ``name (2).gif`` style variant that does not exist."""
    candidate = Path(path)
    if not candidate.exists():
        return candidate
    stem, suffix, parent = candidate.stem, candidate.suffix, candidate.parent
    index = 2
    while True:
        alternative = parent / f"{stem} ({index}){suffix}"
        if not alternative.exists():
            return alternative
        index += 1
