"""The single window of WebM2GIF: pick files, choose a folder, convert."""

from __future__ import annotations

import os
import threading
from pathlib import Path

import objc
from AppKit import (
    NSAlert,
    NSAlertStyleWarning,
    NSBackingStoreBuffered,
    NSBezelBorder,
    NSClosableWindowMask,
    NSColor,
    NSControlStateValueOff,
    NSControlStateValueOn,
    NSIndexSet,
    NSMakeRect,
    NSMiniaturizableWindowMask,
    NSModalResponseOK,
    NSRange,
    NSOpenPanel,
    NSSegmentSwitchTrackingSelectOne,
    NSSegmentedControl,
    NSTableCellView,
    NSTableColumn,
    NSTextAlignmentRight,
    NSTitledWindowMask,
    NSURL,
    NSView,
    NSWorkspace,
    NSWindow,
)
from Foundation import NSObject

from .. import APP_DISPLAY_NAME
from ..converter import (
    CANCELLED,
    DONE,
    FAILED,
    PENDING,
    RUNNING,
    ConversionItem,
    auto_workers,
    assign_outputs,
    convert_batch,
    summarize,
)
from ..discovery import expand_inputs
from ..ffmpeg import FFmpegBinary, MediaInfo, find_ffmpeg, probe_media
from ..hardware import (
    HARDWARE_AUTO,
    HARDWARE_OFF,
    HARDWARE_VIDEOTOOLBOX,
    detect_capabilities,
    prefer_hardware_ffmpeg,
    run_self_test,
)
from ..options import FPS_CHOICES, PRESETS, WIDTH_CHOICES, GifOptions, preset_for
from ..settings import load_settings, save_settings
from .snapshot import snapshot_view
from .table import FileDropTableView
from .widgets import (
    Stack,
    centered,
    make_button,
    make_checkbox,
    make_label,
    make_popup,
    make_progress,
    split_row,
    wrap_in_scroll,
)

WINDOW_WIDTH = 780
WINDOW_HEIGHT = 618
#: Widths of the two popups in the acceleration row (labels must fit inside).
ACCELERATION_POPUP_WIDTH = 170
WORKER_POPUP_WIDTH = 110
#: Space left for the "VideoToolbox：…" hint at the end of that row.
HARDWARE_LABEL_WIDTH = 330

COLUMNS = (
    ("name", "文件名", 380.0),
    ("resolution", "分辨率", 120.0),
    ("status", "状态", 226.0),
)

FPS_MENU: tuple[tuple[str, int | None], ...] = (("跟随预设", None),) + tuple(
    (f"{value} fps", value) for value in FPS_CHOICES
)

#: ``(label, hardware mode, GPU scaling)`` — one popup covers both switches.
#: Labels must stay narrower than :data:`ACCELERATION_POPUP_WIDTH`; the last
#: entry is the widest one that still fits without being clipped.
ACCELERATION_MENU: tuple[tuple[str, str, bool], ...] = (
    ("自动（实测择优）", HARDWARE_AUTO, False),
    ("关闭（纯 CPU）", HARDWARE_OFF, False),
    ("VideoToolbox 解码", HARDWARE_VIDEOTOOLBOX, False),
    ("VideoToolbox + 缩放", HARDWARE_VIDEOTOOLBOX, True),
)

#: ``(label, worker count)``; ``0`` means "decide from the CPU core count".
WORKER_MENU: tuple[tuple[str, int], ...] = (
    ("并行：自动", 0),
    ("并行：1 个", 1),
    ("并行：2 个", 2),
    ("并行：3 个", 3),
    ("并行：4 个", 4),
    ("并行：6 个", 6),
    ("并行：8 个", 8),
)


def hardware_status(capabilities, selftest, gpu_scale: bool) -> tuple[str, bool]:
    """``(text, healthy)`` for the acceleration line under the controls.

    The text is prefixed with "VideoToolbox：" on purpose, so it must stay short
    enough to fit next to the popups; details live in the label's tooltip. Kept
    free of AppKit types so it can be tested without a window.
    """
    if capabilities is None:
        return "正在检测硬件加速…", True
    if not capabilities.videotoolbox_available:
        return "VideoToolbox：当前 ffmpeg 未启用硬件加速", False
    result = selftest
    tested = result is not None and result.tested
    if tested and not result.decode_ok:
        return "VideoToolbox：硬件解码不可用（悬停查看原因）", False
    if tested and result.speedup > 0 and not result.worth_using:
        # Honest headline: the media engine works, it is just not faster here.
        return f"VideoToolbox：实测慢于软件 {result.speedup:.2f}×，自动模式用 CPU", False

    if tested and result.speedup > 0:
        text = f"VideoToolbox：硬件解码（实测 {result.speedup:.2f}×）"
    elif tested:
        text = "VideoToolbox：硬件解码（未实测）"
    else:
        text = "VideoToolbox：硬件解码"
    if gpu_scale:
        text += " · GPU 缩放" if (not tested or result.scale_ok) else " · GPU 缩放→CPU"
    return text, True


