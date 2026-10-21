"""Inspection: page count, metadata, encryption status, validation."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pikepdf

from ..config import Settings
from ..errors import PdfUserError
from .base import open_pdf


def pdf_info(path: Path, settings: Settings, password: str | None = None) -> dict:
    """Page count, size, encryption status, and metadata.

    Reports ``is_encrypted`` without a password; page count and metadata need one.
    """
    info: dict = {
        "path": str(path),
        "size_bytes": path.stat().st_size,
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
        meta: dict[str, str] = {}
        try:
            with pdf.open_metadata() as m:
                for key in m:
                    meta[str(key)] = str(m[key])
        except Exception:
            pass
        info["metadata"] = meta
        if len(pdf.pages):
            box = pdf.pages[0].mediabox
            info["first_page_size_pt"] = [float(box[2]) - float(box[0]),
                                          float(box[3]) - float(box[1])]
        if pdf.is_encrypted:
            info["permissions"] = {k: bool(v) for k, v in zip(
                pikepdf.Permissions._fields, pdf.allow, strict=False)}
    return info


def check_pdf(path: Path, settings: Settings, password: str | None = None) -> dict:
    """Validate structural integrity.

    Uses the qpdf CLI's checker when available for a thorough report; otherwise
    falls back to attempting a full parse with pikepdf.
    """
    problems: list[str] = []
    if settings.qpdf_path:
        cmd = [settings.qpdf_path, "--check"]
        if password:
            cmd.append(f"--password={password}")
        cmd.append(str(path))
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        output = (proc.stdout + proc.stderr).strip()
        # qpdf exit codes: 0 ok, 2 errors, 3 warnings.
        healthy = proc.returncode == 0
        for line in output.splitlines():
            line = line.strip()
            if line and not line.lower().startswith("checking"):
                if "no syntax or stream encoding errors" in line.lower():
                    continue
                problems.append(line)
        return {"path": str(path), "healthy": healthy, "checker": "qpdf",
                "problems": problems, "raw": output[:4000]}

    # Fallback: a successful open + page walk means it parses.
    try:
        with open_pdf(path, settings, password) as pdf:
            for _ in pdf.pages:
                pass
        return {"path": str(path), "healthy": True, "checker": "pikepdf", "problems": []}
    except PdfUserError:
        raise
    except Exception as exc:  # noqa: BLE001
        return {"path": str(path), "healthy": False, "checker": "pikepdf",
                "problems": [str(exc)]}
