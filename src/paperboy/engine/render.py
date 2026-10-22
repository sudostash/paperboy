"""Render pages to images with PDFium (pypdfium2): files on disk, or PNG bytes to view."""
from __future__ import annotations

import io
import os
import tempfile
import threading
from pathlib import Path

import pypdfium2 as pdfium

from ..config import Settings
from ..errors import PdfUserError
from ..pagespec import parse_pages
from ..paths import same_file
from ..results import RenderResult, ViewResult
from .base import open_pdf

DPI_RANGE = (36, 600)
MAX_PIXELS = 40_000_000         # per image, about 6300 x 6300; bounds memory per page
MAX_TOTAL_PIXELS = 400_000_000  # per render_pages call; bounds time and disk use
VIEW_MAX_PAGES = 5
VIEW_SIDE_RANGE = (200, 2000)
FORMATS = {"png": ("png", "PNG", ".png"), "jpeg": ("jpeg", "JPEG", ".jpg"),
           "jpg": ("jpeg", "JPEG", ".jpg")}

# PDFium is not thread-safe, and the MCP SDK runs sync tools on worker threads. Every
# PDFium call holds this lock, but only for that call, so one long job can't stall
# every other render; encoding and disk writes happen outside it.
_PDFIUM_LOCK = threading.Lock()


def _page_count(data: bytes, settings: Settings, password: str | None, *,
                exporting: bool) -> int:
    """Check the password, page cap and, when exporting, the copy permission."""
    with open_pdf(data, settings, password) as pdf:
        if (exporting and pdf.is_encrypted and not pdf.owner_password_matched
                and not pdf.allow.extract):
            raise PdfUserError(
                "This PDF does not allow copying its content; exporting pages as images "
                "needs its owner password"
            )
        return len(pdf.pages)


def _open(data: bytes, password: str | None) -> pdfium.PdfDocument:
    # The same bytes pikepdf checked, never a second read of the path, so the file
    # can't be swapped between the permission check and rendering.
    with _PDFIUM_LOCK:
        try:
            return pdfium.PdfDocument(data, password=password or None)
        except pdfium.PdfiumError as exc:
            raise PdfUserError(f"Could not open PDF: {exc}") from exc


def _close(doc: pdfium.PdfDocument) -> None:
    with _PDFIUM_LOCK:
        doc.close()


def _page_size(doc: pdfium.PdfDocument, index: int) -> tuple[float, float]:
    """Page size in points as displayed (rotation applied)."""
    with _PDFIUM_LOCK:
        try:
            page = doc[index]
        except pdfium.PdfiumError as exc:  # e.g. pikepdf repaired pages PDFium can't see
            raise PdfUserError(f"Could not render page {index + 1}: {exc}") from exc
        try:
            return page.get_size()
        finally:
            page.close()


def _render(doc: pdfium.PdfDocument, index: int, scale: float):
    """Render one page to a PIL image that no longer references PDFium memory."""
    with _PDFIUM_LOCK:
        try:
            page = doc[index]
            try:
                image = page.render(scale=scale).to_pil()
                # to_pil shares the bitmap's buffer for these modes; detach it.
                return image.copy() if image.mode in ("RGBA", "RGBX", "L") else image
            finally:
                page.close()
        except pdfium.PdfiumError as exc:
            raise PdfUserError(f"Could not render page {index + 1}: {exc}") from exc


def _check_target(f: Path, src: Path, overwrite: bool) -> None:
    """Refuse targets that would write anywhere but a fresh or replaceable regular file."""
    if not os.path.lexists(f):
        return
    if f.is_symlink() or not f.is_file():
        raise PdfUserError(f"{f} exists and is not a regular file; remove it or pick another "
                           "output_dir")
    if same_file(f, src):
        raise PdfUserError(f"{f} is the same as the input; pick another output_dir")
    if not overwrite:
        raise PdfUserError(f"{f} already exists (pass overwrite=true to replace it)")


