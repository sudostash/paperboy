"""Create PDFs from images, plain text, and Office/HTML documents."""
from __future__ import annotations

import io
import os
import shutil
import subprocess
import tempfile
import warnings
from pathlib import Path

import pikepdf
from pypdf import PdfReader

from ..config import Settings
from ..errors import DependencyError, PdfUserError
from ..results import ConvertResult, ImagesResult

IMAGE_FORMATS = ("PNG", "JPEG", "GIF", "BMP", "TIFF", "WEBP")
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tif", ".tiff", ".webp"}
TEXT_EXTS = {".txt", ".md", ".log", ".csv", ".json", ".xml", ".yaml", ".yml"}
MAX_TOTAL_PIXELS = 400_000_000  # per images_to_pdf call, like render_pages
OFFICE_EXTS = {
    ".doc", ".docx", ".odt", ".rtf", ".xls", ".xlsx", ".ods",
    ".ppt", ".pptx", ".odp", ".html", ".htm",
}


def _page_count(path: Path) -> int:
    return len(PdfReader(str(path)).pages)


def _text_to_pdf(src: Path, out: Path, max_pages: int) -> None:
    from reportlab.lib.pagesizes import LETTER
    from reportlab.pdfbase.pdfmetrics import stringWidth
    from reportlab.pdfgen import canvas

    font, size, lead, margin = "Courier", 10, 12, 54
    width, height = LETTER
    c = canvas.Canvas(str(out), pagesize=LETTER)  # nothing is written until save()
    pages = 1
    y = height - margin
    c.setFont(font, size)
    text = src.read_text(encoding="utf-8", errors="replace").replace("\t", "    ")
    # Courier is monospaced, so wrap by character count. Word-based wrapping would
    # collapse indentation and runs of spaces, which matter in code, YAML, and logs.
    per_line = max(1, int((width - 2 * margin) // stringWidth(" ", font, size)))
    for raw in text.splitlines() or [""]:
        for line in [raw[i:i + per_line] for i in range(0, len(raw), per_line)] or [""]:
            if y < margin:
                pages += 1
                if pages > max_pages:
                    raise PdfUserError(
                        f"Text would need more than {max_pages} pages, over the "
                        f"{max_pages} page limit"
                    )
                c.showPage()
                c.setFont(font, size)
                y = height - margin
            c.drawString(margin, y, line)
            y -= lead
    c.save()


def _open_image(p: Path):
    """Open and decode an image, turning bad or oversized input into a PdfUserError."""
    from PIL import Image, UnidentifiedImageError

    img = None
    try:
        with warnings.catch_warnings():
            # Pillow only warns between MAX_IMAGE_PIXELS and twice that; refuse those too.
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            # Only common formats: Pillow's others include EPS, handed to Ghostscript.
            img = Image.open(p, formats=IMAGE_FORMATS)
            img.load()
        return img
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        error = PdfUserError(f"{p.name} is too large to convert safely")
        cause = exc
    except (UnidentifiedImageError, OSError) as exc:
        error = PdfUserError(f"{p.name} is not a readable image: {exc}")
        cause = exc
    if img is not None:
        img.close()
    raise error from cause


def _pixels(paths: list[Path]) -> None:
    """Refuse a call whose images add up to too many pixels, from their headers alone."""
    from PIL import Image, UnidentifiedImageError

    total = 0
    for p in paths:
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(p, formats=IMAGE_FORMATS) as img:  # reads the header only
                    total += img.width * img.height
        except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
            raise PdfUserError(f"{p.name} is too large to convert safely") from exc
        except (UnidentifiedImageError, OSError) as exc:
            raise PdfUserError(f"{p.name} is not a readable image: {exc}") from exc
    if total > MAX_TOTAL_PIXELS:
        raise PdfUserError(f"These images have {total / 1e6:.0f} million pixels, over the "
                           f"{MAX_TOTAL_PIXELS / 1e6:.0f} million limit per call; convert "
                           "fewer at a time")


def _images_to_pdf(paths: list[Path], out: Path) -> None:
    """One page per image. Each is decoded, made into a page and let go before the
    next, so memory doesn't grow with the number of images."""
    from PIL import Image

    _pixels(paths)
    with pikepdf.new() as pdf:
        for p in paths:
            img = _open_image(p)
            try:
                if img.mode in ("RGBA", "LA", "P"):
                    rgb = Image.new("RGB", img.size, "white")
                    rgba = img.convert("RGBA")
                    rgb.paste(rgba, mask=rgba.split()[-1])
                    rgba.close()
                else:
                    rgb = img.convert("RGB")
                page = io.BytesIO()
                rgb.save(page, "PDF", resolution=150.0)
                if rgb is not img:
                    rgb.close()
            finally:
                img.close()
            with pikepdf.open(page) as one:
                pdf.pages.extend(one.pages)
        pdf.save(out)


def _office_to_pdf(src: Path, out: Path, settings: Settings) -> None:
    exe = settings.soffice_path
    if not exe:
        raise DependencyError(
            f"Converting {src.suffix} needs LibreOffice (soffice) on PATH "
            "(install LibreOffice, or set PAPERBOY_SOFFICE)"
        )
    with tempfile.TemporaryDirectory() as tmp:
        profile = Path(tmp) / "profile"
        try:
            proc = subprocess.run(
                [exe, f"-env:UserInstallation=file://{profile}", "--headless",
                 "--convert-to", "pdf", "--outdir", tmp, str(src)],
                capture_output=True, text=True, timeout=settings.soffice_timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise PdfUserError(
                f"LibreOffice timed out after {settings.soffice_timeout}s"
            ) from exc
        produced = Path(tmp) / f"{src.stem}.pdf"
        if proc.returncode != 0 or not produced.exists():
            raise PdfUserError(
                f"LibreOffice conversion failed: {(proc.stderr or proc.stdout).strip()}"
            )
        shutil.move(str(produced), str(out))


def _made(out: Path, settings: Settings, make) -> int:
    """Have *make* write the PDF in a private folder beside *out*, check it against the
    size and page limits any input meets, then move it into place: never written
    through a link at *out*, never left half-written. Returns its page count."""
    folder = Path(tempfile.mkdtemp(prefix=".paperboy-", dir=out.parent))
    try:
        made = folder / "out.pdf"
        make(made)
        size = made.stat().st_size
        if size > settings.max_file_bytes:
            raise PdfUserError(f"The PDF would be {size / 1024 / 1024:.1f} MB, over the "
                               f"{settings.max_file_bytes / 1024 / 1024:.0f} MB limit")
        pages = _page_count(made)
        if pages > settings.max_pages:
            raise PdfUserError(f"The PDF would have {pages} pages, over the "
                               f"{settings.max_pages} page limit")
        os.replace(made, out)
        return pages
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def convert_to_pdf(path: Path, out: Path, settings: Settings) -> ConvertResult:
    """Create a PDF from an image, a text file, or an Office/HTML document."""
    ext = path.suffix.lower()
    if ext == ".pdf":
        raise PdfUserError("Input is already a PDF")
    if ext in IMAGE_EXTS:
        kind, make = "image", lambda made: _images_to_pdf([path], made)
    elif ext in TEXT_EXTS:
        kind, make = "text", lambda made: _text_to_pdf(path, made, settings.max_pages)
    elif ext in OFFICE_EXTS:
        kind, make = "office", lambda made: _office_to_pdf(path, made, settings)
    else:
        raise PdfUserError(
            f"Unsupported file type {ext!r}. Supported: images, "
            f"text ({', '.join(sorted(TEXT_EXTS))}), and Office/HTML documents"
        )
    return {"output": str(out), "pages": _made(out, settings, make), "source_kind": kind}


def images_to_pdf(paths: list[Path], out: Path, settings: Settings) -> ImagesResult:
    """Combine images into a single PDF, one image per page, in order."""
    if not paths:
        raise PdfUserError("Provide at least one image")
    bad = [p.name for p in paths if p.suffix.lower() not in IMAGE_EXTS]
    if bad:
        raise PdfUserError(f"Not image files: {', '.join(bad)}")
    if len(paths) > settings.max_pages:
        raise PdfUserError(
            f"{len(paths)} images is over the {settings.max_pages} page limit"
        )
    return {"output": str(out), "pages": _made(out, settings,
                                               lambda made: _images_to_pdf(paths, made))}
