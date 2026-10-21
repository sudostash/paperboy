"""Path resolution with optional root confinement and size limits.

When ``Settings.root`` is set, every input and output path must resolve inside
that directory tree. This is the main guard that makes the server safe to point
at an AI agent: it cannot read or write outside the configured sandbox.
"""
from __future__ import annotations

from pathlib import Path

from .config import Settings
from .errors import PdfUserError


def resolve(path: str, settings: Settings) -> Path:
    """Resolve *path* and enforce root confinement if configured."""
    if not path or not str(path).strip():
        raise PdfUserError("Path must not be empty")
    p = Path(path).expanduser()
    # Resolve symlinks and '..' so confinement cannot be escaped.
    p = p.resolve()
    root = settings.root
    if root is not None and not (p == root or root in p.parents):
        raise PdfUserError(
            f"Path is outside the allowed root. Allowed root: {root}"
        )
    return p


def require_input(path: str, settings: Settings) -> Path:
    """Resolve an input path and verify it exists and is within the size cap."""
    p = resolve(path, settings)
    if not p.exists():
        raise PdfUserError(f"File not found: {p}")
    if not p.is_file():
        raise PdfUserError(f"Not a file: {p}")
    size = p.stat().st_size
    if size > settings.max_file_bytes:
        raise PdfUserError(
            f"File is {size / 1024 / 1024:.1f} MB, over the "
            f"{settings.max_file_bytes / 1024 / 1024:.0f} MB limit"
        )
    return p


def prepare_output(
    input_path: Path,
    output_path: str | None,
    suffix: str,
    overwrite: bool,
    settings: Settings,
    ext: str = ".pdf",
) -> Path:
    """Compute an output path, creating parent dirs, honoring no-overwrite."""
    if output_path:
        out = resolve(output_path, settings)
    else:
        out = input_path.with_name(f"{input_path.stem}{suffix}{ext}")
    if out == input_path:
        raise PdfUserError(
            "Output path is the same as the input; choose a different output_path"
        )
    if out.exists() and not overwrite:
        raise PdfUserError(f"{out} already exists (pass overwrite=true to replace it)")
    out.parent.mkdir(parents=True, exist_ok=True)
    return out


def prepare_output_dir(path: str, settings: Settings) -> Path:
    """Resolve and create an output directory under the configured root."""
    d = resolve(path, settings)
    d.mkdir(parents=True, exist_ok=True)
    return d