def _save(image, f: Path, pil_format: str) -> None:
    """Write via a temp file and os.replace: never through a link, never in place."""
    fd, tmp = tempfile.mkstemp(dir=f.parent, prefix=f".{f.stem}.", suffix=f.suffix)
    try:
        with os.fdopen(fd, "wb") as fh:
            if pil_format == "JPEG":
                image.convert("RGB").save(fh, "JPEG", quality=90)
            else:
                image.save(fh, "PNG")
        os.replace(tmp, f)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def render_pages(
    path: Path,
    out_dir: Path,
    settings: Settings,
    pages: str | None = None,
    dpi: int = 150,
    fmt: str = "png",
    password: str | None = None,
    overwrite: bool = False,
) -> RenderResult:
    """Save pages as PNG or JPEG files named ``<stem>_p001.png``, one per page."""
    if fmt.lower() not in FORMATS:
        raise PdfUserError(f"format must be png or jpeg; got {fmt!r}")
    if not DPI_RANGE[0] <= dpi <= DPI_RANGE[1]:
        raise PdfUserError(f"dpi must be between {DPI_RANGE[0]} and {DPI_RANGE[1]}")
    if out_dir.exists() and not out_dir.is_dir():
        raise PdfUserError(f"{out_dir} exists and is not a folder")
    name, pil_format, ext = FORMATS[fmt.lower()]
    data = path.read_bytes()
    total = _page_count(data, settings, password, exporting=True)
    if total == 0:
        raise PdfUserError("Document has no pages to render")
    targets = parse_pages(pages, total, dedupe=True) if pages else list(range(total))
    files = [out_dir / f"{path.stem}_p{i + 1:03d}{ext}" for i in targets]
    for f in files:
        _check_target(f, path, overwrite)
    scale = dpi / 72
    written: list[Path] = []
    doc = _open(data, password)
    try:
        budget = 0
        for i in targets:  # refuse oversized work before writing anything
            w, h = (round(v * scale) for v in _page_size(doc, i))
            if w * h > MAX_PIXELS:
                raise PdfUserError(
                    f"Page {i + 1} would be {w} x {h} pixels at {dpi} dpi; use a lower dpi"
                )
            budget += w * h
        if budget > MAX_TOTAL_PIXELS:
            raise PdfUserError(
                f"Rendering {len(targets)} pages at {dpi} dpi is {budget / 1e6:.0f} megapixels, "
                f"over the {MAX_TOTAL_PIXELS / 1e6:.0f} MP limit per call; render fewer pages "
                "or a lower dpi"
            )
        out_dir.mkdir(parents=True, exist_ok=True)
        for i, f in zip(targets, files, strict=True):
            _save(_render(doc, i, scale), f, pil_format)
            written.append(f)
    except BaseException:
        for f in written:  # no partial output
            f.unlink(missing_ok=True)
        raise
    finally:
        _close(doc)
    return {"output_dir": str(out_dir), "files": [str(f) for f in files],
            "pages": [i + 1 for i in targets], "dpi": dpi, "format": name}


def view_pages(
    path: Path,
    settings: Settings,
    pages: str | None = None,
    max_side_px: int = 1568,
    password: str | None = None,
) -> tuple[ViewResult, list[bytes]]:
    """Render up to five pages as PNG bytes, each fitted so its long side is *max_side_px*.

    Viewing is display, which PDF permissions always allow, so copy protection
    does not apply here (unlike :func:`render_pages`).
    """
    if not VIEW_SIDE_RANGE[0] <= max_side_px <= VIEW_SIDE_RANGE[1]:
        raise PdfUserError(
            f"max_side_px must be between {VIEW_SIDE_RANGE[0]} and {VIEW_SIDE_RANGE[1]}"
        )
    data = path.read_bytes()
    total = _page_count(data, settings, password, exporting=False)
    targets = parse_pages(pages or "1", total, dedupe=True)
    if len(targets) > VIEW_MAX_PAGES:
        raise PdfUserError(f"View at most {VIEW_MAX_PAGES} pages per call")
    viewed, images = [], []
    doc = _open(data, password)
    try:
        for i in targets:
            # Render a little large, then shrink so the long side is exactly max_side_px.
            scale = max_side_px * 1.02 / max(_page_size(doc, i))
            image = _render(doc, i, scale)
            image.thumbnail((max_side_px, max_side_px))
            buf = io.BytesIO()
            image.save(buf, "PNG")
            images.append(buf.getvalue())
            viewed.append({"page": i + 1, "width_px": image.width, "height_px": image.height})
    finally:
        _close(doc)
    return {"path": str(path), "total_pages": total, "pages": viewed}, images
