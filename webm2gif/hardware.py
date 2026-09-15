"""Apple Silicon hardware acceleration (VideoToolbox) support.

What can be accelerated, and what cannot:

* **Decoding** of VP8/VP9/H.264/HEVC can run on the media engine through
  ``VideoToolbox``. ffmpeg exposes it in two different ways, and which one
  exists depends on the build (and changed over ffmpeg versions):

  - older builds ship dedicated decoders (``-c:v vp9_videotoolbox``), which
    hand *hardware* frames to the filter graph;
  - current builds removed those decoders and only offer the generic
    ``-hwaccel videotoolbox`` input option, which downloads the frames back to
    memory before the filters run.

* **Scaling** can run on the GPU through the ``scale_vt`` filter.
* **GIF encoding itself is CPU only.** ``palettegen``/``paletteuse`` and the
  GIF/LZW muxer have no GPU or Neural Engine implementation in ffmpeg, and the
  Neural Engine is not reachable from ffmpeg at all (it is only exposed through
  CoreML / Metal Performance Shaders). So a GIF can never be *encoded* on the
  NPU.

Hardware decoding is not automatically faster: it is a different engine with a
fixed per-frame cost, while software VP9 decoding scales across all cores. The
self test therefore *measures* both paths and :func:`plan_decode` only turns
hardware decoding on in ``auto`` mode when it is not slower.
"""

from __future__ import annotations

import functools
import os
import re
import subprocess
import time
import unicodedata
from dataclasses import dataclass
from typing import Iterable, Optional

HARDWARE_OFF = "off"
HARDWARE_AUTO = "auto"
HARDWARE_VIDEOTOOLBOX = "videotoolbox"

HARDWARE_MODES: tuple[tuple[str, str, str], ...] = (
    (HARDWARE_AUTO, "自动（实测择优）", "自检实测硬件解码不慢于软件时才启用，否则保持 CPU"),
    (HARDWARE_OFF, "关闭（纯 CPU）", "完全使用 CPU，便于对比速度或排查问题"),
    (HARDWARE_VIDEOTOOLBOX, "强制 VideoToolbox", "强制硬件解码，可能与版本/机器相关地更慢"),
)

HARDWARE_MODE_KEYS = tuple(key for key, _, _ in HARDWARE_MODES)

#: Codec name (as reported by ``ffmpeg -i``) → VideoToolbox decoder name.
_VIDEOTOOLBOX_DECODERS: dict[str, str] = {
    "vp9": "vp9_videotoolbox",
    "vp09": "vp9_videotoolbox",
    "vp8": "vp8_videotoolbox",
    "vp08": "vp8_videotoolbox",
    "h264": "h264_videotoolbox",
    "avc1": "h264_videotoolbox",
    "hevc": "hevc_videotoolbox",
    "h265": "hevc_videotoolbox",
    "av1": "av1_videotoolbox",
    "prores": "prores_videotoolbox",
    "mjpeg": "mjpeg_videotoolbox",
}

_DECODER_LINE_RE = re.compile(r"^\s*[A-Z.]{6}\s+([A-Za-z0-9_]+)\s", re.MULTILINE)
_FILTER_LINE_RE = re.compile(r"^\s*[.A-Z]{1,3}\s+([A-Za-z0-9_]+)\s+[AVN]?->[AVN]?", re.MULTILINE)

#: ffmpeg logs a fallback instead of failing when the media engine cannot start.
_HW_FAILURE_RE = re.compile(
    r"(?i)kIOSurface|videotoolbox_vld|hwaccel.{0,40}?(fail|error|cannot|unable)"
)

GPU_SCALE_FILTER = "scale_vt"
#: Canonical codec names used when reporting capabilities to the user.
CANONICAL_CODECS: tuple[str, ...] = ("vp9", "vp8", "h264", "hevc", "av1", "prores", "mjpeg")

#: Codecs that actually appear inside WebM files.
WEBM_CODECS = ("vp9", "vp8")

_DETECT_TIMEOUT = 20.0
_SELFTEST_TIMEOUT = 120.0
#: Hardware decoding only wins above this margin (it also uses less CPU).
_MIN_SPEEDUP = 1.05
#: Shorter measurements are dominated by process start-up noise.
_MIN_MEASURABLE = 0.05


