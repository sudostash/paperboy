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


@pytest.fixture
def make_encrypted(make_pdf, tmp_path: Path):
    """Build an AES-256 encrypted PDF whose user password is *password*."""
    import pikepdf

    def factory(name: str = "locked.pdf", password: str = "pw", pages: int = 3,
                owner: str | None = None, allow: pikepdf.Permissions | None = None) -> Path:
        plain = make_pdf(f"plain-{name}", pages)
        out = tmp_path / name
        with pikepdf.open(plain) as pdf:
            pdf.save(out, encryption=pikepdf.Encryption(
                user=password, owner=owner or password, R=6,
                allow=allow or pikepdf.Permissions()))
        return out
    return factory


@pytest.fixture
def fake_binary(tmp_path: Path):
    """Write an executable shell script standing in for an external tool."""
    def factory(name: str, body: str) -> str:
        path = tmp_path / "bin" / name
        path.parent.mkdir(exist_ok=True)
        path.write_text("#!/bin/sh\n" + body)
        path.chmod(0o755)
        return str(path)
    return factory


def page_labels(path, password=None) -> list[str]:
    """Each page's text. Encrypted files are decrypted with pikepdf, like the server does."""
    import io

    import pikepdf
    from pypdf import PdfReader
    with pikepdf.open(path, password=password or "") as pdf:
        buf = io.BytesIO()
        pdf.save(buf)
    r = PdfReader(buf)
    return [(r.pages[i].extract_text() or "").strip() for i in range(len(r.pages))]


# Protection flags every writer result carries for an unprotected input.
UNPROTECTED = {"password_protected": False, "permissions_restricted": False,
               "user_password_changed": False}

HAS_SOFFICE = bool(shutil.which("soffice") or shutil.which("libreoffice"))
HAS_QPDF = bool(shutil.which("qpdf"))
