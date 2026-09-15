"""Locating ffmpeg and reading media information."""

from __future__ import annotations

import shutil
import sys

from webm2gif.ffmpeg import FFmpegBinary, MediaInfo, find_ffmpeg, iter_candidates, probe_media


def test_probe_reads_duration_size_and_codec(fake_ffmpeg, sample_webm):
    info = probe_media(str(fake_ffmpeg), sample_webm)
    assert info.duration == 12.5
    assert (info.width, info.height) == (1920, 1080)
    assert info.codec == "vp9"
    assert info.resolution_text == "1920×1080"
    assert info.duration_text == "0:12.5"


def test_probe_of_a_broken_binary_is_not_fatal(tmp_path):
    assert probe_media("/nonexistent/ffmpeg", tmp_path / "missing.webm") == MediaInfo()


def test_resolution_text_without_dimensions():
    assert MediaInfo().resolution_text == "—"
    assert MediaInfo().duration_text == "—"


def test_explicit_binary_wins(fake_ffmpeg):
    found = find_ffmpeg(preferred=str(fake_ffmpeg))
    assert found is not None
    assert found.path == str(fake_ffmpeg)
    assert found.source == "自定义"


def test_non_executable_candidates_are_skipped(tmp_path):
    broken = tmp_path / "ffmpeg"
    broken.write_text("#!/bin/sh\n", encoding="utf-8")  # not executable
    found = find_ffmpeg(preferred=str(broken))
    assert found is None or found.path != str(broken)


def test_bundled_copy_is_preferred_over_path(monkeypatch, tmp_path, fake_ffmpeg):
    bundle_root = tmp_path / "Bundle.app" / "Contents" / "Resources"
    (bundle_root / "bin").mkdir(parents=True)
    shutil.copy2(fake_ffmpeg, bundle_root / "bin" / "ffmpeg")

    monkeypatch.setattr("webm2gif.ffmpeg.resource_root", lambda: bundle_root)
    found = find_ffmpeg()
    assert found is not None
    assert found.source == "内置"
    assert found.path == str(bundle_root / "bin" / "ffmpeg")


def test_bundled_copy_is_found_in_a_frozen_app_layout(monkeypatch, tmp_path, fake_ffmpeg):
    """回归测试：.app 里的 _MEIPASS 与 ffmpeg 所在的 Resources 不是同一个目录。

    PyInstaller 让 sys._MEIPASS 指向 Contents/Frameworks，而 build_app.py 把
    ffmpeg 放在 Contents/Resources/bin/ffmpeg；两者对不上时，独立版会跳过自带
    的 ffmpeg 去用系统 PATH 里的版本，没装 ffmpeg 的机器就直接失败。
    """
    monkeypatch.delenv("WEBM2GIF_FFMPEG", raising=False)
    contents = tmp_path / "WebM2GIF.app" / "Contents"
    bundled = contents / "Resources" / "bin" / "ffmpeg"
    bundled.parent.mkdir(parents=True)
    shutil.copy2(fake_ffmpeg, bundled)
    (contents / "Frameworks").mkdir()
    executable = contents / "MacOS" / "WebM2GIF"
    executable.parent.mkdir()
    executable.write_bytes(b"")

    monkeypatch.setattr("webm2gif.ffmpeg.resource_root", lambda: contents / "Frameworks")
    monkeypatch.setattr("webm2gif.ffmpeg._imageio_ffmpeg_path", lambda: None)
    monkeypatch.setattr(sys, "executable", str(executable))
    monkeypatch.setattr(sys, "frozen", True, raising=False)

    found = find_ffmpeg()
    assert found is not None
    assert found.source == "内置"
    assert found.path == str(bundled)


def test_environment_override(monkeypatch, fake_ffmpeg):
    monkeypatch.setenv("WEBM2GIF_FFMPEG", str(fake_ffmpeg))
    found = find_ffmpeg()
    assert found is not None and found.source == "环境变量"


def test_candidates_are_reported_in_priority_order(monkeypatch, tmp_path, fake_ffmpeg):
    monkeypatch.delenv("WEBM2GIF_FFMPEG", raising=False)
    monkeypatch.setattr("webm2gif.ffmpeg.resource_root", lambda: tmp_path / "empty")
    monkeypatch.setattr("webm2gif.ffmpeg._imageio_ffmpeg_path", lambda: None)
    monkeypatch.setattr("webm2gif.ffmpeg.shutil.which", lambda _name: "/usr/local/bin/ffmpeg")
    monkeypatch.setattr("webm2gif.ffmpeg._SYSTEM_CANDIDATES", (("/usr/bin/ffmpeg", "系统"),))

    sources = [source for _, source in iter_candidates(preferred=str(fake_ffmpeg))]

    assert sources[0] == "自定义"
    # A binary shipped with the app must be considered before anything on PATH.
    assert sources.index("内置") < sources.index("系统 PATH") < sources.index("系统")


def test_version_is_parsed(fake_ffmpeg):
    binary = FFmpegBinary(path=str(fake_ffmpeg), source="测试")
    assert binary.version().startswith("ffmpeg version 9.9.9-fake")


def test_version_of_missing_binary_is_empty():
    assert FFmpegBinary(path="/nonexistent/ffmpeg", source="测试").version() == ""
