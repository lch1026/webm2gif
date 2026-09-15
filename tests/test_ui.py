"""The AppKit window: layout, control states and rendering.

These tests build the real window (without showing it on screen) and render it
to a PNG, so the interface can be reviewed from a terminal session.
"""

from __future__ import annotations

import pytest

pytest.importorskip("AppKit")

from webm2gif.converter import ConversionItem  # noqa: E402
from webm2gif.settings import load_settings  # noqa: E402
from webm2gif.ui.main_window import MainWindowController  # noqa: E402


@pytest.fixture(scope="module")
def application():
    import AppKit

    app = AppKit.NSApplication.sharedApplication()
    app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)
    return app


@pytest.fixture
def controller(application):
    window = MainWindowController.alloc().init()
    # Probing would spawn a thread with run-loop callbacks; not wanted in tests.
    window.ffmpeg = None
    return window


def test_window_has_the_expected_chrome(controller):
    assert controller.window.title() == "WebM → GIF"
    assert controller.window.contentView().frame().size.width == 780


def test_table_columns(controller):
    table = controller.table
    assert table.numberOfColumns() == 3
    titles = [table.tableColumnWithIdentifier_(name).title() for name in ("name", "resolution", "status")]
    assert titles == ["文件名", "分辨率", "状态"]


def test_controls_start_in_a_safe_state(controller):
    assert not controller.start_button.isEnabled()
    assert not controller.cancel_button.isEnabled()
    assert not controller.remove_button.isEnabled()
    assert not controller.clear_button.isEnabled()
    assert controller.count_label.stringValue() == "共 0 个文件"
    assert controller.progress.doubleValue() == 0.0


def test_adding_files_enables_conversion(controller, tmp_path):
    files = []
    for name in ("a.webm", "b.webm"):
        path = tmp_path / name
        path.write_bytes(b"fake")
        files.append(str(path))

    controller.add_paths(files)

    assert len(controller.items) == 2
    assert controller.start_button.isEnabled()
    assert controller.clear_button.isEnabled()
    assert controller.count_label.stringValue() == "共 2 个文件"
    assert "已添加 2 个文件" in controller.status_label.stringValue()


def test_non_webm_and_duplicate_paths_are_ignored(controller, tmp_path):
    webm = tmp_path / "clip.webm"
    webm.write_bytes(b"fake")
    other = tmp_path / "clip.mp4"
    other.write_bytes(b"fake")

    controller.add_paths([str(webm), str(other), str(webm)])

    assert [item.source.name for item in controller.items] == ["clip.webm"]


def test_folder_inputs_are_expanded(controller, tmp_path):
    folder = tmp_path / "clips"
    (folder / "nested").mkdir(parents=True)
    (folder / "one.webm").write_bytes(b"1")
    (folder / "nested" / "two.webm").write_bytes(b"2")

    controller.add_paths([str(folder)])

    assert sorted(item.source.name for item in controller.items) == ["one.webm", "two.webm"]


def test_removing_and_clearing(controller, tmp_path):
    for name in ("a.webm", "b.webm", "c.webm"):
        (tmp_path / name).write_bytes(b"fake")
    controller.add_paths([str(tmp_path)])

    controller.table.selectRowIndexes_byExtendingSelection_(
        __import__("AppKit").NSIndexSet.indexSetWithIndex_(1), False
    )
    controller.onRemoveSelected_(None)
    assert sorted(item.source.name for item in controller.items) == ["a.webm", "c.webm"]

    controller.onClearList_(None)
    assert controller.items == []
    assert not controller.start_button.isEnabled()


def test_options_follow_the_popup_buttons(controller):
    controller.preset_popup.selectItemAtIndex_(2)  # 小体积
    options = controller.current_options()
    assert options.preset_key == "small"
    assert options.fps is None  # "跟随预设"
    assert options.target_width(4000) == 360

    controller.fps_popup.selectItemAtIndex_(7)  # 30 fps
    assert controller.current_options().effective_fps() == 30


def test_output_mode_toggles_the_choose_button(controller):
    controller.output_mode.setSelectedSegment_(0)
    controller.onOutputModeChanged_(None)
    assert not controller.choose_output_button.isEnabled()
    assert controller.output_path_label.stringValue() == "（与源文件相同目录）"

    controller.settings["output_dir"] = "/tmp"
    controller.output_mode.setSelectedSegment_(1)
    controller.onOutputModeChanged_(None)
    assert controller.choose_output_button.isEnabled()
    assert controller.output_path_label.stringValue() == "/tmp"


