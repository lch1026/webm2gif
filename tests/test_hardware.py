"""VideoToolbox capability detection, self-test and decoder selection."""

from __future__ import annotations

import stat

import pytest

from webm2gif import hardware
from webm2gif.converter import auto_workers
from webm2gif.hardware import (
    HARDWARE_AUTO,
    HARDWARE_OFF,
    HARDWARE_VIDEOTOOLBOX,
    DecodePlan,
    SelfTestResult,
    detect_capabilities,
    plan_decode,
    prefer_hardware_ffmpeg,
    run_self_test,
    use_gpu_scale,
)
from webm2gif.ffmpeg import FFmpegBinary


def wrapper_ffmpeg(tmp_path, name, fake_ffmpeg, **env):
    """A copy of the fake ffmpeg that always runs with ``env`` set."""
    path = tmp_path / name
    exports = "\n".join(f'export {key}="{value}"' for key, value in env.items())
    path.write_text(f'#!/bin/sh\n{exports}\nexec "{fake_ffmpeg}" "$@"\n', encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


# ------------------------------------------------------------- detection
def test_capabilities_are_read_from_the_binary(fake_ffmpeg):
    caps = detect_capabilities(str(fake_ffmpeg))

    assert caps.detected and caps.videotoolbox_available
    assert "vp9_videotoolbox" in caps.decoders and "vp8_videotoolbox" in caps.decoders
    assert caps.gpu_scale_available
    assert set(caps.webm_decoders) == {"vp9_videotoolbox", "vp8_videotoolbox"}
    # The listings contain plenty of non hardware entries too.
    assert "vp9" in caps.decoders and "scale" in caps.filters


def test_a_software_only_build_reports_no_acceleration(tmp_path, fake_ffmpeg):
    binary = wrapper_ffmpeg(tmp_path, "ffmpeg-software", fake_ffmpeg, FAKE_FFMPEG_HW=0)
    caps = detect_capabilities(str(binary))

    assert caps.detected and not caps.videotoolbox_available
    assert caps.webm_decoders == ()
    assert not caps.gpu_scale_available
    assert "没有 WebM" in caps.summary() or "不可用" in caps.summary()


def test_missing_binary_is_reported_instead_of_raising(tmp_path):
    caps = detect_capabilities(str(tmp_path / "not-here"))
    assert not caps.detected
    assert caps.summary() == "无法检测（ffmpeg 未运行）"
    assert "不可用" in caps.describe()


def test_describe_explains_what_can_be_accelerated(fake_ffmpeg):
    text = detect_capabilities(str(fake_ffmpeg)).describe()
    assert "scale_vt" in text
    assert "NPU" in text, "the report must be honest about the Neural Engine"


# ------------------------------------------------------------- self test
def test_self_test_confirms_a_working_hardware_pipeline(fake_ffmpeg):
    caps = detect_capabilities(str(fake_ffmpeg))
    result = run_self_test(str(fake_ffmpeg), caps)

    assert result.tested and result.decode_ok and result.scale_ok
    assert result.usable
    assert "均可用" in result.summary()


def test_self_test_detects_a_broken_media_engine(monkeypatch, fake_ffmpeg):
    monkeypatch.setenv("FAKE_FFMPEG_HW_FAIL", "1")
    caps = detect_capabilities(str(fake_ffmpeg))
    result = run_self_test(str(fake_ffmpeg), caps)

    assert not result.decode_ok and not result.scale_ok
    assert "hwaccel initialisation returned error" in result.detail
    assert not result.worth_using


def test_self_test_is_skipped_without_videotoolbox(tmp_path, fake_ffmpeg):
    binary = wrapper_ffmpeg(tmp_path, "ffmpeg-software", fake_ffmpeg, FAKE_FFMPEG_HW=0)
    result = run_self_test(str(binary))

    assert result.tested and not result.usable
    assert "没有 VideoToolbox" in result.detail


# -------------------------------------------------------- decode planning
def test_the_portable_hwaccel_is_preferred_over_dedicated_decoders(fake_ffmpeg):
    """This build offers both mechanisms; ``-hwaccel`` works on every build."""
    caps = detect_capabilities(str(fake_ffmpeg))
    faster = SelfTestResult(decode_ok=True, tested=True, hardware_seconds=0.10, software_seconds=0.30)

    plan = plan_decode(HARDWARE_AUTO, caps, faster)
    assert plan.hwaccel == "videotoolbox" and plan.decoder is None
    assert plan.label == "videotoolbox 硬件解码"
    assert plan.hardware and not plan.hardware_frames
    assert plan_decode(HARDWARE_VIDEOTOOLBOX, caps, faster).hwaccel == "videotoolbox"


def test_disabling_acceleration_never_returns_a_hardware_plan(fake_ffmpeg):
    caps = detect_capabilities(str(fake_ffmpeg))
    assert plan_decode(HARDWARE_OFF, caps).label == "软件解码"
    assert plan_decode(HARDWARE_AUTO, None).label == "软件解码"


def test_software_only_build_never_returns_a_hardware_plan(tmp_path, fake_ffmpeg):
    binary = wrapper_ffmpeg(tmp_path, "ffmpeg-software", fake_ffmpeg, FAKE_FFMPEG_HW=0)
    caps = detect_capabilities(str(binary))
    assert plan_decode(HARDWARE_VIDEOTOOLBOX, caps).hardware is False
    assert plan_decode(HARDWARE_AUTO, caps).hardware is False


# ------------------------------------------------------------ GPU scale
def test_gpu_scale_needs_a_passing_self_test(fake_ffmpeg):
    caps = detect_capabilities(str(fake_ffmpeg))
    failed = SelfTestResult(decode_ok=True, scale_ok=False, tested=True)

    assert use_gpu_scale(HARDWARE_AUTO, True, caps, failed) is False
    assert use_gpu_scale(HARDWARE_AUTO, True, caps, SelfTestResult(scale_ok=True, tested=True)) is True
    assert use_gpu_scale(HARDWARE_AUTO, False, caps, SelfTestResult(scale_ok=True, tested=True)) is False
    assert use_gpu_scale(HARDWARE_OFF, True, caps, SelfTestResult(scale_ok=True, tested=True)) is False


def test_gpu_scale_is_refused_when_the_filter_is_missing(tmp_path, fake_ffmpeg):
    binary = wrapper_ffmpeg(tmp_path, "ffmpeg-software", fake_ffmpeg, FAKE_FFMPEG_HW=0)
    caps = detect_capabilities(str(binary))
    assert use_gpu_scale(HARDWARE_AUTO, True, caps, SelfTestResult(scale_ok=True, tested=True)) is False


# ------------------------------------------------- ffmpeg binary choice
def test_a_better_binary_is_swapped_in_for_hardware_decoding(tmp_path, fake_ffmpeg, monkeypatch):
    weak = FFmpegBinary(path=str(wrapper_ffmpeg(tmp_path, "weak", fake_ffmpeg, FAKE_FFMPEG_HW=0)), source="imageio")
    strong = wrapper_ffmpeg(tmp_path, "strong", fake_ffmpeg)
    # Pin the search order so the result does not depend on this machine's PATH.
    monkeypatch.setattr("webm2gif.ffmpeg.iter_candidates", lambda *_args: [(str(strong), "附加路径")])

    better = prefer_hardware_ffmpeg(weak, HARDWARE_AUTO, extra_paths=[str(strong)])

    assert better.path == str(strong)
    assert better.source == "附加路径"


def test_the_current_binary_is_kept_when_it_already_supports_webm(fake_ffmpeg):
    current = FFmpegBinary(path=str(fake_ffmpeg), source="测试")
    assert prefer_hardware_ffmpeg(current, HARDWARE_AUTO) is current


def test_nothing_is_swapped_when_acceleration_is_off(tmp_path, fake_ffmpeg):
    weak = FFmpegBinary(path=str(wrapper_ffmpeg(tmp_path, "weak", fake_ffmpeg, FAKE_FFMPEG_HW=0)), source="imageio")
    assert prefer_hardware_ffmpeg(weak, HARDWARE_OFF, extra_paths=[str(fake_ffmpeg)]) is weak
    assert prefer_hardware_ffmpeg(None, HARDWARE_AUTO) is None


# ------------------------------------------------------------ workers
def test_auto_workers_uses_about_half_the_cores(monkeypatch):
    monkeypatch.setattr("os.cpu_count", lambda: 10)
    assert auto_workers() == 4  # capped, so a long batch cannot swamp the machine
    monkeypatch.setattr("os.cpu_count", lambda: 4)
    assert auto_workers() == 2
    monkeypatch.setattr("os.cpu_count", lambda: 2)
    assert auto_workers() == 1
    monkeypatch.setattr("os.cpu_count", lambda: None)
    assert auto_workers() == 2


def test_auto_workers_never_exceeds_the_queue_length(monkeypatch):
    monkeypatch.setattr("os.cpu_count", lambda: 12)
    assert auto_workers(1) == 1
    assert auto_workers(3) == 3


@pytest.mark.parametrize("label", ["HARDWARE_OFF", "HARDWARE_AUTO", "HARDWARE_VIDEOTOOLBOX"])
def test_every_mode_has_a_human_label(label):
    assert getattr(hardware, label) in hardware.HARDWARE_MODE_KEYS


# ------------------------------------------------------------- decode plan
def test_a_build_without_dedicated_decoders_uses_the_generic_hwaccel():
    # Modern ffmpeg builds only offer "-hwaccel videotoolbox" for WebM.
    capabilities = hardware.HardwareCapabilities(
        hwaccels=("videotoolbox",), decoders=("vp9", "vp8"), filters=("scale_vt",), detected=True
    )
    plan = plan_decode(HARDWARE_VIDEOTOOLBOX, capabilities)

    assert plan == DecodePlan(hwaccel="videotoolbox")
    assert plan.hardware and not plan.hardware_frames
    assert plan.label == "videotoolbox 硬件解码"


def test_auto_uses_hardware_when_it_has_not_been_measured():
    capabilities = hardware.HardwareCapabilities(
        hwaccels=("videotoolbox",), decoders=("vp9_videotoolbox",), detected=True
    )
    assert plan_decode(HARDWARE_AUTO, capabilities).hwaccel == "videotoolbox"


def test_auto_keeps_the_cpu_when_hardware_measured_slower():
    capabilities = hardware.HardwareCapabilities(
        hwaccels=("videotoolbox",), decoders=("vp9_videotoolbox",), detected=True
    )
    slower = SelfTestResult(decode_ok=True, tested=True, hardware_seconds=0.30, software_seconds=0.12)

    assert slower.speedup < 1 and not slower.worth_using
    assert plan_decode(HARDWARE_AUTO, capabilities, slower).label == "软件解码"
    # Forcing it still works: the measurement is advice, not a block.
    forced = plan_decode(HARDWARE_VIDEOTOOLBOX, capabilities, slower)
    assert forced.hwaccel == "videotoolbox" and forced.label == "videotoolbox 硬件解码"


def test_auto_uses_hardware_when_it_measured_faster():
    capabilities = hardware.HardwareCapabilities(hwaccels=("videotoolbox",), decoders=("vp9",), detected=True)
    faster = SelfTestResult(decode_ok=True, tested=True, hardware_seconds=0.10, software_seconds=0.30)

    assert faster.speedup == pytest.approx(3.0) and faster.worth_using
    assert plan_decode(HARDWARE_AUTO, capabilities, faster).hwaccel == "videotoolbox"


def test_tiny_measurements_are_not_trusted():
    """Both runs are dominated by process start-up, so nothing is concluded."""
    noisy = SelfTestResult(decode_ok=True, tested=True, hardware_seconds=0.003, software_seconds=0.004)

    assert noisy.speedup == 0.0
    assert noisy.worth_using, "unmeasurable runs must not disable acceleration"


def test_a_broken_self_test_keeps_auto_on_the_cpu():
    capabilities = hardware.HardwareCapabilities(hwaccels=("videotoolbox",), detected=True)
    broken = SelfTestResult(decode_ok=False, tested=True, detail="boom")

    assert not broken.worth_using
    assert plan_decode(HARDWARE_AUTO, capabilities, broken).label == "软件解码"


def test_software_only_builds_yield_a_software_plan():
    capabilities = hardware.HardwareCapabilities(hwaccels=(), decoders=("vp9",), detected=True)
    for mode in (HARDWARE_AUTO, HARDWARE_VIDEOTOOLBOX, HARDWARE_OFF):
        assert plan_decode(mode, capabilities).label == "软件解码"
    assert plan_decode(HARDWARE_OFF, capabilities).hardware is False


def test_gpu_scaling_is_added_to_the_plan(monkeypatch, fake_ffmpeg):
    capabilities = detect_capabilities(str(fake_ffmpeg))
    working = SelfTestResult(decode_ok=True, scale_ok=True, tested=True)

    plan = plan_decode(HARDWARE_AUTO, capabilities, working, gpu_scale=True)
    assert plan == DecodePlan(gpu_scale=True)
    assert plan.gpu_scale and not plan.hardware, "GPU 缩放与 -hwaccel 抢同一个设备，只能走软件解码"
    assert plan.label == "scale_vt"

    # Without a passing scale self test the flag is dropped again.
    assert plan_decode(HARDWARE_AUTO, capabilities, working, gpu_scale=False).gpu_scale is False
    skipped = plan_decode(HARDWARE_AUTO, capabilities, SelfTestResult(tested=True), gpu_scale=True)
    assert skipped.gpu_scale is False


def test_self_test_records_both_timings_on_a_healthy_build(fake_ffmpeg):
    capabilities = detect_capabilities(str(fake_ffmpeg))
    result = run_self_test(str(fake_ffmpeg), capabilities)

    assert result.decode_ok and result.scale_ok
    assert result.software_seconds > 0
    assert result.hardware_seconds > 0
    # The fake decodes instantly, so the ratio is not trusted (see speedup).
    assert result.speedup == 0.0 and result.worth_using
