"""Create PDFs from images, plain text, and Office/HTML documents."""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

from pypdf import PdfReader

from ..config import Settings
from ..errors import DependencyError, PdfUserError

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tif", ".tiff", ".webp"}
TEXT_EXTS = {".txt", ".md", ".log", ".csv", ".json", ".xml", ".yaml", ".yml"}
OFFICE_EXTS = {
    ".doc", ".docx", ".odt", ".rtf", ".xls", ".xlsx", ".ods",
    ".ppt", ".pptx", ".odp", ".html", ".htm",
}


def _page_count(path: Path) -> int:
    return len(PdfReader(str(path)).pages)


def _text_to_pdf(src: Path, out: Path) -> None:
    from reportlab.lib.pagesizes import LETTER
    from reportlab.lib.utils import simpleSplit
    from reportlab.pdfgen import canvas

    font, size, lead, margin = "Courier", 10, 12, 54
    width, height = LETTER
    c = canvas.Canvas(str(out), pagesize=LETTER)
    y = height - margin
    c.setFont(font, size)
    text = src.read_text(encoding="utf-8", errors="replace").replace("\t", "    ")
    for raw in text.splitlines() or [""]:
        for line in simpleSplit(raw, font, size, width - 2 * margin) or [""]:
            if y < margin:
                c.showPage()
                c.setFont(font, size)
                y = height - margin
            c.drawString(margin, y, line)
            y -= lead
    c.save()


def _images_to_pdf(paths: list[Path], out: Path) -> None:
    from PIL import Image

    imgs = []
    try:
        for p in paths:
            img = Image.open(p)
            if img.mode in ("RGBA", "LA", "P"):
                bg = Image.new("RGB", img.size, "white")
                rgba = img.convert("RGBA")
                bg.paste(rgba, mask=rgba.split()[-1])
                img = bg
            else:
                img = img.convert("RGB")
            imgs.append(img)
        imgs[0].save(out, "PDF", resolution=150.0, save_all=True, append_images=imgs[1:])
    finally:
        for img in imgs:
            img.close()


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


def convert_to_pdf(path: Path, out: Path, settings: Settings) -> dict:
    """Create a PDF from an image, a text file, or an Office/HTML document."""
    ext = path.suffix.lower()
    if ext == ".pdf":
        raise PdfUserError("Input is already a PDF")
    if ext in IMAGE_EXTS:
        _images_to_pdf([path], out)
        kind = "image"
    elif ext in TEXT_EXTS:
        _text_to_pdf(path, out)
        kind = "text"
    elif ext in OFFICE_EXTS:
        _office_to_pdf(path, out, settings)
        kind = "office"
    else:
        raise PdfUserError(
            f"Unsupported file type {ext!r}. Supported: images, "
            f"text ({', '.join(sorted(TEXT_EXTS))}), and Office/HTML documents"
        )
    return {"output": str(out), "pages": _page_count(out), "source_kind": kind}


def images_to_pdf(paths: list[Path], out: Path, settings: Settings) -> dict:
    """Combine images into a single PDF, one image per page, in order."""
    if not paths:
        raise PdfUserError("Provide at least one image")
    bad = [p.name for p in paths if p.suffix.lower() not in IMAGE_EXTS]
    if bad:
        raise PdfUserError(f"Not image files: {', '.join(bad)}")
    _images_to_pdf(paths, out)
    return {"output": str(out), "pages": len(paths)}
