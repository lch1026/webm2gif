"""Finding ``.webm`` files on disk."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable

from . import WEBM_SUFFIXES


def is_webm(path: str | os.PathLike[str]) -> bool:
    return Path(path).suffix.lower() in WEBM_SUFFIXES


def _hidden(path: Path) -> bool:
    return path.name.startswith(".")


def discover_webm_files(folder: str | os.PathLike[str], recursive: bool = True) -> list[Path]:
    """Collect ``.webm`` files inside ``folder`` (hidden entries are skipped)."""
    root = Path(folder)
    if not root.is_dir():
        return []

    results: list[Path] = []
    if recursive:
        for current_root, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(name for name in dirnames if not name.startswith("."))
            for name in sorted(filenames):
                if name.startswith("."):
                    continue
                candidate = Path(current_root) / name
                if is_webm(candidate):
                    results.append(candidate)
    else:
        for candidate in sorted(root.iterdir()):
            if _hidden(candidate):
                continue
            if candidate.is_file() and is_webm(candidate):
                results.append(candidate)
    return results


def expand_inputs(paths: Iterable[str | os.PathLike[str]], recursive: bool = True) -> list[Path]:
    """Turn a mix of files and folders into a de-duplicated list of inputs."""
    collected: list[Path] = []
    seen: set[str] = set()

    def add(candidate: Path) -> None:
        key = str(candidate.resolve())
        if key not in seen:
            seen.add(key)
            collected.append(candidate)

    for raw in paths:
        path = Path(raw)
        if path.is_dir():
            for found in discover_webm_files(path, recursive=recursive):
                add(found)
        elif path.is_file() and is_webm(path):
            add(path)
    return collected


def output_for(source: str | os.PathLike[str], output_dir: str | os.PathLike[str] | None = None) -> Path:
    """Destination ``.gif`` path for ``source``."""
    source_path = Path(source)
    folder = Path(output_dir) if output_dir else source_path.parent
    return folder / f"{source_path.stem}.gif"
