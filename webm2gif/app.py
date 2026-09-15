"""Application bootstrap: menu bar, delegate and the AppKit event loop."""

from __future__ import annotations

from typing import Iterable, Optional

import objc
from AppKit import (
    NSApplication,
    NSApplicationActivationPolicyRegular,
    NSEventModifierFlagCommand,
    NSEventModifierFlagOption,
    NSEventModifierFlagShift,
    NSImage,
    NSMenu,
    NSMenuItem,
)
from Foundation import NSObject
from PyObjCTools import AppHelper

from . import APP_DISPLAY_NAME, APP_NAME
from .ffmpeg import resource_root
from .ui.main_window import MainWindowController

ICON_NAME = "AppIcon.icns"


def make_menu_item(
    menu: NSMenu,
    title: str,
    action: Optional[bytes] = None,
    key_equivalent: str = "",
    target=None,
    modifiers: Optional[int] = None,
) -> NSMenuItem:
    item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, action, key_equivalent)
    if target is not None:
        item.setTarget_(target)
    if modifiers is not None:
        item.setKeyEquivalentModifierMask_(modifiers)
    menu.addItem_(item)
    return item


class AppDelegate(NSObject):
    """Creates the window, the menu bar and wires up file arguments."""

    def initWithInputs_(self, inputs: Iterable[str]):
        self = objc.super(AppDelegate, self).init()
        if self is None:
            return None
        self.inputs = [str(path) for path in inputs or []]
        self.controller = None
        return self

    # ------------------------------------------------------------ lifecycle
    def applicationDidFinishLaunching_(self, notification) -> None:
        self.controller = MainWindowController.alloc().init()
        self.buildMenu()
        self.applyBundleIcon()
        self.controller.show()
        if self.inputs:
            self.controller.add_paths(self.inputs)
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)

    def applicationShouldTerminateAfterLastWindowClosed_(self, application) -> bool:
        return True

    def application_openFiles_(self, application, filenames) -> None:
        if self.controller is not None:
            self.controller.add_paths(list(filenames))

    # ----------------------------------------------------------------- menu
    def buildMenu(self) -> None:
        application = NSApplication.sharedApplication()
        main_menu = NSMenu.alloc().init()

        app_item = NSMenuItem.alloc().init()
        main_menu.addItem_(app_item)
        app_menu = NSMenu.alloc().initWithTitle_(APP_NAME)
        make_menu_item(app_menu, f"关于 {APP_DISPLAY_NAME}", b"orderFrontStandardAboutPanel:", "")
        app_menu.addItem_(NSMenuItem.separatorItem())
        make_menu_item(app_menu, f"隐藏 {APP_NAME}", b"hide:", "h")
        hide_others = make_menu_item(app_menu, "隐藏其他", b"hideOtherApplications:", "h")
        hide_others.setKeyEquivalentModifierMask_(NSEventModifierFlagCommand | NSEventModifierFlagOption)
        app_menu.addItem_(NSMenuItem.separatorItem())
        make_menu_item(app_menu, f"退出 {APP_NAME}", b"terminate:", "q")
        app_item.setSubmenu_(app_menu)

        file_item = NSMenuItem.alloc().init()
        main_menu.addItem_(file_item)
        file_menu = NSMenu.alloc().initWithTitle_("文件")
        if self.controller is not None:
            make_menu_item(file_menu, "添加文件…", b"onAddFiles:", "o", target=self.controller)
            folder_item = make_menu_item(file_menu, "添加文件夹…", b"onAddFolder:", "o", target=self.controller)
            folder_item.setKeyEquivalentModifierMask_(NSEventModifierFlagCommand | NSEventModifierFlagShift)
            file_menu.addItem_(NSMenuItem.separatorItem())
            make_menu_item(file_menu, "清空列表", b"onClearList:", "", target=self.controller)
            make_menu_item(file_menu, "开始转换", b"onStart:", "r", target=self.controller)
        file_item.setSubmenu_(file_menu)

        application.setMainMenu_(main_menu)

    def applyBundleIcon(self) -> None:
        icon_path = resource_root() / "Resources" / ICON_NAME
        if not icon_path.exists():
            icon_path = resource_root() / ICON_NAME
        if icon_path.exists():
            image = NSImage.alloc().initWithContentsOfFile_(str(icon_path))
            if image is not None:
                NSApplication.sharedApplication().setApplicationIconImage_(image)


def run(inputs: Iterable[str] = ()) -> int:
    """Start the GUI application and block until the user quits."""
    application = NSApplication.sharedApplication()
    application.setActivationPolicy_(NSApplicationActivationPolicyRegular)
    delegate = AppDelegate.alloc().initWithInputs_(inputs)
    application.setDelegate_(delegate)
    AppHelper.runEventLoop()
    return 0
