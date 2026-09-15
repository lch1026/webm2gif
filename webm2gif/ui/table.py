"""``NSTableView`` subclass that accepts ``.webm`` files dropped from Finder."""

from __future__ import annotations

import objc
from AppKit import (
    NSDragOperationCopy,
    NSDragOperationNone,
    NSPasteboardTypeFileURL,
    NSTableView,
)
from Foundation import NSURL


class FileDropTableView(NSTableView):
    """Table view that reports dropped file URLs through ``drop_handler``."""

    def initWithFrame_(self, frame):
        self = objc.super(FileDropTableView, self).initWithFrame_(frame)
        if self is None:
            return None
        self.drop_handler = None
        self.registerForDraggedTypes_([NSPasteboardTypeFileURL])
        return self

    # -- NSDraggingDestination -------------------------------------------
    def _dropped_paths(self, sender):
        paths = []
        pasteboard = sender.draggingPasteboard()
        options = {NSPasteboardTypeFileURL: "file://"}
        urls = pasteboard.readObjectsForClasses_options_([NSURL], options) or []
        for url in urls:
            if url.isFileURL():
                paths.append(url.path())
        return paths

    def draggingEntered_(self, sender):
        return NSDragOperationCopy if self._dropped_paths(sender) else NSDragOperationNone

    def draggingUpdated_(self, sender):
        return NSDragOperationCopy if self._dropped_paths(sender) else NSDragOperationNone

    def prepareForDragOperation_(self, sender):
        return bool(self._dropped_paths(sender))

    def performDragOperation_(self, sender):
        paths = self._dropped_paths(sender)
        if not paths:
            return False
        if callable(self.drop_handler):
            self.drop_handler(paths)
        return True
