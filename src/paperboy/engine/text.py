"""Text extraction, via pypdf (stronger text layer than pikepdf)."""
from __future__ import annotations

from pathlib import Path

from pypdf import PdfReader
from pypdf.errors import PdfReadError

from ..config import Settings
from ..errors import PdfUserError
from ..pagespec import parse_pages
from ..results import TextResult


def extract_text(
    path: Path,
    settings: Settings,
    pages: str | None = None,
    password: str | None = None,
    max_chars: int = 50000,
) -> TextResult:
    """Extract text from all or selected pages. Returns text plus a truncation flag."""
    try:
        reader = PdfReader(str(path))
        if reader.is_encrypted:
            if not password:
                raise PdfUserError("This PDF is password protected; supply its password")
            if reader.decrypt(password) == 0:
                raise PdfUserError("Incorrect password")
    except PdfReadError as exc:
        raise PdfUserError(f"Could not read PDF: {exc}") from exc

    total = len(reader.pages)
    if total > settings.max_pages:
        raise PdfUserError(f"Document has {total} pages, over the {settings.max_pages} limit")
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
