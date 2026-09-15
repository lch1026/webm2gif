"""End-to-end conversion behaviour, driven by a fake ffmpeg."""

from __future__ import annotations

import threading
import time

import pytest

from webm2gif.converter import (
    CANCELLED,
    DONE,
    FAILED,
    ConversionCancelled,
    ConversionError,
    ConversionItem,
    convert_batch,
    convert_item,
)
from webm2gif.hardware import (
    HARDWARE_OFF,
    HARDWARE_VIDEOTOOLBOX,
    detect_capabilities,
    run_self_test,
)
from webm2gif.options import GifOptions


def make_item(source, output):
    return ConversionItem(source=source, output=output)


def test_conversion_produces_a_gif_and_reports_progress(fake_ffmpeg, sample_webm, tmp_path):
    item = make_item(sample_webm, tmp_path / "clip.gif")
    progress = []
    logs = []

    result = convert_item(
        str(fake_ffmpeg), item, GifOptions(), on_progress=progress.append, on_log=logs.append
    )

    assert result is item
    assert item.status == DONE
    assert item.output.read_bytes() == b"GIF89a"
    assert item.progress == 1.0
    assert progress, "on_progress should be called while ffmpeg runs"
    assert any("-filter_complex" in line for line in logs)


def test_media_info_is_probed_before_converting(fake_ffmpeg, sample_webm, tmp_path):
    item = make_item(sample_webm, tmp_path / "clip.gif")
    convert_item(str(fake_ffmpeg), item, GifOptions())
    assert item.info is not None and item.info.width == 1920
    assert item.resolution_text == "1920×1080"


def test_failure_is_reported_with_ffmpeg_output(monkeypatch, fake_ffmpeg, sample_webm, tmp_path):
    monkeypatch.setenv("FAKE_FFMPEG_FAIL", "1")
    item = make_item(sample_webm, tmp_path / "clip.gif")

    with pytest.raises(ConversionError) as error:
        convert_item(str(fake_ffmpeg), item, GifOptions())

    assert "Invalid data" in str(error.value)
    assert item.status == FAILED
    assert "Invalid data" in item.message


def test_unwritable_destination_fails(monkeypatch, fake_ffmpeg, sample_webm, tmp_path):
    item = make_item(sample_webm, tmp_path / "missing" / "clip.gif")
    with pytest.raises(ConversionError):
        convert_item(str(fake_ffmpeg), item, GifOptions())
    assert item.status == FAILED


def test_cancel_stops_ffmpeg_and_removes_partial_output(monkeypatch, fake_ffmpeg, sample_webm, tmp_path):
    monkeypatch.setenv("FAKE_FFMPEG_STEPS", "400")
    monkeypatch.setenv("FAKE_FFMPEG_STEP_DELAY", "0.05")
    cancel = threading.Event()
    item = make_item(sample_webm, tmp_path / "clip.gif")

    def stop_immediately(_item):
        cancel.set()

    with pytest.raises(ConversionCancelled):
        convert_item(str(fake_ffmpeg), item, GifOptions(), on_progress=stop_immediately, cancel_event=cancel)

    assert item.status == CANCELLED
    assert not item.output.exists()


def test_batch_keeps_going_after_a_failure(fake_ffmpeg, sample_webm, tmp_path):
    second_source = tmp_path / "second.webm"
    second_source.write_bytes(b"another clip")

    broken = make_item(sample_webm, tmp_path / "nope" / "broken.gif")
    good = make_item(second_source, tmp_path / "good.gif")

    convert_batch(str(fake_ffmpeg), [broken, good], GifOptions())

    assert broken.status == FAILED
    assert good.status == DONE
    assert good.output.exists()


def test_batch_marks_remaining_items_cancelled(fake_ffmpeg, sample_webm, tmp_path):
    first = make_item(sample_webm, tmp_path / "one.gif")
    second_source = tmp_path / "two.webm"
    second_source.write_bytes(b"two")
    second = make_item(second_source, tmp_path / "two.gif")

    cancel = threading.Event()
    cancel.set()

    convert_batch(str(fake_ffmpeg), [first, second], GifOptions(), cancel_event=cancel)

    assert first.status == CANCELLED and second.status == CANCELLED
    assert not first.output.exists()


def test_status_labels_are_human_readable(sample_webm, tmp_path):
    item = make_item(sample_webm, tmp_path / "clip.gif")
    assert item.status_label == "等待中"
    item.status = FAILED
    assert item.status_label == "失败"
    item.status = "running"
    item.progress = 0.42
    assert item.status_label == "转换中 42%"


# ------------------------------------------------- hardware accelerated runs
def recorded_runs(path):
    """Every ffmpeg argv the fake binary was called with."""
    return [line for line in path.read_text(encoding="utf-8").splitlines() if line]


def conversion_lines(path):
    return [line for line in recorded_runs(path) if "-filter_complex" in line]


def test_videotoolbox_decode_is_requested_when_the_build_supports_it(monkeypatch, fake_ffmpeg, sample_webm, tmp_path):
    args_file = tmp_path / "args.txt"
    monkeypatch.setenv("FAKE_FFMPEG_ARGS_FILE", str(args_file))
    capabilities = detect_capabilities(str(fake_ffmpeg))
    item = make_item(sample_webm, tmp_path / "clip.gif")

    convert_item(
        str(fake_ffmpeg),
        item,
        GifOptions(hardware=HARDWARE_VIDEOTOOLBOX),
        capabilities=capabilities,
        selftest=run_self_test(str(fake_ffmpeg), capabilities),
    )

    line = conversion_lines(args_file)[0]
    parts = line.split()
    assert item.status == DONE
    assert parts[parts.index("-hwaccel") + 1] == "videotoolbox"
    assert parts.index("-hwaccel") < parts.index("-i")
    assert "-c:v" not in parts, "专用 vp9_videotoolbox 解码器已被新版 ffmpeg 移除"
    assert "hwdownload" not in line, "-hwaccel 交回的是内存帧，无需下载"
    assert item.hardware == "videotoolbox 硬件解码"
    assert item.hardware_label == "videotoolbox 硬件解码"


