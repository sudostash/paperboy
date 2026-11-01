"""Page-level operations: reorder, delete, extract, rotate, merge, split."""
from __future__ import annotations

import io
from pathlib import Path

import pikepdf

from ..config import Settings
from ..errors import PdfUserError
from ..pagespec import parse_pages
from ..results import (
    DeleteResult,
    ExtractResult,
    MergeResult,
    RearrangeResult,
    RotateResult,
    SplitResult,
)
from .base import combine_protections, copy_docinfo, inherit_protection, open_pdf
from .extract import _check_all, _Writer


def rearrange_pages(
    path: Path,
    out: Path,
    page_order: str,
    settings: Settings,
    append_unlisted: bool = False,
    password: str | None = None,
) -> RearrangeResult:
    """Reorder/duplicate/reverse pages. ``page_order`` is the new order, e.g. "3,1,2"."""
    with open_pdf(path, settings, password) as src:
        total = len(src.pages)
        order = parse_pages(page_order, total)
        if append_unlisted:
            listed = set(order)
            order += [i for i in range(total) if i not in listed]
        if len(order) > settings.max_pages:  # "-,-,-" repeats every page
            raise PdfUserError(f"That order has {len(order)} pages; at most "
                               f"{settings.max_pages}")
        dst = pikepdf.new()
        for i in order:
            dst.pages.append(src.pages[i])
        copy_docinfo(src, dst)
        protection = inherit_protection(src, password)
        dst.save(str(out), encryption=protection.encryption)
    return protection.report(
        {"output": str(out), "pages": len(order), "order": [i + 1 for i in order]})


def delete_pages(
    path: Path, out: Path, pages: str, settings: Settings, password: str | None = None
) -> DeleteResult:
    with open_pdf(path, settings, password) as src:
        total = len(src.pages)
        drop = set(parse_pages(pages, total, dedupe=True))
        keep = [i for i in range(total) if i not in drop]
        if not keep:
            raise PdfUserError("Cannot delete every page")
        dst = pikepdf.new()
        for i in keep:
            dst.pages.append(src.pages[i])
        copy_docinfo(src, dst)
        protection = inherit_protection(src, password)
        dst.save(str(out), encryption=protection.encryption)
    return protection.report(
        {"output": str(out), "pages": len(keep), "deleted": sorted(i + 1 for i in drop)})


def extract_pages(
    path: Path, out: Path, pages: str, settings: Settings, password: str | None = None
) -> ExtractResult:
    with open_pdf(path, settings, password) as src:
        idx = parse_pages(pages, len(src.pages))
        dst = pikepdf.new()
        for i in idx:
            dst.pages.append(src.pages[i])
        copy_docinfo(src, dst)
        protection = inherit_protection(src, password)
        dst.save(str(out), encryption=protection.encryption)
    return protection.report({"output": str(out), "pages": len(idx)})


def rotate_pages(
    path: Path,
    out: Path,
    degrees: int,
    settings: Settings,
    pages: str | None = None,
    password: str | None = None,
) -> RotateResult:
    """Rotate pages clockwise by a multiple of 90 degrees (all pages if ``pages`` omitted)."""
    if degrees % 90 != 0:
        raise PdfUserError("degrees must be a multiple of 90")
    with open_pdf(path, settings, password) as src:
        total = len(src.pages)
        targets = set(parse_pages(pages, total, dedupe=True)) if pages else set(range(total))
        for i in targets:
            pikepdf.Page(src.pages[i]).rotate(degrees, relative=True)
        protection = inherit_protection(src, password)
        src.save(str(out), encryption=protection.encryption)
    return protection.report({"output": str(out), "rotated_pages": sorted(i + 1 for i in targets)})


def merge_pdfs(
    inputs: list[Path],
    out: Path,
    settings: Settings,
    passwords: dict[str, str] | None = None,
) -> MergeResult:
    """Merge PDFs in order. ``passwords`` maps an input path to its password."""
    if len(inputs) < 2:
        raise PdfUserError("Provide at least two PDFs to merge")
    passwords = passwords or {}
    dst = pikepdf.new()
    total = 0
    protections = []
    try:
        for p in inputs:
            pw = passwords.get(str(p))
            with open_pdf(p, settings, pw) as src:
                if total + len(src.pages) > settings.max_pages:
                    raise PdfUserError(
                        f"Merged document would exceed the {settings.max_pages} page limit"
                    )
                dst.pages.extend(src.pages)
                total += len(src.pages)
                protections.append(inherit_protection(src, pw))
        protection = combine_protections(protections)
        dst.save(str(out), encryption=protection.encryption)
    finally:
        dst.close()
    return protection.report({"output": str(out), "pages": total, "sources": len(inputs)})


def split_pdf(
    path: Path,
    out_dir: Path,
    settings: Settings,
    ranges: str | None = None,
    password: str | None = None,
    overwrite: bool = False,
) -> SplitResult:
    """Split into one file per range (``"1-3;4-6;7-"``) or one file per page.

    Every name is checked before anything is written, the folder is made only for
    the first file, and if a part fails the parts already written are removed."""
    with open_pdf(path, settings, password) as src:
        total = len(src.pages)
        groups = (
            [parse_pages(r, total) for r in ranges.split(";") if r.strip()]
            if ranges
            else [[i] for i in range(total)]
        )
        if not groups:
            raise PdfUserError("No ranges to split")
        if out_dir.exists() and not out_dir.is_dir():
            raise PdfUserError(f"{out_dir} exists and is not a folder")
        targets = [out_dir / f"{path.stem}_part{n:03d}.pdf" for n in range(1, len(groups) + 1)]
        _check_all(targets, path, overwrite)
        protection = inherit_protection(src, password)
        with _Writer(out_dir) as writer:
            for out, idx in zip(targets, groups, strict=True):
                with pikepdf.new() as dst:
                    for i in idx:
                        dst.pages.append(src.pages[i])
                    copy_docinfo(src, dst)
                    data = io.BytesIO()
                    dst.save(data, encryption=protection.encryption)
                writer.write(out, data.getvalue())
    return protection.report({"output_dir": str(out_dir), "files": [str(t) for t in targets],
                              "count": len(targets)})