def test_settings_are_persisted(controller, isolated_config):
    controller.preset_popup.selectItemAtIndex_(0)
    controller.loop_checkbox.setState_(0)
    controller.onOptionChanged_(None)

    stored = load_settings()
    assert stored["preset_key"] == "high"
    assert stored["loop"] is False


def test_status_column_colours(controller, tmp_path):
    item = ConversionItem(source=tmp_path / "x.webm", output=tmp_path / "x.gif")
    from webm2gif.converter import DONE, FAILED

    item.status = DONE
    assert controller.status_color(item).isEqual_(__import__("AppKit").NSColor.systemGreenColor())
    item.status = FAILED
    assert controller.status_color(item).isEqual_(__import__("AppKit").NSColor.systemRedColor())


def test_dropped_files_use_the_same_path_as_the_buttons(controller, tmp_path):
    dropped = tmp_path / "dropped.webm"
    dropped.write_bytes(b"fake")

    controller.handle_dropped_paths([str(dropped)])

    assert [item.source.name for item in controller.items] == ["dropped.webm"]


def test_window_renders_to_png(controller, tmp_path):
    controller.add_paths([])
    destination = controller.write_snapshot(tmp_path / "ui.png")
    assert destination.exists()
    assert destination.stat().st_size > 5_000


def test_finishing_a_batch_restores_the_controls(controller, tmp_path):
    import threading

    (tmp_path / "clip.webm").write_bytes(b"fake")
    controller.add_paths([str(tmp_path)])

    release = threading.Event()
    controller.worker = threading.Thread(target=release.wait, args=(5,), daemon=True)
    controller.worker.start()
    controller.refresh_controls()
    assert not controller.start_button.isEnabled()
    assert not controller.add_files_button.isEnabled()

    controller.items[0].status = "done"
    controller.batch_finished()
    release.set()

    assert controller.worker is None
    assert controller.start_button.isEnabled()
    assert controller.add_files_button.isEnabled()
    assert "已结束" in controller.status_label.stringValue()


def test_window_drives_a_full_conversion(controller, fake_ffmpeg, sample_webm, tmp_path):
    """Start → worker thread → run-loop callbacks → finished state."""
    import time

    from Foundation import NSDate, NSRunLoop

    from webm2gif.ffmpeg import FFmpegBinary

    output_dir = tmp_path / "gifs"
    output_dir.mkdir()
    controller.ffmpeg = FFmpegBinary(path=str(fake_ffmpeg), source="测试")
    controller.add_paths([str(sample_webm)])
    controller.settings["output_dir"] = str(output_dir)
    controller.output_mode.setSelectedSegment_(1)
    controller.onOutputModeChanged_(None)

    controller.onStart_(None)
    assert controller.worker is not None
    assert not controller.start_button.isEnabled()

    # 轮询而不是死等：CI 的 macOS 运行器要跑完整套用例（大量子进程）时会明显变慢，
    # 所以期限给得宽松些，并在两次泵 run loop 之间让出 CPU，避免和工作线程抢 GIL。
    deadline = time.time() + 60
    while controller.worker is not None and time.time() < deadline:
        NSRunLoop.currentRunLoop().runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.02))
        time.sleep(0.01)

    assert controller.worker is None, "转换没有在预期时间内结束"
    assert controller.items[0].status == "done"
    assert (output_dir / "clip.gif").read_bytes() == b"GIF89a"
    assert controller.start_button.isEnabled()
    assert controller.progress.doubleValue() == 1.0
    assert "已结束" in controller.status_label.stringValue()


def test_conversion_is_refused_without_ffmpeg(controller, tmp_path, monkeypatch):
    monkeypatch.setattr("webm2gif.ui.main_window.find_ffmpeg", lambda **_kwargs: None)
    (tmp_path / "clip.webm").write_bytes(b"fake")
    controller.add_paths([str(tmp_path)])
    controller.ffmpeg = None

    shown = {}

    def fake_alert(title, message):
        shown["title"] = title

    controller.alert_panel = fake_alert
    controller.onStart_(None)

    assert controller.worker is None
    assert "ffmpeg" in shown.get("title", "").lower()


