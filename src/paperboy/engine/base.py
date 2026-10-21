"""Shared engine helpers: opening, limit enforcement, metadata copy."""
from __future__ import annotations

import subprocess
from contextlib import contextmanager
from pathlib import Path

import pikepdf

from ..config import Settings
from ..errors import PdfUserError


@contextmanager
def open_pdf(path: Path, settings: Settings, password: str | None = None):
    """Open a PDF with pikepdf, enforcing the page-count cap.

    Raises PdfUserError for a missing/incorrect password or an oversized document.
    """
    try:
        pdf = pikepdf.open(str(path), password=password or "")
    except pikepdf.PasswordError:
        if password:
            raise PdfUserError("Incorrect password") from None
        raise PdfUserError("This PDF is password protected; supply its password") from None
    except pikepdf.PdfError as exc:
        raise PdfUserError(f"Could not open PDF: {exc}") from exc
    try:
        n = len(pdf.pages)
        if n > settings.max_pages:
            raise PdfUserError(
                f"Document has {n} pages, over the {settings.max_pages} page limit"
            )
        yield pdf
    finally:
        pdf.close()


def is_encrypted(path: Path) -> bool:
    """True if the PDF is encrypted, without needing its password."""
    try:
        with pikepdf.open(str(path)) as pdf:
            return pdf.is_encrypted
    except pikepdf.PasswordError:
        return True  # needs a user password to open, so it is encrypted


def copy_docinfo(src: pikepdf.Pdf, dst: pikepdf.Pdf) -> None:
    """Carry basic document info to a freshly built PDF."""
    try:
        with dst.open_metadata(set_pikepdf_as_editor=False) as dst_meta, \
                src.open_metadata() as src_meta:
            for key in src_meta:
                dst_meta[key] = src_meta[key]
    except Exception:
        # Metadata copy is best-effort; never fail an operation over it.
        pass


def run_qpdf(
    settings: Settings, args: list[str], password: str | None = None, timeout: int = 120
) -> subprocess.CompletedProcess:
    """Run the qpdf CLI and return the completed process.

    The password is sent on stdin (``--password-file=-``), never in argv, so
    other local users cannot read it from the process list. A timeout becomes a
    PdfUserError rather than an internal crash.
    """
    cmd = [settings.qpdf_path]
    if password:
        cmd.append("--password-file=-")
    cmd += args
    try:
        return subprocess.run(cmd, input=password or "", capture_output=True, text=True,
                              timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise PdfUserError(f"qpdf timed out after {timeout}s") from exc
