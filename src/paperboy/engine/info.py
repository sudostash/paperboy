"""Inspection: page count, metadata, encryption status, validation."""
from __future__ import annotations

import re
from pathlib import Path

import pikepdf

from ..config import Settings
from ..errors import PdfUserError
from ..results import CheckResult, InfoResult
from .base import open_pdf, run_qpdf
from .metadata import read_metadata

# qpdf echoes the user password, even one it recovered from the owner password, and
# the value may span lines; redact up to the next line qpdf always prints after it.
_PASSWORD_VALUE = re.compile(
    r"(User password = ).*?"
    r"(?=\n(?:Supplied password is|extract for|stream encryption|File is|No syntax)|\Z)",
    re.DOTALL,
)
# Lines qpdf --check prints for every file; anything else is a warning or error.
_INFO_LINE = re.compile(
    r"checking |PDF Version:|R = |P = |User password = |Supplied password is|File is "
    r"|No syntax or stream encoding errors|errors that qpdf cannot detect"
    r"|qpdf: operation succeeded with warnings|(?:stream|string|file) encryption method:"
    r"|(?:extract|print|modify) [^:]*: (?:allowed|not allowed)$"
)


def pdf_info(path: Path, settings: Settings, password: str | None = None) -> InfoResult:
    """Page count, size, encryption status, and metadata.

    Reports ``is_encrypted`` without a password; page count and metadata need one.
    """
    info: dict = {
        "path": str(path),
        "size_bytes": path.stat().st_size,
        "is_encrypted": False,
        "note": None,
        "pages": None,
        "pdf_version": None,
        "linearized": None,
        "metadata": None,
        "first_page_size_pt": None,
        "permissions": None,
    }
    try:
        pdf = pikepdf.open(str(path), password=password or "")
    except pikepdf.PasswordError:
        info["is_encrypted"] = True
        if not password:
            info["note"] = "Password required for page count and metadata"
            return info
        raise PdfUserError("Incorrect password") from None
    except pikepdf.PdfError as exc:
        raise PdfUserError(f"Could not open PDF: {exc}") from exc

    with pdf:
        info["is_encrypted"] = pdf.is_encrypted
        info["pages"] = len(pdf.pages)
        info["pdf_version"] = pdf.pdf_version
        info["linearized"] = bool(getattr(pdf, "is_linearized", False))
        try:
            info["metadata"] = read_metadata(pdf)
        except Exception:  # unreadable XMP must not hide the rest of the report
            info["metadata"] = {}
        if len(pdf.pages):
            box = pdf.pages[0].mediabox
            info["first_page_size_pt"] = [float(box[2]) - float(box[0]),
                                          float(box[3]) - float(box[1])]
        if pdf.is_encrypted:
            info["permissions"] = {k: bool(v) for k, v in zip(
                pikepdf.Permissions._fields, pdf.allow, strict=False)}
    return info


def check_pdf(path: Path, settings: Settings, password: str | None = None) -> CheckResult:
    """Validate structural integrity.

    Uses the qpdf CLI's checker when available for a thorough report; otherwise
    falls back to attempting a full parse with pikepdf.
    """
    if settings.qpdf_path:
        proc = run_qpdf(settings, ["--check", str(path)], password)
        output = _PASSWORD_VALUE.sub(r"\1[redacted]", (proc.stdout + proc.stderr).strip())
        # qpdf exit codes: 0 ok, 2 errors, 3 warnings.
        problems = [
            line.strip() for line in output.splitlines()
            if line.strip() and not _INFO_LINE.match(line.strip())
        ]
        return {"path": str(path), "healthy": proc.returncode == 0, "checker": "qpdf",
                "problems": problems, "raw": output[:4000]}

    # Fallback: a successful open + page walk means it parses.
    try:
        with open_pdf(path, settings, password) as pdf:
            for _ in pdf.pages:
                pass
        return {"path": str(path), "healthy": True, "checker": "pikepdf", "problems": [],
                "raw": None}
    except PdfUserError:
        raise
    except Exception as exc:  # noqa: BLE001
        return {"path": str(path), "healthy": False, "checker": "pikepdf",
                "problems": [str(exc)], "raw": None}
