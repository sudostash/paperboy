"""Optimization, repair, and annotation flattening."""
from __future__ import annotations

import os
import shutil
import stat
import tempfile
import time
from pathlib import Path

import pikepdf

from ..config import Settings
from ..errors import DependencyError, PdfUserError
from ..results import FlattenResult, OptimizeResult, RepairResult
from . import images
from .base import inherit_protection, open_pdf, open_with_password, run_qpdf

MAX_IMAGE_DPI_RANGE = (36, 1200)
MIN_TARGET_MB = 0.01
MAX_TARGET_MB = 100_000
IMAGE_QUALITY = 85          # JPEG quality for pictures that were JPEG already
# Steps tried, in order, to reach a target size: (resolution, JPEG quality).
TARGET_STEPS = ((300, 85), (200, 80), (150, 75), (120, 65), (96, 55), (72, 45))
OPTIMIZE_TIME_LIMIT_S = 180  # per call; the best result so far is kept when it runs out


def _replaceable(out: Path) -> int | None:
    """The permissions of the file *out* replaces, if there is one; refuses what
    shouldn't be replaced."""
    if not os.path.lexists(out):
        return None
    if out.is_symlink() or not out.is_file():
        raise PdfUserError(f"{out} exists and is not a regular file; choose another "
                           "output_path")
    if not os.access(out, os.W_OK):
        raise PdfUserError(f"{out} is read-only")
    return stat.S_IMODE(os.stat(out).st_mode)


def _check(max_image_dpi, target_size_mb) -> None:
    low, high = MAX_IMAGE_DPI_RANGE
    if max_image_dpi is not None and (
            not isinstance(max_image_dpi, int) or isinstance(max_image_dpi, bool)
            or not low <= max_image_dpi <= high):
        raise PdfUserError(f"max_image_dpi must be {low} to {high}")
    if target_size_mb is not None and (
            not isinstance(target_size_mb, (int, float)) or isinstance(target_size_mb, bool)
            or not MIN_TARGET_MB <= target_size_mb <= MAX_TARGET_MB):
        raise PdfUserError(f"target_size_mb must be {MIN_TARGET_MB} to {MAX_TARGET_MB}")


def optimize_pdf(
    path: Path,
    out: Path,
    settings: Settings,
    linearize: bool = True,
    compress: bool = True,
    password: str | None = None,
    max_image_dpi: int | None = None,
    target_size_mb: float | None = None,
) -> OptimizeResult:
    """Linearize ("fast web view") and recompress object streams to shrink the file.

    ``max_image_dpi`` lowers the resolution of pictures drawn sharper than that.
    ``target_size_mb`` keeps lowering it (and JPEG quality) until the file fits, or
    the steps run out; the smallest result is kept either way.
    """
    _check(max_image_dpi, target_size_mb)
    before = path.stat().st_size
    deadline = time.monotonic() + OPTIMIZE_TIME_LIMIT_S
    target = None if target_size_mb is None else int(target_size_mb * 1_000_000)
    where: list = []   # where images are drawn: worked out once, on the first pass
    mode = _replaceable(out)
    # Attempts are saved in a private folder beside the output (no one can plant a
    # link there; files get the usual mode); only the one kept replaces the output.
    trials = Path(tempfile.mkdtemp(prefix=".paperboy-", dir=out.parent))
    made = iter(range(len(TARGET_STEPS) + 1))

    def attempt(dpi: int | None, quality: int, jpeg_all: bool) -> dict:
        dest = trials / f"{next(made)}.pdf"
        with open_pdf(path, settings, password) as pdf:
            protection = inherit_protection(pdf, password)
            counts, lowest = {"resampled": 0}, 0.0
            if dpi is not None:
                if not where:
                    where.append(images.placements(pdf, deadline))
                found = images.candidates(pdf, *where[0], deadline)
                lowest = max((current for _, current in found), default=0.0)
                counts = images.lower_resolution(pdf, dpi, quality, found, deadline,
                                                 jpeg_all)
            pdf.save(
                str(dest),
                encryption=protection.encryption,
                linearize=linearize,
                compress_streams=compress,
                object_stream_mode=(
                    pikepdf.ObjectStreamMode.generate
                    if compress
                    else pikepdf.ObjectStreamMode.preserve
                ),
            )
            return {"file": dest, "pages": len(pdf.pages), "protection": protection,
                    "dpi": dpi, "resampled": counts["resampled"],
                    "size": dest.stat().st_size, "sharpest": lowest}

    try:
        best = attempt(max_image_dpi, IMAGE_QUALITY, False)
        if target is not None and best["size"] > target:
            for dpi, quality in TARGET_STEPS:
                if max_image_dpi is not None and dpi > max_image_dpi:
                    continue
                if time.monotonic() > deadline:
                    break
                tried = attempt(dpi, quality, True)
                nothing_to_shrink = tried["sharpest"] == 0.0
                if tried["size"] < best["size"]:
                    tried, best = best, tried
                tried["file"].unlink()   # beaten: no need to keep it on disk
                if best["size"] <= target or nothing_to_shrink:
                    break
        if mode is not None:
            os.chmod(best["file"], mode)   # replacing a file keeps its permissions
        os.replace(best["file"], out)
    finally:
        shutil.rmtree(trials, ignore_errors=True)
    return best["protection"].report({
        "output": str(out),
        "pages": best["pages"],
        "size_before": before,
        "size_after": best["size"],
        "saved_bytes": before - best["size"],
        "linearized": linearize,
        "images_resampled": best["resampled"],
        "image_dpi": best["dpi"] if best["resampled"] else None,
        "target_size_bytes": target,
        "target_met": None if target is None else best["size"] <= target,
    })


def repair_pdf(
    path: Path, out: Path, settings: Settings, password: str | None = None
) -> RepairResult:
    """Rebuild a damaged PDF's structure by reading and rewriting it with qpdf."""
    try:
        pdf = open_with_password(str(path), password)
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


def flatten_pdf(
    path: Path, out: Path, settings: Settings, password: str | None = None
) -> FlattenResult:
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
