"""Render pages to images with PDFium (pypdfium2): files on disk, or PNG bytes to view."""
from __future__ import annotations

import io
import os
import tempfile
import threading
import time
from pathlib import Path

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_raw

from ..config import Settings
from ..errors import PdfUserError
from ..pagespec import parse_pages
from ..paths import same_file
from ..results import BlankPagesResult, RenderResult, ViewResult
from .base import open_pdf, require_copy_permission

DPI_RANGE = (36, 600)
MAX_PIXELS = 40_000_000         # per image, about 6300 x 6300; bounds memory per page
MAX_TOTAL_PIXELS = 400_000_000  # per render_pages call; bounds time and disk use
VIEW_MAX_PAGES = 5
VIEW_SIDE_RANGE = (200, 2000)
BLANK_DPI = 50              # enough to see marks, cheap to render
BLANK_MAX_SIDE = 1000       # bounds pixels per page whatever its size
BLANK_THRESHOLD_RANGE = (0.0, 5.0)
NEARLY_BLANK_LIMIT = 0.5    # % ink up to which a text-free page is worth a look
BLANK_TIME_LIMIT_S = 60     # per call; a hostile file can make each page slow
INK_CONTRAST = 25           # grey levels from the background that count as a mark
FORMATS = {"png": ("png", "PNG", ".png"), "jpeg": ("jpeg", "JPEG", ".jpg"),
           "jpg": ("jpeg", "JPEG", ".jpg")}

# PDFium is not thread-safe, and the MCP SDK runs sync tools on worker threads. Every
# PDFium call holds this lock, but only for that call, so one long job can't stall
# every other render; encoding and disk writes happen outside it.
_PDFIUM_LOCK = threading.Lock()


def _plain_copy(data: bytes, settings: Settings, password: str | None, *,
                exporting: bool) -> tuple[bytes, int]:
    """Check the password, page cap and, when exporting, the copy permission.

    Returns the document decrypted (unchanged if it wasn't encrypted) and its page
    count, so PDFium never handles passwords: it rejects some non-ASCII ones that
    pikepdf accepts, and every tool should decide passwords the same way.
    """
    with open_pdf(data, settings, password) as pdf:
        if exporting:
            require_copy_permission(pdf, "exporting pages as images")
        if not pdf.is_encrypted:
            return data, len(pdf.pages)
        plain = io.BytesIO()
        pdf.save(plain)  # without encryption=, the copy is decrypted
        return plain.getvalue(), len(pdf.pages)


def _open(data: bytes) -> pdfium.PdfDocument:
    # Bytes pikepdf checked (and decrypted), never a second read of the path, so the
    # file can't be swapped between the permission check and rendering.
    with _PDFIUM_LOCK:
        try:
            doc = pdfium.PdfDocument(data)
        except pdfium.PdfiumError as exc:
            raise PdfUserError(f"Could not open PDF: {exc}") from exc
        # Render form fields too; without this PDFium skips them, so a filled-in form
        # would look empty (and a page of fields could be reported blank).
        doc.init_forms()
        return doc


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


def _render(doc: pdfium.PdfDocument, index: int, scale: float, *, grayscale: bool = False):
    """Render one page to a PIL image that no longer references PDFium memory."""
    with _PDFIUM_LOCK:
        try:
            page = doc[index]
            try:
                image = page.render(scale=scale, grayscale=grayscale).to_pil()
                # to_pil shares the bitmap's buffer for these modes; detach it.
                return image.copy() if image.mode in ("RGBA", "RGBX", "L") else image
            finally:
                page.close()
        except pdfium.PdfiumError as exc:
            raise PdfUserError(f"Could not render page {index + 1}: {exc}") from exc


def _has_text(doc: pdfium.PdfDocument, index: int) -> bool:
    """True if the page has any text object, even whitespace or invisible OCR text.

    Probes page objects instead of building PDFium's text index, which a tiny file
    can make take minutes and gigabytes. Counting any text as content errs toward
    keeping a page.
    """
    with _PDFIUM_LOCK:
        try:
            page = doc[index]
            try:
                objects = page.get_objects(filter=[pdfium_raw.FPDF_PAGEOBJ_TEXT],
                                           max_depth=15)
                return next(objects, None) is not None
            finally:
                page.close()
        except pdfium.PdfiumError as exc:
            raise PdfUserError(f"Could not read page {index + 1}: {exc}") from exc


