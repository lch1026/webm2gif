"""Turn queued WebM files into GIFs, reporting progress and honouring cancel.

Two things make this fast on an Apple Silicon Mac:

* several files are converted in parallel (``max_workers``), which uses the
  performance *and* efficiency cores at once;
* decoding (and optionally scaling) can run on the media engine/GPU through
  VideoToolbox, while the GIF palette/encoder stays on the CPU — see
  :mod:`webm2gif.hardware` for why that split is unavoidable.
"""

from __future__ import annotations

import os
import queue
import re
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Optional, Sequence

from .discovery import output_for
from .ffmpeg import MediaInfo, probe_media
from .hardware import (
    DecodePlan,
    HardwareCapabilities,
    SelfTestResult,
    plan_decode,
)
from .options import GifOptions

PENDING = "pending"
RUNNING = "running"
DONE = "done"
FAILED = "failed"
CANCELLED = "cancelled"

STATUS_LABELS = {
    PENDING: "等待中",
    RUNNING: "转换中",
    DONE: "已完成",
    FAILED: "失败",
    CANCELLED: "已取消",
}

_OUT_TIME_RE = re.compile(r"out_time=(-?\d+):(\d{2}):(\d{2}(?:\.\d+)?)")

ProgressCallback = Callable[["ConversionItem"], None]
LogCallback = Callable[[str], None]

#: Never run more than this many ffmpeg processes at once.
MAX_WORKERS_CAP = 8


class ConversionError(RuntimeError):
    """ffmpeg reported a failure."""


class ConversionCancelled(RuntimeError):
    """The user cancelled while ffmpeg was running."""


@dataclass
class ConversionItem:
    """One queued ``.webm`` → ``.gif`` conversion."""

    source: Path
    output: Path
    info: Optional[MediaInfo] = None
    status: str = PENDING
    message: str = ""
    progress: float = 0.0
    #: What actually ran this file, e.g. ``"videotoolbox 硬件解码"`` or ``"scale_vt"``.
    hardware: str = ""

    @property
    def status_label(self) -> str:
        if self.status == RUNNING:
            return f"转换中 {int(self.progress * 100)}%"
        return STATUS_LABELS.get(self.status, self.status)

    @property
    def resolution_text(self) -> str:
        return self.info.resolution_text if self.info else "—"

    @property
    def output_name(self) -> str:
        return self.output.name

    @property
    def hardware_label(self) -> str:
        return self.hardware or "软件解码"


