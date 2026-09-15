"""Small helpers that keep the AppKit layout code readable."""

from __future__ import annotations

from typing import Iterable, Optional, Sequence

from AppKit import (
    NSBezelStyleRounded,
    NSButton,
    NSButtonTypeSwitch,
    NSControlSizeRegular,
    NSControlStateValueOff,
    NSControlStateValueOn,
    NSFont,
    NSLineBreakByTruncatingMiddle,
    NSLineBreakByTruncatingTail,
    NSPopUpButton,
    NSProgressIndicator,
    NSProgressIndicatorStyleBar,
    NSScrollView,
    NSTextField,
    NSViewWidthSizable,
)
from Foundation import NSMakeRect

MARGIN = 20
GAP = 8


class Stack:
    """Lay views out from the top of a fixed size content view."""

    def __init__(self, width: float, height: float, margin: float = MARGIN, top: float = MARGIN):
        self.width = width
        self.height = height
        self.margin = margin
        self.cursor = height - top

    @property
    def content_width(self) -> float:
        return self.width - 2 * self.margin

    def row(self, height: float, gap: float = 0, width: Optional[float] = None, x: Optional[float] = None):
        """Consume ``height`` (+``gap``) from the top and return its rect."""
        self.cursor -= height
        rect = NSMakeRect(
            self.margin if x is None else x,
            self.cursor,
            self.content_width if width is None else width,
            height,
        )
        self.cursor -= gap
        return rect

    def space(self, gap: float) -> None:
        self.cursor -= gap


def split_row(rect, widths: Sequence[Optional[float]], gap: float = GAP):
    """Slice ``rect`` left to right; ``None`` widths take the leftover space."""
    fixed = sum(width for width in widths if width is not None)
    flexible = sum(1 for width in widths if width is None)
    leftover = rect.size.width - fixed - gap * (len(widths) - 1)
    flexible_width = max(0.0, leftover / flexible) if flexible else 0.0

    rects = []
    x = rect.origin.x
    for width in widths:
        actual = flexible_width if width is None else width
        rects.append(NSMakeRect(x, rect.origin.y, actual, rect.size.height))
        x += actual + gap
    return rects


def centered(rect, height: float):
    """Vertically centre a control of ``height`` inside ``rect``."""
    offset = (rect.size.height - height) / 2.0
    return NSMakeRect(rect.origin.x, rect.origin.y + offset, rect.size.width, height)


def make_label(
    text: str,
    size: float = 13,
    bold: bool = False,
    color=None,
    align: Optional[int] = None,
    truncate: bool = False,
) -> NSTextField:
    field = NSTextField.alloc().initWithFrame_(NSMakeRect(0, 0, 10, 10))
    field.setStringValue_(text)
    field.setBezeled_(False)
    field.setDrawsBackground_(False)
    field.setEditable_(False)
    field.setSelectable_(False)
    field.setFont_(NSFont.boldSystemFontOfSize_(size) if bold else NSFont.systemFontOfSize_(size))
    if color is not None:
        field.setTextColor_(color)
    if align is not None:
        field.setAlignment_(align)
    field.setLineBreakMode_(NSLineBreakByTruncatingMiddle if truncate else NSLineBreakByTruncatingTail)
    field.setAutoresizingMask_(NSViewWidthSizable)
    return field


def make_button(
    title: str,
    target=None,
    action: Optional[bytes] = None,
    key_equivalent: str = "",
    enabled: bool = True,
) -> NSButton:
    button = NSButton.alloc().initWithFrame_(NSMakeRect(0, 0, 100, 28))
    button.setTitle_(title)
    button.setBezelStyle_(NSBezelStyleRounded)
    button.setFont_(NSFont.systemFontOfSize_(13))
    button.setControlSize_(NSControlSizeRegular)
    if target is not None and action is not None:
        button.setTarget_(target)
        button.setAction_(action)
    if key_equivalent:
        button.setKeyEquivalent_(key_equivalent)
    button.setEnabled_(enabled)
    return button


def make_checkbox(title: str, target=None, action: Optional[bytes] = None, on: bool = True) -> NSButton:
    button = NSButton.alloc().initWithFrame_(NSMakeRect(0, 0, 100, 22))
    button.setButtonType_(NSButtonTypeSwitch)
    button.setTitle_(title)
    button.setFont_(NSFont.systemFontOfSize_(13))
    button.setState_(NSControlStateValueOn if on else NSControlStateValueOff)
    if target is not None and action is not None:
        button.setTarget_(target)
        button.setAction_(action)
    return button


def make_popup(items: Iterable[str], target=None, action: Optional[bytes] = None, width: float = 100) -> NSPopUpButton:
    popup = NSPopUpButton.alloc().initWithFrame_pullsDown_(NSMakeRect(0, 0, width, 26), False)
    popup.setFont_(NSFont.systemFontOfSize_(13))
    popup.addItemsWithTitles_(list(items))
    if target is not None and action is not None:
        popup.setTarget_(target)
        popup.setAction_(action)
    return popup


def make_progress() -> NSProgressIndicator:
    bar = NSProgressIndicator.alloc().initWithFrame_(NSMakeRect(0, 0, 100, 12))
    bar.setStyle_(NSProgressIndicatorStyleBar)
    bar.setIndeterminate_(False)
    bar.setMinValue_(0.0)
    bar.setMaxValue_(1.0)
    bar.setDoubleValue_(0.0)
    bar.setControlSize_(NSControlSizeRegular)
    return bar


def wrap_in_scroll(view, rect) -> NSScrollView:
    scroll = NSScrollView.alloc().initWithFrame_(rect)
    scroll.setBorderType_(1)  # NSBezelBorder
    scroll.setHasVerticalScroller_(True)
    scroll.setHasHorizontalScroller_(False)
    scroll.setAutohidesScrollers_(True)
    scroll.setDrawsBackground_(True)
    scroll.setDocumentView_(view)
    return scroll