def _display_width(text: str) -> int:
    """Terminal columns taken by ``text`` (CJK labels count as two)."""
    return sum(2 if unicodedata.east_asian_width(char) in "WF" else 1 for char in text)


@dataclass(frozen=True)
class HardwareCapabilities:
    """What the current ffmpeg build offers for offloading work."""

    hwaccels: tuple[str, ...] = ()
    decoders: tuple[str, ...] = ()
    filters: tuple[str, ...] = ()
    detected: bool = False

    @property
    def videotoolbox_available(self) -> bool:
        """The build was compiled with VideoToolbox (listings only)."""
        return HARDWARE_VIDEOTOOLBOX in self.hwaccels

    @property
    def gpu_scale_available(self) -> bool:
        return GPU_SCALE_FILTER in self.filters

    @property
    def webm_decoders(self) -> tuple[str, ...]:
        """Hardware decoders able to read the codecs used by WebM."""
        return tuple(
            decoder
            for codec, decoder in _VIDEOTOOLBOX_DECODERS.items()
            if codec in WEBM_CODECS and decoder in self.decoders
        )

    @property
    def can_decode_webm_in_hardware(self) -> bool:
        """Either mechanism counts: a dedicated decoder, or ``-hwaccel``."""
        return bool(self.webm_decoders) or self.videotoolbox_available

    @property
    def supported_codecs(self) -> tuple[str, ...]:
        """Canonical codec names that have a hardware decoder in this build."""
        return tuple(
            codec for codec in CANONICAL_CODECS if _VIDEOTOOLBOX_DECODERS[codec] in self.decoders
        )

    def summary(self) -> str:
        """Short human readable status for the interface and the CLI."""
        if not self.detected:
            return "无法检测（ffmpeg 未运行）"
        if not self.videotoolbox_available:
            return "VideoToolbox 不可用（当前 ffmpeg 未启用硬件加速）"
        if self.webm_decoders:
            codecs = "/".join(name.split("_")[0].upper() for name in self.webm_decoders)
            suffix = "，GPU 缩放可用" if self.gpu_scale_available else ""
            return f"VideoToolbox 可用（{codecs} 硬件解码{suffix}）"
        suffix = "，GPU 缩放可用" if self.gpu_scale_available else ""
        return f"VideoToolbox 可用（通过 -hwaccel videotoolbox 解码 WebM{suffix}）"

    def notes(self) -> tuple[tuple[str, str], ...]:
        """``(label, text)`` footnotes stating what acceleration cannot do."""
        notes: list[tuple[str, str]] = []
        if not self.videotoolbox_available:
            notes.append(("提示", "当前 ffmpeg 未启用 VideoToolbox，可执行 brew install ffmpeg 后重试"))
        notes.append(("说明", "GIF 编码只能由 CPU 完成；NPU（神经网络引擎）无法被 ffmpeg 调用"))
        return tuple(notes)

    def describe(self) -> str:
        """Aligned multi-line report used by ``--check`` and the benchmark tool."""
        rows: list[tuple[str, str]] = [
            ("VideoToolbox", "可用" if self.videotoolbox_available else "不可用"),
            ("GPU 缩放", ("可用" if self.gpu_scale_available else "不可用") + f"（{GPU_SCALE_FILTER}）"),
            ("硬件解码器", ", ".join(self.supported_codecs) if self.supported_codecs else "无（使用 -hwaccel）"),
        ]
        rows.extend(self.notes())
        width = max(_display_width(label) for label, _ in rows)
        return "\n".join(f"{label}{' ' * (width - _display_width(label))} : {text}" for label, text in rows)