def test_gpu_scaling_is_added_when_it_passed_the_self_test(monkeypatch, fake_ffmpeg, sample_webm, tmp_path):
    args_file = tmp_path / "args.txt"
    monkeypatch.setenv("FAKE_FFMPEG_ARGS_FILE", str(args_file))
    capabilities = detect_capabilities(str(fake_ffmpeg))
    item = make_item(sample_webm, tmp_path / "clip.gif")

    convert_item(
        str(fake_ffmpeg),
        item,
        GifOptions(hardware=HARDWARE_VIDEOTOOLBOX, gpu_scale=True),
        capabilities=capabilities,
        selftest=run_self_test(str(fake_ffmpeg), capabilities),
    )

    line = conversion_lines(args_file)[0]
    assert item.hardware == "scale_vt"
    assert "-init_hw_device videotoolbox=vt" in line
    assert "-hwaccel" not in line.split(), "GPU 缩放与 -hwaccel 抢同一个设备，不能同时用"
    assert "hwupload,scale_vt=w=" in line and "hwdownload" in line


def test_a_failing_hardware_attempt_falls_back_to_the_cpu(monkeypatch, fake_ffmpeg, sample_webm, tmp_path):
    args_file = tmp_path / "args.txt"
    monkeypatch.setenv("FAKE_FFMPEG_ARGS_FILE", str(args_file))
    monkeypatch.setenv("FAKE_FFMPEG_HW_DECODE_FAIL", "1")
    capabilities = detect_capabilities(str(fake_ffmpeg))
    logs = []
    item = make_item(sample_webm, tmp_path / "clip.gif")

    convert_item(
        str(fake_ffmpeg),
        item,
        GifOptions(hardware=HARDWARE_VIDEOTOOLBOX),
        on_log=logs.append,
        capabilities=capabilities,
    )

    attempts = conversion_lines(args_file)
    assert item.status == DONE and item.output.exists(), "the CPU retry must produce the GIF"
    assert len(attempts) == 2
    assert "-hwaccel" in attempts[0] and "-hwaccel" not in attempts[1]
    assert item.hardware == "软件解码（硬件回退）"
    assert any("自动改用 CPU" in line for line in logs)


def test_software_only_builds_never_ask_for_hardware(monkeypatch, fake_ffmpeg, sample_webm, tmp_path):
    monkeypatch.setenv("FAKE_FFMPEG_HW", "0")
    args_file = tmp_path / "args.txt"
    monkeypatch.setenv("FAKE_FFMPEG_ARGS_FILE", str(args_file))
    capabilities = detect_capabilities(str(fake_ffmpeg))
    assert capabilities.webm_decoders == ()
    item = make_item(sample_webm, tmp_path / "clip.gif")

    convert_item(str(fake_ffmpeg), item, GifOptions(), capabilities=capabilities)

    assert item.status == DONE
    assert "-c:v" not in conversion_lines(args_file)[0].split()
    assert item.hardware == "软件解码" and item.hardware_label == "软件解码"


def test_disabled_acceleration_keeps_the_command_software_only(monkeypatch, fake_ffmpeg, sample_webm, tmp_path):
    args_file = tmp_path / "args.txt"
    monkeypatch.setenv("FAKE_FFMPEG_ARGS_FILE", str(args_file))
    capabilities = detect_capabilities(str(fake_ffmpeg))
    item = make_item(sample_webm, tmp_path / "clip.gif")

    convert_item(str(fake_ffmpeg), item, GifOptions(hardware=HARDWARE_OFF), capabilities=capabilities)
    assert "-c:v" not in conversion_lines(args_file)[0].split()


# -------------------------------------------------------------- parallelism
def test_parallel_batch_is_faster_and_still_converts_everything(monkeypatch, fake_ffmpeg, sample_webm, tmp_path):
    monkeypatch.setenv("FAKE_FFMPEG_STEPS", "10")
    monkeypatch.setenv("FAKE_FFMPEG_STEP_DELAY", "0.05")

    def run_batch(max_workers, folder):
        folder.mkdir()
        items = [make_item(sample_webm, folder / f"clip{index}.gif") for index in range(3)]
        started = time.monotonic()
        convert_batch(str(fake_ffmpeg), items, GifOptions(), max_workers=max_workers)
        return time.monotonic() - started, items

    serial, serial_items = run_batch(1, tmp_path / "serial")
    parallel, parallel_items = run_batch(3, tmp_path / "parallel")

    assert all(item.status == DONE for item in serial_items + parallel_items)
    assert all(item.output.exists() for item in parallel_items)
    assert parallel < serial * 0.75, f"3 个并行任务没有节省时间（串行 {serial:.2f}s，并行 {parallel:.2f}s）"


def test_worker_count_is_capped_and_never_zero(fake_ffmpeg, sample_webm, tmp_path):
    items = [make_item(sample_webm, tmp_path / f"clip{index}.gif") for index in range(3)]
    convert_batch(str(fake_ffmpeg), items, GifOptions(), max_workers=999)
    assert all(item.status == DONE for item in items)

    empty: list = []
    assert convert_batch(str(fake_ffmpeg), empty, GifOptions(), max_workers=4) == []