# ------------------------------------------------------- hardware controls
def test_acceleration_menu_maps_to_modes(controller):
    from webm2gif.hardware import HARDWARE_AUTO, HARDWARE_OFF, HARDWARE_VIDEOTOOLBOX
    from webm2gif.ui.main_window import ACCELERATION_MENU

    assert controller.acceleration_choice() == (HARDWARE_AUTO, False), "默认应当是自动加速"

    controller.accel_popup.selectItemAtIndex_(1)
    assert controller.acceleration_choice() == (HARDWARE_OFF, False)

    controller.accel_popup.selectItemAtIndex_(3)
    assert controller.acceleration_choice() == (HARDWARE_VIDEOTOOLBOX, True)
    options = controller.current_options()
    assert options.hardware == HARDWARE_VIDEOTOOLBOX and options.gpu_scale is True

    # Every entry must describe a valid combination.
    assert {mode for _, mode, _ in ACCELERATION_MENU} <= {"auto", "off", "videotoolbox"}


def test_acceleration_menu_restores_the_stored_choice(controller):
    controller.settings["hardware"] = "videotoolbox"
    controller.settings["gpu_scale"] = True
    assert controller.acceleration_menu_index() == 3
    controller.accel_popup.selectItemAtIndex_(controller.acceleration_menu_index())
    assert controller.current_options().gpu_scale is True


def test_acceleration_and_workers_are_persisted(controller, isolated_config):
    controller.accel_popup.selectItemAtIndex_(2)  # VideoToolbox 解码
    controller.workers_popup.selectItemAtIndex_(3)  # 并行：3 个
    controller.onOptionChanged_(None)

    stored = load_settings()
    assert stored["hardware"] == "videotoolbox"
    assert stored["gpu_scale"] is False
    assert stored["workers"] == 3
    assert controller.current_workers() == 3


def test_automatic_worker_count_follows_the_queue(controller, tmp_path):
    from webm2gif.converter import auto_workers

    controller.workers_popup.selectItemAtIndex_(0)  # 并行：自动
    assert controller.current_workers() == auto_workers()

    for name in ("a.webm", "b.webm"):
        (tmp_path / name).write_bytes(b"fake")
    controller.add_paths([str(tmp_path)])
    assert controller.current_workers() == auto_workers(2)


def test_hardware_report_updates_the_status_line(controller, fake_ffmpeg):
    import AppKit

    from webm2gif.hardware import HardwareCapabilities, SelfTestResult, detect_capabilities, run_self_test

    capabilities = detect_capabilities(str(fake_ffmpeg))
    controller.apply_hardware_report(None, capabilities, run_self_test(str(fake_ffmpeg), capabilities))
    assert "VideoToolbox" in controller.hardware_label.stringValue()

    slower = SelfTestResult(decode_ok=True, tested=True, hardware_seconds=0.4, software_seconds=0.2)
    controller.apply_hardware_report(None, capabilities, slower)
    assert "自动模式用 CPU" in controller.hardware_label.stringValue()
    assert controller.hardware_label.textColor().isEqual_(AppKit.NSColor.systemOrangeColor())

    controller.apply_hardware_report(None, HardwareCapabilities(hwaccels=(), decoders=(), detected=True), None)
    assert "未启用硬件加速" in controller.hardware_label.stringValue()

    controller.accel_popup.selectItemAtIndex_(1)  # 关闭
    controller.refresh_hardware_label()
    assert controller.hardware_label.stringValue() == "已关闭加速（纯 CPU 转换）"