def auto_workers(item_count: int = 0) -> int:
    """Sensible default parallelism: about half the cores, capped."""
    cores = os.cpu_count() or 4
    workers = max(1, min(4, cores // 2))
    if item_count:
        workers = max(1, min(workers, item_count))
    return workers


def assign_outputs(
    items: Sequence[ConversionItem],
    output_dir: Optional[str | os.PathLike[str]] = None,
) -> None:
    """Point every item at its destination GIF, never overwriting existing files."""
    used: set[str] = set()
    for item in items:
        base = output_for(item.source, output_dir)
        target = base
        index = 2
        # Skip existing files, and skip names already handed out in this batch
        # (two inputs with the same stem must not fight over one output file).
        while target.exists() or str(target) in used:
            target = base.with_name(f"{base.stem} ({index}){base.suffix}")
            index += 1
        used.add(str(target))
        item.output = target


def plan_items(
    sources: Iterable[str | os.PathLike[str]],
    output_dir: Optional[str | os.PathLike[str]] = None,
) -> list[ConversionItem]:
    """Build conversions for ``sources``, avoiding overwriting existing GIFs."""
    items = [ConversionItem(source=Path(source), output=Path(source).with_suffix(".gif")) for source in sources]
    assign_outputs(items, output_dir)
    return items


def _pump(stream, kind: str, sink: "queue.Queue[tuple[str, str]]") -> None:
    try:
        for line in stream:
            sink.put((kind, line))
    except (ValueError, OSError):
        pass
    finally:
        try:
            stream.close()
        except Exception:
            pass


def _seconds_from_out_time(line: str) -> Optional[float]:
    match = _OUT_TIME_RE.search(line)
    if not match:
        return None
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def _remove_partial(path: Path) -> None:
    try:
        if path.exists():
            path.unlink()
    except OSError:
        pass


def _run_ffmpeg(
    command: Sequence[str],
    item: ConversionItem,
    on_progress: Optional[ProgressCallback],
    on_log: Optional[LogCallback],
    cancel_event: Optional[threading.Event],
) -> tuple[int, list[str]]:
    """Run one ffmpeg process, feeding progress back to the caller."""
    try:
        process = subprocess.Popen(
            list(command),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            stdin=subprocess.DEVNULL,
        )
    except OSError as error:
        raise ConversionError(f"无法启动 ffmpeg：{error}") from error

    sink: "queue.Queue[tuple[str, str]]" = queue.Queue()
    readers = [
        threading.Thread(target=_pump, args=(process.stdout, "progress", sink), daemon=True),
        threading.Thread(target=_pump, args=(process.stderr, "error", sink), daemon=True),
    ]
    for reader in readers:
        reader.start()

    duration = item.info.duration if item.info else 0.0
    errors: list[str] = []
    cancelled = False
    last_report = 0.0
    started = os.times().elapsed if hasattr(os.times(), "elapsed") else 0.0

    while True:
        if cancel_event is not None and cancel_event.is_set() and process.poll() is None:
            cancelled = True
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()

        try:
            kind, line = sink.get(timeout=0.15)
        except queue.Empty:
            if process.poll() is not None and not any(reader.is_alive() for reader in readers) and sink.empty():
                break
            continue

        if kind == "error":
            stripped = line.strip()
            if stripped:
                errors.append(stripped)
                if on_log:
                    on_log(stripped)
            continue

        seconds = _seconds_from_out_time(line)
        if seconds is not None and duration > 0:
            item.progress = max(0.0, min(1.0, seconds / duration))
            now = os.times().elapsed if hasattr(os.times(), "elapsed") else started
            if on_progress and now - last_report > 0.05:
                last_report = now
                on_progress(item)

    return_code = process.wait()
    if cancelled:
        raise ConversionCancelled("已取消")
    return return_code, errors


def convert_item(
    ffmpeg_path: str,
    item: ConversionItem,
    options: GifOptions,
    on_progress: Optional[ProgressCallback] = None,
    on_log: Optional[LogCallback] = None,
    cancel_event: Optional[threading.Event] = None,
    probe: bool = True,
    capabilities: Optional[HardwareCapabilities] = None,
    selftest: Optional[SelfTestResult] = None,
) -> ConversionItem:
    """Convert a single item, updating it in place and returning it.

    When hardware decoding was requested but fails, the conversion is retried
    once with the CPU decoder so a GIF is still produced.
    """
    if probe and item.info is None:
        item.info = probe_media(ffmpeg_path, item.source)

    plan = plan_decode(options.hardware, capabilities, selftest, gpu_scale=options.gpu_scale)

    # Anything accelerated is retried once on the plain CPU path: a machine can
    # lose its media engine (sleep, screen sharing, another app holding it) and
    # a GIF is better than an error message.
    attempts: list[DecodePlan] = [plan]
    if plan.hardware or plan.gpu_scale:
        attempts.append(DecodePlan())

    item.status = RUNNING
    item.progress = 0.0
    item.message = ""
    item.hardware = plan.label

    for attempt_index, attempt in enumerate(attempts):
        command = options.build_command(
            ffmpeg_path,
            item.source,
            item.output,
            item.info,
            decoder=attempt.decoder,
            gpu_scale=attempt.gpu_scale,
            hwaccel=attempt.hwaccel,
        )
        if on_log:
            on_log("$ " + " ".join(command))

        try:
            return_code, errors = _run_ffmpeg(command, item, on_progress, on_log, cancel_event)
        except ConversionCancelled:
            item.status = CANCELLED
            item.progress = 0.0
            item.message = "已取消"
            _remove_partial(item.output)
            raise

        if return_code == 0 and item.output.exists():
            item.status = DONE
            item.progress = 1.0
            if on_progress:
                on_progress(item)
            return item

        detail = errors[-1] if errors else f"ffmpeg 退出码 {return_code}"
        if (attempt.hardware or attempt.gpu_scale) and attempt_index + 1 < len(attempts):
            if on_log:
                on_log(f"⚠ 硬件加速失败（{detail}），自动改用 CPU 重试")
            item.hardware = "软件解码（硬件回退）"
            item.progress = 0.0
            _remove_partial(item.output)
            continue

        item.status = FAILED
        item.message = detail
        raise ConversionError(detail)

    raise ConversionError("转换未能完成")  # pragma: no cover - loop always returns/raises


def _convert_one(
    ffmpeg_path: str,
    item: ConversionItem,
    options: GifOptions,
    on_item_update: Optional[ProgressCallback],
    on_log: Optional[LogCallback],
    cancel_event: Optional[threading.Event],
    capabilities: Optional[HardwareCapabilities],
    selftest: Optional[SelfTestResult],
) -> ConversionItem:
    if item.status == PENDING and cancel_event is not None and cancel_event.is_set():
        item.status = CANCELLED
        item.message = "已取消"
        if on_item_update:
            on_item_update(item)
        return item
    try:
        convert_item(
            ffmpeg_path,
            item,
            options,
            on_progress=on_item_update,
            on_log=on_log,
            cancel_event=cancel_event,
            capabilities=capabilities,
            selftest=selftest,
        )
    except ConversionCancelled:
        if on_item_update:
            on_item_update(item)
    except ConversionError as error:
        if on_log:
            on_log(f"✗ {item.source.name}: {error}")
        if on_item_update:
            on_item_update(item)
    return item


def convert_batch(
    ffmpeg_path: str,
    items: Sequence[ConversionItem],
    options: GifOptions,
    on_item_update: Optional[ProgressCallback] = None,
    on_log: Optional[LogCallback] = None,
    cancel_event: Optional[threading.Event] = None,
    max_workers: int = 1,
    capabilities: Optional[HardwareCapabilities] = None,
    selftest: Optional[SelfTestResult] = None,
) -> list[ConversionItem]:
    """Convert every item; a failure never stops the rest of the batch."""
    batch = list(items)
    workers = max(1, min(int(max_workers or 1), MAX_WORKERS_CAP))

    def run(item: ConversionItem) -> ConversionItem:
        return _convert_one(
            ffmpeg_path, item, options, on_item_update, on_log, cancel_event, capabilities, selftest
        )

    if workers == 1 or len(batch) <= 1:
        for item in batch:
            run(item)
        return batch

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="webm2gif") as pool:
        list(pool.map(run, batch))
    return batch


@dataclass
class BatchSummary:
    done: int = 0
    failed: int = 0
    cancelled: int = 0
    pending: int = 0

    @property
    def total(self) -> int:
        return self.done + self.failed + self.cancelled + self.pending


def summarize(items: Iterable[ConversionItem]) -> BatchSummary:
    summary = BatchSummary()
    for item in items:
        if item.status == DONE:
            summary.done += 1
        elif item.status == FAILED:
            summary.failed += 1
        elif item.status == CANCELLED:
            summary.cancelled += 1
        else:
            summary.pending += 1
    return summary