class MainWindowController(NSObject):
    """Owns the window, the file queue and the background conversion thread."""

    def init(self):
        self = objc.super(MainWindowController, self).init()
        if self is None:
            return None

        self.settings = load_settings()
        self.items: list[ConversionItem] = []
        self.cancel_event: threading.Event | None = None
        self.worker: threading.Thread | None = None
        self.probing = False
        self.last_output_dir = ""
        self.ffmpeg = find_ffmpeg(preferred=self.settings.get("ffmpeg_path") or None)
        self.hardware_thread: threading.Thread | None = None
        self.capabilities = None
        self.selftest = None

        self.build_window()
        self.refresh_ffmpeg_status()
        self.refresh_controls()
        self.refresh_status()
        self.start_hardware_detection()
        return self

    # ------------------------------------------------------------------ UI
    def build_window(self) -> None:
        style = NSTitledWindowMask | NSClosableWindowMask | NSMiniaturizableWindowMask
        window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0.0, 0.0, WINDOW_WIDTH, WINDOW_HEIGHT), style, NSBackingStoreBuffered, False
        )
        window.setTitle_(APP_DISPLAY_NAME)
        window.setReleasedWhenClosed_(False)
        window.setDelegate_(self)

        content = NSView.alloc().initWithFrame_(NSMakeRect(0.0, 0.0, WINDOW_WIDTH, WINDOW_HEIGHT))
        window.setContentView_(content)
        stack = Stack(WINDOW_WIDTH, WINDOW_HEIGHT)

        hint = make_label("拖入 .webm 文件，或用下面的按钮添加文件 / 文件夹", 12, color=NSColor.secondaryLabelColor())
        hint.setFrame_(stack.row(18, gap=8))
        content.addSubview_(hint)

        table = FileDropTableView.alloc().initWithFrame_(NSMakeRect(0, 0, stack.content_width - 2, 240))
        table.setRowHeight_(22)
        table.setUsesAlternatingRowBackgroundColors_(True)
        table.setAllowsMultipleSelection_(True)
        table.setColumnAutoresizingStyle_(4)  # last column only
        table.setDataSource_(self)
        table.setDelegate_(self)
        for identifier, title, width in COLUMNS:
            column = NSTableColumn.alloc().initWithIdentifier_(identifier)
            column.setTitle_(title)
            column.setWidth_(width)
            column.setMinWidth_(80.0)
            table.addTableColumn_(column)
        table.drop_handler = self.handle_dropped_paths
        self.table = table

        scroll = wrap_in_scroll(table, stack.row(240, gap=10))
        scroll.setBorderType_(NSBezelBorder)
        content.addSubview_(scroll)
        self.scroll = scroll

        button_row = stack.row(28, gap=18)
        add_files, add_folder, remove_selected, clear_list, _, count = split_row(
            button_row, [110, 130, 100, 100, None, 110]
        )
        self.add_files_button = make_button("添加文件…", self, b"onAddFiles:")
        self.add_folder_button = make_button("添加文件夹…", self, b"onAddFolder:")
        self.remove_button = make_button("移除选中", self, b"onRemoveSelected:")
        self.clear_button = make_button("清空列表", self, b"onClearList:")
        for control, rect in (
            (self.add_files_button, add_files),
            (self.add_folder_button, add_folder),
            (self.remove_button, remove_selected),
            (self.clear_button, clear_list),
        ):
            control.setFrame_(rect)
            content.addSubview_(control)

        self.count_label = make_label("共 0 个文件", 12, color=NSColor.secondaryLabelColor(), align=NSTextAlignmentRight)
        self.count_label.setFrame_(count)
        content.addSubview_(self.count_label)

        output_row = stack.row(28, gap=16)
        output_label, mode_rect, path_rect, choose_rect = split_row(output_row, [70, 250, None, 96])
        label = make_label("输出目录", 13)
        label.setFrame_(centered(output_label, 18))
        content.addSubview_(label)

        self.output_mode = NSSegmentedControl.segmentedControlWithLabels_trackingMode_target_action_(
            ["与源文件相同目录", "自定义目录"],
            NSSegmentSwitchTrackingSelectOne,
            self,
            b"onOutputModeChanged:",
        )
        self.output_mode.setFrame_(centered(mode_rect, 24))
        self.output_mode.setSelectedSegment_(1 if self.settings.get("output_mode") == "custom" else 0)
        content.addSubview_(self.output_mode)

        self.output_path_label = make_label("", 12, color=NSColor.secondaryLabelColor(), truncate=True)
        self.output_path_label.setFrame_(centered(path_rect, 18))
        content.addSubview_(self.output_path_label)

        self.choose_output_button = make_button("选择…", self, b"onChooseOutput:")
        self.choose_output_button.setFrame_(centered(choose_rect, 26))
        content.addSubview_(self.choose_output_button)

        params_row = stack.row(28, gap=16)
        fps_label, fps_rect, width_label, width_rect, preset_label, preset_rect, loop_rect = split_row(
            params_row, [40, 96, 40, 110, 44, 210, None]
        )
        for text, rect in (("帧率", fps_label), ("宽度", width_label), ("画质", preset_label)):
            field = make_label(text, 13)
            field.setFrame_(centered(rect, 18))
            content.addSubview_(field)

        self.fps_popup = make_popup(
            [title for title, _ in FPS_MENU], self, b"onOptionChanged:", width=fps_rect.size.width
        )
        self.fps_popup.setFrame_(centered(fps_rect, 26))
        content.addSubview_(self.fps_popup)

        self.width_popup = make_popup(
            [title for title, _ in WIDTH_CHOICES], self, b"onOptionChanged:", width=width_rect.size.width
        )
        self.width_popup.setFrame_(centered(width_rect, 26))
        content.addSubview_(self.width_popup)

        self.preset_popup = make_popup(
            [preset.label for preset in PRESETS], self, b"onOptionChanged:", width=preset_rect.size.width
        )
        self.preset_popup.setFrame_(centered(preset_rect, 26))
        content.addSubview_(self.preset_popup)

        self.loop_checkbox = make_checkbox("循环播放", self, b"onOptionChanged:")
        self.loop_checkbox.setFrame_(centered(loop_rect, 22))
        content.addSubview_(self.loop_checkbox)

        accel_row = stack.row(28, gap=16)
        accel_label, accel_rect, worker_label, worker_rect, hint_rect = split_row(
            accel_row, [40, ACCELERATION_POPUP_WIDTH, 40, WORKER_POPUP_WIDTH, None]
        )
        for title, rect in (("加速", accel_label), ("", worker_label)):
            field = make_label(title, 13)
            field.setFrame_(centered(rect, 18))
            content.addSubview_(field)

        self.accel_popup = make_popup(
            [title for title, _, _ in ACCELERATION_MENU], self, b"onOptionChanged:", width=accel_rect.size.width
        )
        self.accel_popup.setFrame_(centered(accel_rect, 26))
        self.accel_popup.setToolTip_(
            "自动：先用一段微素材实测硬件解码与软件解码的速度，只有不慢于软件时才启用"
            "（实测 M 系列芯片上硬件解码常常更慢，因为 GIF 编码本身必须在 CPU 上跑）。\n"
            "强制 VideoToolbox：不管实测结果都使用媒体引擎解码，失败会自动回退 CPU。\n"
            "“+ 缩放”再用 scale_vt 让 GPU 负责缩放（需要自检通过）。\n"
            "GIF 编码只能由 CPU 完成；神经网络引擎（NPU）无法被 ffmpeg 调用。"
        )
        content.addSubview_(self.accel_popup)

        self.workers_popup = make_popup(
            [title for title, _ in WORKER_MENU], self, b"onOptionChanged:", width=worker_rect.size.width
        )
        self.workers_popup.setFrame_(centered(worker_rect, 26))
        self.workers_popup.setToolTip_("同时转换的文件数量；批量转换时按 CPU 核心数自动选择。")
        content.addSubview_(self.workers_popup)

        self.hardware_label = make_label("", 12, color=NSColor.secondaryLabelColor(), truncate=True)
        self.hardware_label.setFrame_(centered(hint_rect, 18))
        content.addSubview_(self.hardware_label)

        progress_row = stack.row(16, gap=8)
        bar_rect, count_rect = split_row(progress_row, [None, 90])
        self.progress = make_progress()
        self.progress.setFrame_(centered(bar_rect, 14))
        content.addSubview_(self.progress)

        self.progress_label = make_label("0 / 0", 12, color=NSColor.secondaryLabelColor(), align=NSTextAlignmentRight)
        self.progress_label.setFrame_(centered(count_rect, 16))
        content.addSubview_(self.progress_label)

        status_row = stack.row(18, gap=14)
        self.status_label = make_label("准备就绪", 12, color=NSColor.secondaryLabelColor(), truncate=True)
        self.status_label.setFrame_(status_row)
        content.addSubview_(self.status_label)

        ffmpeg_row = stack.row(24, gap=12)
        ffmpeg_label_rect, ffmpeg_button_rect = split_row(ffmpeg_row, [None, 130])
        self.ffmpeg_label = make_label("", 12, color=NSColor.secondaryLabelColor(), truncate=True)
        self.ffmpeg_label.setFrame_(centered(ffmpeg_label_rect, 18))
        content.addSubview_(self.ffmpeg_label)

        self.ffmpeg_button = make_button("设置 ffmpeg…", self, b"onChooseFFmpeg:")
        self.ffmpeg_button.setFrame_(centered(ffmpeg_button_rect, 26))
        content.addSubview_(self.ffmpeg_button)

        action_row = stack.row(32, gap=0)
        reveal_rect, _, cancel_rect, start_rect = split_row(action_row, [140, None, 90, 120])
        self.reveal_button = make_button("打开输出目录", self, b"onRevealOutput:")
        self.reveal_button.setFrame_(reveal_rect)
        self.cancel_button = make_button("取消", self, b"onCancel:", key_equivalent="\x1b")
        self.cancel_button.setFrame_(cancel_rect)
        self.start_button = make_button("开始转换", self, b"onStart:", key_equivalent="\r")
        self.start_button.setFrame_(start_rect)
        for control in (self.reveal_button, self.cancel_button, self.start_button):
            content.addSubview_(control)

        self.window = window
        self.content = content
        self.sync_controls_from_settings()
        window.center()

    def show(self) -> None:
        self.window.makeKeyAndOrderFront_(None)

    def write_snapshot(self, destination) -> Path:
        """Render the window content to a PNG (used by tests and previews)."""
        return snapshot_view(self.content, destination)

    # ------------------------------------------------------ settings <-> UI
    def sync_controls_from_settings(self) -> None:
        preset_index = next(
            (index for index, preset in enumerate(PRESETS) if preset.key == self.settings.get("preset_key")),
            len(PRESETS) - 1,
        )
        self.preset_popup.selectItemAtIndex_(preset_index)

        fps_value = self.settings.get("fps")
        fps_index = next((i for i, (_, value) in enumerate(FPS_MENU) if value == fps_value), 0)
        self.fps_popup.selectItemAtIndex_(fps_index)

        width_value = self.settings.get("width")
        width_index = next((i for i, (_, value) in enumerate(WIDTH_CHOICES) if value == width_value), 0)
        self.width_popup.selectItemAtIndex_(width_index)

        loop_on = self.settings.get("loop", True)
        self.loop_checkbox.setState_(NSControlStateValueOn if loop_on else NSControlStateValueOff)

        self.accel_popup.selectItemAtIndex_(self.acceleration_menu_index())
        workers = int(self.settings.get("workers") or 0)
        worker_index = next((i for i, (_, value) in enumerate(WORKER_MENU) if value == workers), 0)
        self.workers_popup.selectItemAtIndex_(worker_index)

    def acceleration_choice(self) -> tuple[str, bool]:
        index = max(0, min(self.accel_popup.indexOfSelectedItem(), len(ACCELERATION_MENU) - 1))
        _, mode, gpu_scale = ACCELERATION_MENU[index]
        return mode, gpu_scale

    def acceleration_menu_index(self) -> int:
        mode = self.settings.get("hardware", HARDWARE_AUTO)
        gpu_scale = bool(self.settings.get("gpu_scale", False))
        for index, (_, candidate_mode, candidate_scale) in enumerate(ACCELERATION_MENU):
            if candidate_mode == mode and candidate_scale == gpu_scale:
                return index
        return 0

    def current_workers(self) -> int:
        configured = WORKER_MENU[self.workers_popup.indexOfSelectedItem()][1]
        if configured <= 0:
            return auto_workers(len(self.items))
        return configured

    def current_options(self) -> GifOptions:
        preset_key = PRESETS[self.preset_popup.indexOfSelectedItem()].key
        fps = FPS_MENU[self.fps_popup.indexOfSelectedItem()][1]
        width = WIDTH_CHOICES[self.width_popup.indexOfSelectedItem()][1]
        loop = self.loop_checkbox.state() == NSControlStateValueOn
        hardware, gpu_scale = self.acceleration_choice()
        return GifOptions(
            preset_key=preset_key,
            fps=fps,
            width=width,
            loop=loop,
            hardware=hardware,
            gpu_scale=gpu_scale,
        )

    def persist_settings(self) -> None:
        options = self.current_options()
        self.settings.update(
            {
                "preset_key": options.preset_key,
                "fps": options.fps,
                "width": options.width,
                "loop": options.loop,
                "hardware": options.hardware,
                "gpu_scale": options.gpu_scale,
                "workers": WORKER_MENU[self.workers_popup.indexOfSelectedItem()][1],
                "output_mode": "custom" if self.output_mode.selectedSegment() == 1 else "source",
                "output_dir": self.custom_output_dir(),
                "ffmpeg_path": self.ffmpeg.path if self.ffmpeg else "",
            }
        )
        save_settings(self.settings)

    def custom_output_dir(self) -> str:
        return str(self.settings.get("output_dir") or "")

    def output_dir(self) -> str | None:
        if self.output_mode.selectedSegment() == 1 and self.custom_output_dir():
            return self.custom_output_dir()
        return None

    # ------------------------------------------------------------ refresh
    def refresh_controls(self) -> None:
        running = self.worker is not None and self.worker.is_alive()
        selection = len(self.table.selectedRowIndexes()) if hasattr(self, "table") else 0

        self.add_files_button.setEnabled_(not running)
        self.add_folder_button.setEnabled_(not running)
        self.remove_button.setEnabled_(bool(selection) and not running)
        self.clear_button.setEnabled_(bool(self.items) and not running)
        self.start_button.setEnabled_(bool(self.items) and not running)
        self.cancel_button.setEnabled_(running)
        self.reveal_button.setEnabled_(bool(self.last_output_dir) and not running)
        self.output_mode.setEnabled_(not running)
        self.choose_output_button.setEnabled_(not running and self.output_mode.selectedSegment() == 1)
        self.preset_popup.setEnabled_(not running)
        self.fps_popup.setEnabled_(not running)
        self.width_popup.setEnabled_(not running)
        self.loop_checkbox.setEnabled_(not running)
        self.accel_popup.setEnabled_(not running)
        self.workers_popup.setEnabled_(not running)

        self.count_label.setStringValue_(f"共 {len(self.items)} 个文件")

        if self.output_mode.selectedSegment() == 1:
            if self.custom_output_dir():
                self.output_path_label.setStringValue_(self.custom_output_dir())
            else:
                self.output_path_label.setStringValue_("尚未选择输出目录，请点击右侧“选择…”")
        else:
            self.output_path_label.setStringValue_("（与源文件相同目录）")

        options = self.current_options()
        preset = preset_for(options.preset_key)
        self.preset_popup.setToolTip_(preset.summary)

        self.refresh_hardware_label()

        if not running and self.worker is None and options.hardware != HARDWARE_OFF:
            auto = auto_workers(len(self.items)) if self.items else auto_workers()
            self.workers_popup.setToolTip_(
                f"同时转换的文件数量；自动 = {auto} 个（本机 {os.cpu_count() or '?'} 个核心）"
            )

    def refresh_ffmpeg_status(self) -> None:
        if self.ffmpeg is None:
            self.ffmpeg_label.setStringValue_(
                "未找到 ffmpeg：请运行 scripts/setup.sh 安装，或点击右侧按钮手动指定"
            )
            self.ffmpeg_label.setTextColor_(NSColor.systemRedColor())
            self.ffmpeg_button.setTitle_("选择 ffmpeg…")
        else:
            version = getattr(self, "_ffmpeg_version", "")
            if not version:
                version = self.ffmpeg.version()
                self.ffmpeg_version_text = version
            detail = version.split(" Copyright")[0] if version else self.ffmpeg.path
            self.ffmpeg_label.setStringValue_(f"ffmpeg：{self.ffmpeg.source} · {detail}")
            self.ffmpeg_label.setTextColor_(NSColor.secondaryLabelColor())
            self.ffmpeg_button.setTitle_("设置 ffmpeg…")

    def refresh_hardware_label(self) -> None:
        """Short status of the media engine for the current configuration."""
        mode, gpu_scale = self.acceleration_choice()
        if mode == HARDWARE_OFF:
            self.hardware_label.setStringValue_("已关闭加速（纯 CPU 转换）")
            self.hardware_label.setTextColor_(NSColor.secondaryLabelColor())
            self.hardware_label.setToolTip_("完全使用 CPU 解码与缩放，便于对比速度或排查问题。")
            return

        text, healthy = hardware_status(self.capabilities, self.selftest, gpu_scale)
        self.hardware_label.setStringValue_(text)
        self.hardware_label.setTextColor_(
            NSColor.secondaryLabelColor() if healthy else NSColor.systemOrangeColor()
        )
        detail = self.selftest.detail if self.selftest is not None else ""
        self.hardware_label.setToolTip_(detail or "GIF 编码只能由 CPU 完成；NPU 无法被 ffmpeg 调用。")

    def refresh_status(self) -> None:
        if self.worker is not None and self.worker.is_alive():
            return
        if not self.items:
            self.status_label.setStringValue_("准备就绪 · " + self.current_options().describe())
            return
        summary = summarize(self.items)
        if summary.done or summary.failed or summary.cancelled:
            parts = [f"完成 {summary.done} 个"]
            if summary.failed:
                parts.append(f"失败 {summary.failed} 个")
            if summary.cancelled:
                parts.append(f"取消 {summary.cancelled} 个")
            self.status_label.setStringValue_("已结束：" + "，".join(parts))
        else:
            self.status_label.setStringValue_("准备就绪 · " + self.current_options().describe())

    def refresh_progress(self) -> None:
        total = len(self.items)
        finished = sum(1 for item in self.items if item.status in (DONE, FAILED, CANCELLED))
        # Several files can be converting at the same time.
        in_flight = sum(item.progress for item in self.items if item.status == RUNNING)
        overall = (finished + in_flight) / total if total else 0.0
        self.progress.setDoubleValue_(max(0.0, min(1.0, overall)))
        self.progress_label.setStringValue_(f"{finished} / {total}")

    def status_color(self, item: ConversionItem):
        if item.status == DONE:
            return NSColor.systemGreenColor()
        if item.status == FAILED:
            return NSColor.systemRedColor()
        if item.status == RUNNING:
            return NSColor.controlAccentColor()
        return NSColor.secondaryLabelColor()

    def reload_row(self, index: int) -> None:
        """Repaint one row (status and, after probing, the resolution column)."""
        if index < 0 or index >= len(self.items):
            return
        rows = NSIndexSet.indexSetWithIndex_(index)
        columns = NSIndexSet.indexSetWithIndexesInRange_(NSRange(0, self.table.numberOfColumns()))
        self.table.reloadDataForRowIndexes_columnIndexes_(rows, columns)

    def index_of(self, item: ConversionItem) -> int:
        for index, candidate in enumerate(self.items):
            if candidate is item:
                return index
        return -1

    # ------------------------------------------------------------- actions
    def onAddFiles_(self, sender) -> None:
        panel = NSOpenPanel.openPanel()
        panel.setTitle_("选择 WebM 文件")
        panel.setAllowsMultipleSelection_(True)
        panel.setCanChooseFiles_(True)
        panel.setCanChooseDirectories_(False)
        _restrict_panel_to_webm(panel)
        if panel.runModal() != NSModalResponseOK:
            return
        paths = [url.path() for url in panel.URLs()]
        if paths:
            self.settings["last_input_dir"] = str(Path(paths[0]).parent)
        self.add_paths(paths)

    def onAddFolder_(self, sender) -> None:
        panel = NSOpenPanel.openPanel()
        panel.setTitle_("选择包含 WebM 的文件夹")
        panel.setAllowsMultipleSelection_(False)
        panel.setCanChooseFiles_(False)
        panel.setCanChooseDirectories_(True)
        panel.setCanCreateDirectories_(False)
        if panel.runModal() != NSModalResponseOK:
            return
        folder = panel.URLs()[0].path()
        self.settings["last_input_dir"] = folder
        self.add_paths([folder])

    def onRemoveSelected_(self, sender) -> None:
        indexes = sorted(self.table.selectedRowIndexes(), reverse=True)
        for index in indexes:
            if 0 <= index < len(self.items):
                del self.items[index]
        self.table.reloadData()
        self.refresh_controls()
        self.refresh_progress()
        self.refresh_status()

    def onClearList_(self, sender) -> None:
        self.items = []
        self.table.reloadData()
        self.progress.setDoubleValue_(0.0)
        self.progress_label.setStringValue_("0 / 0")
        self.refresh_controls()
        self.refresh_status()

    def onOutputModeChanged_(self, sender) -> None:
        self.persist_settings()
        self.refresh_controls()

    def onChooseOutput_(self, sender) -> None:
        panel = NSOpenPanel.openPanel()
        panel.setTitle_("选择 GIF 输出目录")
        panel.setCanChooseDirectories_(True)
        panel.setCanChooseFiles_(False)
        panel.setAllowsMultipleSelection_(False)
        panel.setCanCreateDirectories_(True)
        if panel.runModal() != NSModalResponseOK:
            return
        self.settings["output_dir"] = panel.URLs()[0].path()
        self.output_mode.setSelectedSegment_(1)
        self.persist_settings()
        self.refresh_controls()

    def onOptionChanged_(self, sender) -> None:
        self.persist_settings()
        self.refresh_controls()
        self.refresh_status()

    def onChooseFFmpeg_(self, sender) -> None:
        panel = NSOpenPanel.openPanel()
        panel.setTitle_("选择 ffmpeg 可执行文件")
        panel.setCanChooseFiles_(True)
        panel.setCanChooseDirectories_(False)
        panel.setAllowsMultipleSelection_(False)
        panel.setShowsHiddenFiles_(True)
        if panel.runModal() != NSModalResponseOK:
            return
        candidate = panel.URLs()[0].path()
        binary = FFmpegBinary(path=candidate, source="自定义")
        if not binary.version():
            self.alert_panel("无法运行 ffmpeg", "选中的文件不是可执行的 ffmpeg，请重新选择。")
            return
        self.ffmpeg = binary
        self.settings["ffmpeg_path"] = candidate
        self.ffmpeg_version_text = ""
        self.persist_settings()
        self.refresh_ffmpeg_status()
        self.start_probing()

    def onRevealOutput_(self, sender) -> None:
        if self.last_output_dir and Path(self.last_output_dir).is_dir():
            NSWorkspace.sharedWorkspace().openURL_(NSURL.fileURLWithPath_(self.last_output_dir))

    def onStart_(self, sender) -> None:
        if self.worker is not None and self.worker.is_alive():
            return
        ffmpeg = self.ffmpeg or find_ffmpeg(preferred=self.settings.get("ffmpeg_path") or None)
        if ffmpeg is None:
            self.alert_panel(
                "缺少 ffmpeg",
                "转换需要 ffmpeg。\n\n请先在项目目录运行 scripts/setup.sh（会自动安装到本地虚拟环境），"
                "或点击“设置 ffmpeg…”手动指定一个 ffmpeg 可执行文件。",
            )
            return
        self.ffmpeg = ffmpeg
        if not self.items:
            return

        output_dir = self.output_dir()
        if output_dir and not Path(output_dir).is_dir():
            self.alert_panel("输出目录不存在", f"{output_dir}\n\n请重新选择输出目录。")
            return

        options = self.current_options()
        assign_outputs(self.items, output_dir)
        for item in self.items:
            item.status = PENDING
            item.progress = 0.0
            item.message = ""
        self.last_output_dir = output_dir or str(self.items[0].output.parent)

        self.cancel_event = threading.Event()
        self.table.reloadData()
        self.progress.setDoubleValue_(0.0)
        self.refresh_progress()
        self.status_label.setStringValue_("开始转换…")

        batch = list(self.items)
        worker = threading.Thread(
            target=self.run_batch,
            args=(ffmpeg.path, batch, options, self.cancel_event, self.current_workers()),
            daemon=True,
        )
        self.worker = worker
        worker.start()
        self.refresh_controls()

    def onCancel_(self, sender) -> None:
        if self.cancel_event is not None:
            self.cancel_event.set()
            self.status_label.setStringValue_("正在取消…")

    # ------------------------------------------------------------- worker
    def run_batch(
        self,
        ffmpeg_path: str,
        batch: list[ConversionItem],
        options: GifOptions,
        cancel_event,
        max_workers: int,
    ) -> None:
        from PyObjCTools import AppHelper

        def on_update(item: ConversionItem) -> None:
            AppHelper.callAfter(self.apply_item_update, item)

        capabilities = self.capabilities
        selftest = self.selftest
        if options.hardware != HARDWARE_OFF and capabilities is None:
            # Fall back to detecting here: this is already a worker thread.
            capabilities = detect_capabilities(ffmpeg_path)
            selftest = run_self_test(ffmpeg_path, capabilities)

        convert_batch(
            ffmpeg_path,
            batch,
            options,
            on_item_update=on_update,
            cancel_event=cancel_event,
            max_workers=max_workers,
            capabilities=capabilities,
            selftest=selftest,
        )
        AppHelper.callAfter(self.batch_finished)

    def apply_item_update(self, item: ConversionItem) -> None:
        index = self.index_of(item)
        self.reload_row(index)
        self.refresh_progress()
        if item.status == RUNNING:
            running = [i for i in self.items if i.status == RUNNING]
            finished = sum(1 for i in self.items if i.status in (DONE, FAILED, CANCELLED))
            self.progress_label.setStringValue_(f"{finished} / {len(self.items)}")
            if len(running) > 1:
                names = "、".join(i.source.name for i in running[:2])
                more = f" 等 {len(running)} 个" if len(running) > 2 else ""
                self.status_label.setStringValue_(f"正在并行转换：{names}{more}")
            else:
                self.status_label.setStringValue_(
                    f"正在转换：{item.source.name}（{int(item.progress * 100)}%）"
                )
        elif item.status == FAILED:
            self.status_label.setStringValue_(f"失败：{item.source.name} — {item.message}")

    def batch_finished(self) -> None:
        # Clear the worker first: the helper methods below treat a live worker
        # as "still converting" and would keep the controls disabled.
        self.worker = None
        self.cancel_event = None
        self.refresh_progress()
        self.refresh_controls()
        self.refresh_status()
        self.table.reloadData()

    # --------------------------------------------------------------- files
    def handle_dropped_paths(self, paths) -> None:
        self.add_paths(paths)

    def add_paths(self, paths) -> None:
        if self.worker is not None and self.worker.is_alive():
            return
        found = expand_inputs(paths, recursive=bool(self.settings.get("recursive_folders", True)))
        existing = {str(item.source) for item in self.items}
        added = [path for path in found if str(path) not in existing]
        if not added:
            self.status_label.setStringValue_("没有发现新的 .webm 文件")
            return
        for path in added:
            self.items.append(ConversionItem(source=path, output=path.with_suffix(".gif")))
        assign_outputs(self.items, self.output_dir())
        self.table.reloadData()
        self.refresh_controls()
        self.refresh_progress()
        self.status_label.setStringValue_(f"已添加 {len(added)} 个文件")
        self.start_probing()

    def start_probing(self) -> None:
        if self.probing or self.ffmpeg is None:
            return
        pending = [item for item in self.items if item.info is None]
        if not pending:
            return
        self.probing = True
        threading.Thread(target=self.probe_worker, daemon=True).start()

    def probe_worker(self) -> None:
        from PyObjCTools import AppHelper

        while True:
            targets = [item for item in list(self.items) if item.info is None]
            if not targets or self.ffmpeg is None:
                break
            item = targets[0]
            item.info = probe_media(self.ffmpeg.path, item.source) or MediaInfo()
            AppHelper.callAfter(self.apply_probe, item)
        self.probing = False

    def apply_probe(self, item: ConversionItem) -> None:
        index = self.index_of(item)
        if index >= 0:
            rows = NSIndexSet.indexSetWithIndex_(index)
            self.table.reloadDataForRowIndexes_columnIndexes_(rows, NSIndexSet.indexSetWithIndex_(1))

    # --------------------------------------------------------------- misc
    def start_hardware_detection(self) -> None:
        """Probe the media engine in the background and report back."""
        if self.hardware_thread is not None:
            return
        self.hardware_thread = threading.Thread(target=self.hardware_worker, daemon=True)
        self.hardware_thread.start()

    def hardware_worker(self) -> None:
        from PyObjCTools import AppHelper

        binary = self.ffmpeg
        if binary is None:
            AppHelper.callAfter(self.apply_hardware_report, None, None, None)
            return
        capabilities = detect_capabilities(binary.path)
        selftest = run_self_test(binary.path, capabilities) if capabilities.detected else None

        better = None
        mode, _ = ACCELERATION_MENU[self.acceleration_menu_index()][1:]
        if capabilities.detected and not capabilities.can_decode_webm_in_hardware and mode != HARDWARE_OFF:
            better = prefer_hardware_ffmpeg(binary, mode)
            if better is not None and better.path != binary.path:
                capabilities = detect_capabilities(better.path)
                selftest = run_self_test(better.path, capabilities)
        AppHelper.callAfter(self.apply_hardware_report, better, capabilities, selftest)

    def apply_hardware_report(self, binary, capabilities, selftest) -> None:
        self.hardware_thread = None
        if binary is not None and binary.path != (self.ffmpeg.path if self.ffmpeg else None):
            self.ffmpeg = binary
            self.settings["ffmpeg_path"] = binary.path
            self.ffmpeg_version_text = ""
            self.persist_settings()
            self.refresh_ffmpeg_status()
        self.capabilities = capabilities
        self.selftest = selftest
        self.refresh_hardware_label()

    def alert_panel(self, title: str, message: str) -> None:
        alert = NSAlert.alloc().init()
        alert.setMessageText_(title)
        alert.setInformativeText_(message)
        alert.setAlertStyle_(NSAlertStyleWarning)
        alert.addButtonWithTitle_("好")
        if self.window is not None:
            alert.beginSheetModalForWindow_completionHandler_(self.window, None)
        else:
            alert.runModal()

    def windowShouldClose_(self, sender) -> bool:
        self.persist_settings()
        return True

    # ------------------------------------------------------- data source
    def numberOfRowsInTableView_(self, table) -> int:
        return len(self.items)

    def tableView_viewForTableColumn_row_(self, table, column, row):
        if row >= len(self.items):
            return None
        item = self.items[row]
        identifier = column.identifier()
        view = table.makeViewWithIdentifier_owner_(identifier, self)
        if view is None:
            view = NSTableCellView.alloc().initWithFrame_(NSMakeRect(0.0, 0.0, column.width(), 22.0))
            view.setIdentifier_(identifier)
            field = make_label("", 12, truncate=True)
            field.setFrame_(NSMakeRect(4.0, 3.0, column.width() - 10.0, 16.0))
            view.addSubview_(field)
            view.setTextField_(field)

        field = view.textField()
        if identifier == "name":
            field.setStringValue_(item.source.name)
            field.setTextColor_(NSColor.labelColor())
            view.setToolTip_(str(item.source))
        elif identifier == "resolution":
            field.setStringValue_(item.resolution_text)
            field.setTextColor_(NSColor.secondaryLabelColor())
            view.setToolTip_(f"时长 {item.info.duration_text} · {item.info.codec}" if item.info else "")
        else:
            field.setStringValue_(item.status_label)
            field.setTextColor_(self.status_color(item))
            view.setToolTip_(item.message or f"{item.hardware_label}\n{item.output}")
        return view

    def tableViewSelectionDidChange_(self, notification) -> None:
        self.refresh_controls()


def _restrict_panel_to_webm(panel) -> None:
    try:
        from UniformTypeIdentifiers import UTType

        panel.setAllowedContentTypes_([UTType.typeWithFilenameExtension_("webm")])
    except Exception:
        panel.setAllowedFileTypes_(["webm"])
