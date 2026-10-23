"""list_pdfs: the PDFs under the allowed root, for clients to pick from.

Listing needs PAPERBOY_ROOT: without a root, the server could be asked to walk the
whole disk. The walk is its own loop (not os.walk) so it can stop anywhere: every
entry, file or folder, counts toward a cap, and depth and time are limited too. It
never descends a link or junction, lists a linked file only if it resolves inside
the root, skips hidden files and folders (a convenience, not a security control),
reads no file contents, and leaves out names with control characters (file names
are untrusted text that reaches the assistant).
"""
from __future__ import annotations

import os
import stat
import time
from datetime import datetime, timezone
from pathlib import Path

from ..config import Settings
from ..errors import PdfUserError
from ..results import PdfListResult

MAX_RESULTS = 1_000      # files one call may list
MAX_VISITED = 50_000     # folder entries (files and folders) one call may look at
MAX_DEPTH = 32           # folders inside folders
LIST_TIME_LIMIT_S = 10   # per call (a network folder can be slow)


def _inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _plain_name(name: str) -> bool:
    return not any(ord(c) < 32 or ord(c) == 127 for c in name)


def _is_junction(entry: os.DirEntry) -> bool:
    check = getattr(entry, "is_junction", None)  # Python 3.12+ (Windows junctions)
    return bool(check and check())


def _record(path: Path, root: Path, info: os.stat_result) -> dict | None:
    try:
        modified = datetime.fromtimestamp(info.st_mtime, timezone.utc)
    except (OverflowError, ValueError, OSError):  # a time no calendar can hold
        return None
    return {"path": str(path), "relative_path": str(path.relative_to(root)),
            "size": info.st_size, "modified": modified.isoformat(timespec="seconds")}


def list_pdfs(settings: Settings, folder: Path | None = None, recursive: bool = True,
              max_results: int = 200) -> PdfListResult:
    """PDFs in *folder* (default: the root), and in its subfolders if *recursive*,
    sorted by path, with their size and when they were last changed."""
    root = settings.root
    if root is None:
        raise PdfUserError("Listing files needs PAPERBOY_ROOT, the folder the server may "
                           "use; it isn't set")
    if isinstance(max_results, bool) or not isinstance(max_results, int) \
            or not 1 <= max_results <= MAX_RESULTS:
        raise PdfUserError(f"max_results must be 1 to {MAX_RESULTS}")
    start = folder or root
    if not start.is_dir():
        raise PdfUserError(f"Not a folder: {start}")
    deadline = time.monotonic() + LIST_TIME_LIMIT_S
    found, visited, skipped, truncated = [], 0, 0, False
    stack = [(start, 0)]
    while stack and not truncated:
        here, depth = stack.pop()
        try:
            entries = os.scandir(here)
        except OSError:  # unreadable, or gone
            skipped += 1
            continue
        subfolders = []
        with entries:
            for entry in entries:
                visited += 1
                if visited > MAX_VISITED or time.monotonic() > deadline:
                    truncated = True
                    break
                name = entry.name
                if name.startswith(".") or not _plain_name(name):
                    continue
                try:
                    if entry.is_dir(follow_symlinks=False) and not entry.is_symlink() \
                            and not _is_junction(entry):
                        if recursive:
                            if depth + 1 > MAX_DEPTH:
                                truncated = True
                            else:
                                subfolders.append(Path(entry.path))
                        continue
                    if not name.lower().endswith(".pdf"):
                        continue
                    path = Path(entry.path)
                    if entry.is_symlink():
                        # Listed only if it resolves to a file inside the root; the size
                        # and type come from the same stat as the file it points to.
                        real = path.resolve(strict=True)
                        if not _inside(real, root):
                            continue
                        info = os.stat(real)
                    else:
                        info = entry.stat(follow_symlinks=False)
                except OSError:  # a broken link, or gone since it was listed
                    continue
                if stat.S_ISREG(info.st_mode):
                    record = _record(path, root, info)
                    if record is not None:
                        found.append(record)
        # Visit subfolders in name order (the stack takes the last first).
        stack.extend((sub, depth + 1) for sub in sorted(subfolders, reverse=True))
    found.sort(key=lambda f: f["relative_path"])
    if len(found) > max_results:
        found, truncated = found[:max_results], True
    return {"root": str(root), "folder": str(start), "files": found, "count": len(found),
            "truncated": truncated, "skipped_folders": skipped}