@dataclass(frozen=True)
class SelfTestResult:
    """Whether the hardware pipeline really runs on this machine right now."""

    decode_ok: bool = False
    scale_ok: bool = False
    detail: str = ""
    tested: bool = False
    #: Seconds for the same tiny decode through the media engine / on the CPU.
    hardware_seconds: float = 0.0
    software_seconds: float = 0.0

    @property
    def usable(self) -> bool:
        return self.decode_ok or self.scale_ok

    @property
    def speedup(self) -> float:
        """``>1`` means hardware decoding is faster than software decoding.

        ``0`` means "not measured": both runs have to last long enough that
        process start-up is not the dominant cost.
        """
        if self.hardware_seconds <= 0 or self.software_seconds <= _MIN_MEASURABLE:
            return 0.0
        return self.software_seconds / self.hardware_seconds

    @property
    def worth_using(self) -> bool:
        """Auto mode only enables hardware decoding when it pays off."""
        if not self.decode_ok:
            return False
        if self.speedup <= 0:
            return True  # nothing measured: trust the self test
        return self.speedup >= _MIN_SPEEDUP

    def summary(self) -> str:
        """Pipeline status; ``detail`` carries the ffmpeg error behind it."""
        if not self.tested:
            return "未检测"
        if self.decode_ok and self.scale_ok:
            base = "硬件解码与 GPU 缩放均可用"
        elif self.decode_ok:
            base = "硬件解码可用（GPU 缩放不可用）"
        elif self.scale_ok:
            base = "GPU 缩放可用（硬件解码不可用）"
        else:
            return "硬件解码与 GPU 缩放均不可用"
        if self.speedup > 0:
            verdict = "不慢于" if self.speedup >= 1 else "慢于"
            base += f"；解码实测{verdict}软件 {self.speedup:.2f}×"
        return base