def test_hardware_status_text_for_every_situation():
    from webm2gif.hardware import HardwareCapabilities, SelfTestResult
    from webm2gif.ui.main_window import hardware_status

    assert hardware_status(None, None, False)[0].startswith("正在检测")

    no_videotoolbox = HardwareCapabilities(decoders=("vp9",), detected=True)
    text, healthy = hardware_status(no_videotoolbox, None, False)
    assert "未启用硬件加速" in text and healthy is False

    hwaccel_only = HardwareCapabilities(hwaccels=("videotoolbox",), decoders=("vp9",), detected=True)
    text, healthy = hardware_status(hwaccel_only, None, False)
    assert text == "VideoToolbox：硬件解码" and healthy is True

    slower = SelfTestResult(decode_ok=True, tested=True, hardware_seconds=0.30, software_seconds=0.12)
    text, healthy = hardware_status(hwaccel_only, slower, False)
    assert text == "VideoToolbox：实测慢于软件 0.40×，自动模式用 CPU" and healthy is False

    fast = SelfTestResult(decode_ok=True, scale_ok=True, tested=True, hardware_seconds=0.1, software_seconds=0.3)
    text, healthy = hardware_status(hwaccel_only, fast, False)
    assert text == "VideoToolbox：硬件解码（实测 3.00×）" and healthy is True
    assert hardware_status(hwaccel_only, fast, True)[0].endswith(" · GPU 缩放")
    unprepared = SelfTestResult(decode_ok=True, tested=True)
    assert hardware_status(hwaccel_only, unprepared, True)[0].endswith(" · GPU 缩放→CPU")

    broken = SelfTestResult(tested=True, detail="初始化失败")
    text, healthy = hardware_status(hwaccel_only, broken, False)
    assert "硬件解码不可用" in text and healthy is False


def test_hardware_label_budget_matches_the_real_layout(controller):
    from webm2gif.ui.main_window import HARDWARE_LABEL_WIDTH

    live = controller.hardware_label.frame().size.width
    assert live >= HARDWARE_LABEL_WIDTH, f"布局只剩下 {live:.0f}px，需要同步 HARDWARE_LABEL_WIDTH"


def test_hardware_status_never_duplicates_or_overflows(controller):
    """The line must fit the space left by the two popups (it is truncated)."""
    import AppKit
    from Foundation import NSDictionary

    from webm2gif.hardware import HardwareCapabilities, SelfTestResult
    from webm2gif.ui.main_window import HARDWARE_LABEL_WIDTH, hardware_status

    capable = HardwareCapabilities(
        hwaccels=("videotoolbox",),
        decoders=("vp9_videotoolbox", "vp8_videotoolbox"),
        filters=("scale_vt",),
        detected=True,
    )
    situations = [
        (None, None, False),
        (HardwareCapabilities(decoders=("vp9",), detected=True), None, False),
        (HardwareCapabilities(hwaccels=("videotoolbox",), decoders=("vp9",), detected=True), None, False),
        (capable, SelfTestResult(decode_ok=True, scale_ok=True, tested=True), False),
        (capable, SelfTestResult(decode_ok=True, scale_ok=True, tested=True), True),
        (capable, SelfTestResult(decode_ok=True, tested=True), True),
        (capable, SelfTestResult(tested=True, detail="初始化失败"), False),
    ]
    attributes = NSDictionary.dictionaryWithObject_forKey_(AppKit.NSFont.systemFontOfSize_(12), "NSFont")

    budget = min(HARDWARE_LABEL_WIDTH, controller.hardware_label.frame().size.width)
    for capabilities, selftest, gpu_scale in situations:
        text, _ = hardware_status(capabilities, selftest, gpu_scale)
        assert text.count("VideoToolbox") == (0 if capabilities is None else 1), text
        width = AppKit.NSAttributedString.alloc().initWithString_attributes_(text, attributes).size().width
        assert width < budget, f"{text} 需要 {width:.0f}px，只有 {budget:.0f}px"


def test_popup_labels_fit_their_buttons():
    """A clipped label would hide the current setting from the user."""
    import AppKit
    from Foundation import NSDictionary

    from webm2gif.ui.main_window import (
        ACCELERATION_POPUP_WIDTH,
        ACCELERATION_MENU,
        WORKER_MENU,
        WORKER_POPUP_WIDTH,
    )

    attributes = NSDictionary.dictionaryWithObject_forKey_(AppKit.NSFont.systemFontOfSize_(13), "NSFont")

    def width_of(text):
        return AppKit.NSAttributedString.alloc().initWithString_attributes_(text, attributes).size().width

    # NSPopUpButton keeps room for the arrows and its own padding.
    for label, _, _ in ACCELERATION_MENU:
        assert width_of(label) < ACCELERATION_POPUP_WIDTH - 30, label
    for label, _ in WORKER_MENU:
        assert width_of(label) < WORKER_POPUP_WIDTH - 30, label