def _ink_percent(image) -> float:
    """Share of pixels that clearly differ from the page's background (its median shade).

    Measuring against the background, not white, keeps off-white or coloured scans
    blank; a uniform page of any colour has no ink.
    """
    hist = image.histogram()
    total = sum(hist)
    seen, background = 0, 255
    for shade, count in enumerate(hist):
        seen += count
        if seen * 2 >= total:
            background = shade
            break
    ink = sum(count for shade, count in enumerate(hist) if abs(shade - background) > INK_CONTRAST)
    return ink / total * 100


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
    data, total = _plain_copy(path.read_bytes(), settings, password, exporting=True)
    if total == 0:
        raise PdfUserError("Document has no pages to render")
    targets = parse_pages(pages, total, dedupe=True) if pages else list(range(total))
    files = [out_dir / f"{path.stem}_p{i + 1:03d}{ext}" for i in targets]
    for f in files:
        _check_target(f, path, overwrite)
    scale = dpi / 72
    written: list[Path] = []
    doc = _open(data)
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
    data, total = _plain_copy(path.read_bytes(), settings, password, exporting=False)
    targets = parse_pages(pages or "1", total, dedupe=True)
    if len(targets) > VIEW_MAX_PAGES:
        raise PdfUserError(f"View at most {VIEW_MAX_PAGES} pages per call")
    viewed, images = [], []
    doc = _open(data)
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


def find_blank_pages(
    path: Path,
    settings: Settings,
    pages: str | None = None,
    ink_threshold_percent: float = 0.005,
    password: str | None = None,
) -> BlankPagesResult:
    """Sort text-free pages into blank and nearly blank.

    A page with any text object is content. Otherwise it is rendered small in
    grayscale: blank if at most *ink_threshold_percent* of its pixels differ from
    its background, nearly blank up to NEARLY_BLANK_LIMIT (dust, a stamp, initials:
    look with view_pages before deleting). Analysing pages isn't copying them, so a
    copy-restricted PDF only needs its open password. Stops after
    BLANK_TIME_LIMIT_S, reporting ``complete: false``.
    """
    if not BLANK_THRESHOLD_RANGE[0] <= ink_threshold_percent <= BLANK_THRESHOLD_RANGE[1]:
        raise PdfUserError(
            f"ink_threshold_percent must be between {BLANK_THRESHOLD_RANGE[0]:g} and "
            f"{BLANK_THRESHOLD_RANGE[1]:g}"
        )
    data, total = _plain_copy(path.read_bytes(), settings, password, exporting=False)
    targets = parse_pages(pages, total, dedupe=True) if pages else list(range(total))
    details, checked = [], 0
    if targets:
        deadline = time.monotonic() + BLANK_TIME_LIMIT_S
        doc = _open(data)
        try:
            for i in targets:
                if checked and time.monotonic() > deadline:
                    break
                checked += 1
                if _has_text(doc, i):
                    continue
                scale = min(BLANK_DPI / 72, BLANK_MAX_SIDE / max(_page_size(doc, i)))
                ink = _ink_percent(_render(doc, i, scale, grayscale=True))
                if ink <= max(ink_threshold_percent, NEARLY_BLANK_LIMIT):
                    verdict = "blank" if ink <= ink_threshold_percent else "nearly_blank"
                    details.append({"page": i + 1, "ink_percent": round(ink, 4),
                                    "verdict": verdict})
        finally:
            _close(doc)
    return {
        "path": str(path), "total_pages": total, "pages_checked": checked,
        "complete": checked == len(targets),
        "blank_pages": [d["page"] for d in details if d["verdict"] == "blank"],
        "nearly_blank_pages": [d["page"] for d in details if d["verdict"] == "nearly_blank"],
        "details": details, "ink_threshold_percent": ink_threshold_percent,
    }