def _run(
    ffmpeg_path: str, argument: str, timeout: float = _DETECT_TIMEOUT
) -> tuple[bool, str]:
    """Run a listing flag; ``(ran, output)`` so "no support" ≠ "no binary"."""
    try:
        completed = subprocess.run(
            [ffmpeg_path, "-hide_banner", argument],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return False, ""
    output = f"{completed.stdout or ''}\n{completed.stderr or ''}"
    return True, output


def _parse_listing(text: str, pattern: re.Pattern[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(pattern.findall(text)))


def detect_capabilities(ffmpeg_path: str) -> HardwareCapabilities:
    """Inspect the ffmpeg build (results are cached per binary)."""
    try:
        stamp = os.stat(ffmpeg_path).st_mtime
    except OSError:
        return HardwareCapabilities()
    return _detect_cached(ffmpeg_path, stamp)


@functools.lru_cache(maxsize=8)
def _detect_cached(ffmpeg_path: str, _stamp: float) -> HardwareCapabilities:
    # A build without VideoToolbox still "ran": say so, instead of pretending
    # that ffmpeg is missing.
    ran, hwaccel_output = _run(ffmpeg_path, "-hwaccels")
    if not ran:
        return HardwareCapabilities()

    hwaccels = tuple(
        line.strip()
        for line in hwaccel_output.splitlines()
        if line.strip() and not line.strip().endswith(":")
    )
    decoders = _parse_listing(_run(ffmpeg_path, "-decoders")[1], _DECODER_LINE_RE)
    filters = _parse_listing(_run(ffmpeg_path, "-filters")[1], _FILTER_LINE_RE)
    return HardwareCapabilities(hwaccels=hwaccels, decoders=decoders, filters=filters, detected=True)


@dataclass(frozen=True)
class DecodePlan:
    """How one file is decoded, and whether its frames stay in hardware."""

    #: ``-c:v <decoder>`` before ``-i`` (dedicated VideoToolbox decoder).
    decoder: Optional[str] = None
    #: ``-hwaccel <name>`` before ``-i`` (generic hardware acceleration).
    hwaccel: Optional[str] = None
    #: Run the scaling step on the GPU (``scale_vt``).
    gpu_scale: bool = False

    @property
    def hardware(self) -> bool:
        return bool(self.decoder or self.hwaccel)

    @property
    def hardware_frames(self) -> bool:
        """A dedicated decoder hands the filters hardware surfaces."""
        return bool(self.decoder)

    @property
    def label(self) -> str:
        """What actually ran, for the status column and the CLI."""
        parts = []
        if self.decoder:
            parts.append(self.decoder)
        elif self.hwaccel:
            parts.append(f"{self.hwaccel} 硬件解码")
        if self.gpu_scale:
            parts.append(GPU_SCALE_FILTER)
        return " + ".join(parts) or "软件解码"


def plan_decode(
    mode: str,
    capabilities: Optional[HardwareCapabilities],
    selftest: Optional[SelfTestResult] = None,
    gpu_scale: bool = False,
) -> DecodePlan:
    """Decide how to decode the input, honouring the acceleration mode.

    The source codec is deliberately not consulted: ffmpeg falls back to its
    software decoder by itself when the media engine cannot handle a codec, so
    the only question is whether VideoToolbox may be used at all.

    Two mechanisms exist for hardware decoding, and this uses the generic one
    (``-hwaccel videotoolbox``): it works on every VideoToolbox-enabled build,
    including current ffmpeg releases that dropped the dedicated
    ``vp9_videotoolbox`` style decoders. Those decoders are still *reported* by
    :class:`HardwareCapabilities`, because they are the strongest evidence that
    a build can use the media engine at all.

    ``HARDWARE_AUTO`` uses the measured self test: hardware decoding is only
    selected when it is not slower than software decoding on this machine.
    ``HARDWARE_VIDEOTOOLBOX`` forces it, whatever the measurement says.

    ``gpu_scale`` (``scale_vt``) implies software decoding: it uploads each
    frame to the GPU, which would fight with the download that ``-hwaccel``
    already performs, and mixing the two device setups is not supported by
    every build.
    """
    if gpu_scale and use_gpu_scale(mode, True, capabilities, selftest):
        return DecodePlan(gpu_scale=True)

    plan = DecodePlan()
    if mode == HARDWARE_OFF or capabilities is None or not capabilities.detected:
        return plan
    if not capabilities.videotoolbox_available:
        return plan

    # Forced use is unconditional; auto only when the measurement (if any) says
    # hardware decoding is not slower.
    measurement_ok = selftest is None or not selftest.tested or selftest.worth_using
    if mode == HARDWARE_VIDEOTOOLBOX or measurement_ok:
        plan = DecodePlan(hwaccel=HARDWARE_VIDEOTOOLBOX)
    return plan


def use_gpu_scale(
    mode: str,
    gpu_scale: bool,
    capabilities: Optional[HardwareCapabilities],
    selftest: Optional["SelfTestResult"] = None,
) -> bool:
    """GPU scaling needs a working VideoToolbox device, not just the filter."""
    if not gpu_scale or mode == HARDWARE_OFF or not capabilities:
        return False
    if not capabilities.gpu_scale_available:
        return False
    if selftest is None:
        selftest = run_self_test(capabilities=capabilities)
    return selftest.scale_ok


def prefer_hardware_ffmpeg(current, mode: str, extra_paths: Iterable[str] = ()):
    """Swap ``current`` for an ffmpeg that was built with VideoToolbox.

    Any such build (including the one inside ``imageio-ffmpeg``) can drive the
    media engine through ``-hwaccel``; builds without it cannot, whatever else
    they support. Returns the original binary when nothing better exists.
    """
    from .ffmpeg import FFmpegBinary, iter_candidates

    if mode == HARDWARE_OFF or current is None:
        return current
    if detect_capabilities(current.path).videotoolbox_available:
        return current

    current_path = os.path.abspath(current.path)
    for path, source in iter_candidates(None, extra_paths):
        candidate = os.path.abspath(path)
        if candidate == current_path:
            continue
        if detect_capabilities(candidate).videotoolbox_available:
            return FFmpegBinary(path=candidate, source=source)
    return current


def run_self_test(ffmpeg_path: str, capabilities: Optional[HardwareCapabilities] = None) -> SelfTestResult:
    """Check that VideoToolbox really works here (cached per binary).

    Capability listings are not enough: a build can advertise ``videotoolbox``
    while the media engine refuses to initialise (headless session, sandbox, or
    a machine without a supported decoder), and ``-hwaccel`` silently falls back
    when that happens. So this runs the real thing on a tiny generated clip and
    times both the hardware and the software path.
    """
    capabilities = capabilities or detect_capabilities(ffmpeg_path)
    if not capabilities.detected or not capabilities.videotoolbox_available:
        return SelfTestResult(detail="当前 ffmpeg 没有 VideoToolbox 支持", tested=True)
    try:
        stamp = os.stat(ffmpeg_path).st_mtime
    except OSError:
        return SelfTestResult(tested=True)
    return _selftest_cached(ffmpeg_path, stamp)


def _run_command(command: list[str], timeout: float = _SELFTEST_TIMEOUT) -> tuple[int, str]:
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as error:
        return 127, str(error)
    output = (completed.stderr or completed.stdout or "").strip()
    return completed.returncode, output


def _make_sample(ffmpeg_path: str, folder: str) -> Optional[str]:
    """A short VP9 clip used to measure decoding (VP9 is what WebM uses)."""
    sample = os.path.join(folder, "probe.webm")
    code, _ = _run_command(
        [
            ffmpeg_path, "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30:duration=1",
            "-c:v", "libvpx-vp9", "-b:v", "3M", "-pix_fmt", "yuv420p", "-deadline", "realtime", sample,
        ]
    )
    return sample if code == 0 and os.path.exists(sample) else None


def _time_decode(ffmpeg_path: str, sample: str, input_options: list[str], repeats: int = 2) -> tuple[bool, float, str]:
    """Best-of-``repeats`` wall time for one decode; ``ok`` means "really ran"."""
    best = 0.0
    detail = ""
    for _ in range(max(1, repeats)):
        started = time.monotonic()
        code, output = _run_command(
            [
                ffmpeg_path, "-hide_banner", "-nostdin", "-loglevel", "error",
                *input_options, "-i", sample, "-f", "null", "-",
            ]
        )
        elapsed = time.monotonic() - started
        if code != 0:
            lines = output.splitlines()
            return False, elapsed, lines[-1] if lines else f"退出码 {code}"
        if _HW_FAILURE_RE.search(output):
            # ffmpeg logs "Failed setup for format videotoolbox_vld" and then
            # decodes in software; the timing would be meaningless.
            lines = output.splitlines()
            return False, elapsed, lines[-1] if lines else "VideoToolbox 初始化失败"
        if best <= 0 or elapsed < best:
            best = elapsed
    return True, best, detail


@functools.lru_cache(maxsize=8)
def _selftest_cached(ffmpeg_path: str, _stamp: float) -> SelfTestResult:
    """Measure hardware vs software decoding on a tiny clip made on the spot.

    Both runs use exactly the flags :func:`plan_decode` would emit, so the
    measurement describes the real conversion and not a nicer-looking variant.
    """
    import tempfile

    with tempfile.TemporaryDirectory(prefix="webm2gif-selftest-") as folder:
        sample = _make_sample(ffmpeg_path, folder)
        if sample is None:
            return SelfTestResult(tested=True, detail="无法生成自检素材")

        hardware_input = ["-hwaccel", HARDWARE_VIDEOTOOLBOX]
        decode_ok, hardware_seconds, detail = _time_decode(ffmpeg_path, sample, hardware_input)
        _, software_seconds, _ = _time_decode(ffmpeg_path, sample, [])
        scale_ok, scale_detail = _scale_selftest(ffmpeg_path, sample)

    if not decode_ok and not detail:
        detail = scale_detail
    return SelfTestResult(
        decode_ok=decode_ok,
        scale_ok=scale_ok,
        detail=detail,
        tested=True,
        hardware_seconds=hardware_seconds if decode_ok else 0.0,
        software_seconds=software_seconds,
    )


def _scale_selftest(ffmpeg_path: str, sample: str) -> tuple[bool, str]:
    """Can ``scale_vt`` really run here (it needs a working VT device)?

    The chain mirrors what :func:`webm2gif.options.GifOptions.filter_graph`
    builds for GPU scaling: software frames are uploaded to the device, and
    ``scale_vt`` needs a real VideoToolbox device to upload them to.
    """
    chain = f"hwupload,{GPU_SCALE_FILTER}=w=640:h=360"
    code, output = _run_command(
        [
            ffmpeg_path, "-hide_banner", "-nostdin", "-loglevel", "error",
            "-init_hw_device", "videotoolbox=vt", "-filter_hw_device", "vt",
            "-i", sample,
            "-vf", f"{chain},hwdownload,format=yuv420p",
            "-frames:v", "1", "-f", "null", "-",
        ]
    )
    if code == 0 and not _HW_FAILURE_RE.search(output):
        return True, ""
    lines = output.splitlines()
    return False, lines[-1] if lines else f"退出码 {code}"
