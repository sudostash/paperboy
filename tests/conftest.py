"""Shared pytest fixtures."""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from reportlab.pdfgen import canvas

from paperboy.config import Settings


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        root=tmp_path.resolve(),
        soffice_path=shutil.which("soffice") or shutil.which("libreoffice"),
        qpdf_path=shutil.which("qpdf"),
    )


def _make_pdf(path: Path, pages: int) -> Path:
    c = canvas.Canvas(str(path))
    for i in range(pages):
        c.drawString(72, 720, f"PAGE-{i + 1}")
        c.showPage()
    c.save()
    return path


@pytest.fixture
def make_pdf(tmp_path: Path):
    def factory(name: str = "doc.pdf", pages: int = 5) -> Path:
        return _make_pdf(tmp_path / name, pages)
    return factory


@pytest.fixture
def doc(make_pdf) -> Path:
    return make_pdf("doc.pdf", 5)


def page_labels(path, password=None) -> list[str]:
    from pypdf import PdfReader
    r = PdfReader(str(path))
    if r.is_encrypted:
        r.decrypt(password or "")
    return [(r.pages[i].extract_text() or "").strip() for i in range(len(r.pages))]


HAS_SOFFICE = bool(shutil.which("soffice") or shutil.which("libreoffice"))
HAS_QPDF = bool(shutil.which("qpdf"))
