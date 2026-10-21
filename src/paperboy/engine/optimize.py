"""Optimization, repair, and annotation flattening."""
from __future__ import annotations

from pathlib import Path

import pikepdf

from ..config import Settings
from ..errors import DependencyError, PdfUserError
from .base import inherit_protection, open_pdf, run_qpdf


def optimize_pdf(
    path: Path,
    out: Path,
    settings: Settings,
    linearize: bool = True,
    compress: bool = True,
    password: str | None = None,
) -> dict:
    """Linearize ("fast web view") and recompress object streams to shrink the file."""
    before = path.stat().st_size
    with open_pdf(path, settings, password) as pdf:
        pages = len(pdf.pages)
        protection = inherit_protection(pdf, password)
        pdf.save(
            str(out),
            encryption=protection.encryption,
            linearize=linearize,
            compress_streams=compress,
            object_stream_mode=(
                pikepdf.ObjectStreamMode.generate
                if compress
                else pikepdf.ObjectStreamMode.preserve
            ),
        )
    after = out.stat().st_size
    return protection.report({
        "output": str(out),
        "pages": pages,
        "size_before": before,
        "size_after": after,
        "saved_bytes": before - after,
        "linearized": linearize,
    })


def repair_pdf(path: Path, out: Path, settings: Settings, password: str | None = None) -> dict:
    """Rebuild a damaged PDF's structure by reading and rewriting it with qpdf."""
    try:
        pdf = pikepdf.open(str(path), password=password or "")
    except pikepdf.PasswordError:
        raise PdfUserError(
            "This PDF is password protected; supply its password" if not password
            else "Incorrect password"
        ) from None
    except pikepdf.PdfError as exc:
        raise PdfUserError(f"PDF is too damaged to repair automatically: {exc}") from exc
    with pdf:
        pages = len(pdf.pages)
        if pages > settings.max_pages:
            raise PdfUserError(
                f"Document has {pages} pages, over the {settings.max_pages} page limit"
            )
        protection = inherit_protection(pdf, password)
        pdf.save(str(out), encryption=protection.encryption)
    return protection.report({"output": str(out), "pages": pages})


def flatten_pdf(path: Path, out: Path, settings: Settings, password: str | None = None) -> dict:
    """Flatten form fields and annotations into page content (requires the qpdf CLI)."""
    if not settings.qpdf_path:
        raise DependencyError(
            "flatten_pdf needs the qpdf command-line tool on PATH "
            "(install qpdf, or set PAPERBOY_QPDF)"
        )
    # Password and page cap are checked before qpdf does any work.
    with open_pdf(path, settings, password) as src:
        protection = inherit_protection(src, password)
    flattened = out.with_name(f".{out.name}.flattening")
    try:
        proc = run_qpdf(
            settings,
            ["--flatten-annotations=all", "--generate-appearances", str(path), str(flattened)],
            password,
        )
        if proc.returncode not in (0, 3):  # 3 == warnings
            raise PdfUserError(
                f"Flatten failed: {(proc.stderr or proc.stdout).strip()}"
            )
        # qpdf keeps the input's encryption (possibly an old algorithm); re-save with
        # the same protection every other tool gives its output.
        with open_pdf(flattened, settings, password) as pdf:
            pages = len(pdf.pages)
            pdf.save(str(out), encryption=protection.encryption)
    except BaseException:
        out.unlink(missing_ok=True)
        raise
    finally:
        flattened.unlink(missing_ok=True)
    return protection.report(
        {"output": str(out), "pages": pages, "warnings": proc.returncode == 3})
