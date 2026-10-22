"""Text extraction, via pypdf (stronger text layer than pikepdf)."""
from __future__ import annotations

import io
from pathlib import Path

from pypdf import PdfReader
from pypdf.errors import PdfReadError

from ..config import Settings
from ..errors import PdfUserError
from ..pagespec import parse_pages
from ..results import TextResult
from .base import open_pdf, require_copy_permission


def extract_text(
    path: Path,
    settings: Settings,
    pages: str | None = None,
    password: str | None = None,
    max_chars: int = 50000,
) -> TextResult:
    """Extract text from all or selected pages. Returns text plus a truncation flag.

    pikepdf opens the file (passwords, the copy permission and limits are decided
    the same way for every tool) and pypdf reads a plain, decrypted copy. pypdf's
    own decryption needs the cryptography package and mishandles non-ASCII
    passwords.
    """
    plain = io.BytesIO()
    with open_pdf(path.read_bytes(), settings, password) as pdf:
        require_copy_permission(pdf, "extracting its text")
        pdf.save(plain)  # without encryption=, the copy is decrypted
    plain.seek(0)
    try:
        reader = PdfReader(plain)
        total = len(reader.pages)
    except PdfReadError as exc:
        raise PdfUserError(f"Could not read PDF: {exc}") from exc
    idx = parse_pages(pages, total) if pages else range(total)
    parts = [f"--- Page {i + 1} ---\n{reader.pages[i].extract_text() or ''}" for i in idx]
    text = "\n\n".join(parts)
    truncated = len(text) > max_chars
    return {
        "path": str(path),
        "pages_read": len(list(idx)) if pages else total,
        "truncated": truncated,
        "text": text[:max_chars] + ("\n[truncated]" if truncated else ""),
    }
