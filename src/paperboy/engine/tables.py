"""extract_tables / export_tables: tables on PDF pages, as rows or CSV files.

pdfplumber (on pdfminer.six) finds the tables. pdfminer is pure Python and decodes
whole streams: page content, and the fonts and character maps it reads. So pages
are measured first (content, forms, and their fonts' programs and maps) and skipped
past a budget; a call stops after a time limit; and pdfminer only ever sees bytes
pikepdf checked and decrypted.
"""
from __future__ import annotations

import csv
import io
import re
import time
from pathlib import Path

import pdfplumber
import pikepdf

from ..config import Settings
from ..errors import PdfUserError
from ..pagespec import parse_pages
from ..results import ExportTablesResult, TablesResult
from .base import open_pdf, require_copy_permission
from .extract import _check_all, _Writer
from .render import _check_target
from .search import _content_size
from .stamp import _displayed_size, _isolate, _placement, _rotation
from .streams import decoded_size

MAX_PAGE_CONTENT = 1 * 2**20   # decoded page content (forms included) read per page
MAX_FONT_DATA = 16 * 2**20     # decoded font programs and character maps per page
MAX_TABLES = 200               # tables one call may return
MAX_CELLS = 50_000             # cells one call may return
MAX_CELL = 1_000               # characters kept per cell
MAX_FORM_DEPTH = 12
TABLE_TIME_LIMIT_S = 60        # per call
MAX_LISTED = 1_000             # pages listed as skipped
STRATEGIES = {
    # Tables drawn with lines between cells (most reports, invoices, statements).
    "lines": {"vertical_strategy": "lines", "horizontal_strategy": "lines"},
    # Tables laid out by alignment only, without lines (rougher).
    "text": {"vertical_strategy": "text", "horizontal_strategy": "text"},
}
_NUMBER = re.compile(r"[+-]?[\d.,\s%]+")


# --- budgets -------------------------------------------------------------------------------


def _font_streams(font) -> list:
    """The streams pdfminer decodes for a font: its program, ToUnicode and CMap."""
    found = []
    fonts = [font]
    descendants = font.get("/DescendantFonts")
    if isinstance(descendants, pikepdf.Array):
        fonts += [d for d in descendants if isinstance(d, pikepdf.Dictionary)]
    for f in fonts:
        for key in ("/ToUnicode", "/Encoding"):
            if isinstance(f.get(key), pikepdf.Stream):
                found.append(f.get(key))
        descriptor = f.get("/FontDescriptor")
        if isinstance(descriptor, pikepdf.Dictionary):
            found += [descriptor.get(k) for k in ("/FontFile", "/FontFile2", "/FontFile3")
                      if isinstance(descriptor.get(k), pikepdf.Stream)]
    return found


def _font_size(page: pikepdf.Page, cap: int, sizes: dict) -> int | None:
    """Decoded bytes of the font data a page (and the forms it draws) uses, counted no
    further than past *cap*; None if some can't be measured."""
    total, seen, stack = 0, set(), [(page.obj.get("/Resources"), 0)]
    while stack:
        resources, depth = stack.pop()
        if not isinstance(resources, pikepdf.Dictionary):
            continue
        fonts = resources.get("/Font")
        for _, font in fonts.items() if isinstance(fonts, pikepdf.Dictionary) else []:
            if not isinstance(font, pikepdf.Dictionary):
                continue
            for stream in _font_streams(font):
                key = stream.objgen
                if key in seen:
                    continue
                seen.add(key)
                if key not in sizes:
                    sizes[key] = decoded_size(stream, cap)
                if sizes[key] is None:
                    return None
                total += sizes[key]
                if total > cap:
                    return total
        xobjects = resources.get("/XObject")
        for _, form in xobjects.items() if isinstance(xobjects, pikepdf.Dictionary) else []:
            if isinstance(form, pikepdf.Stream) and form.get("/Subtype") == pikepdf.Name.Form \
                    and form.objgen not in seen and depth < MAX_FORM_DEPTH:
                seen.add(form.objgen)
                stack.append((form.get("/Resources"), depth + 1))
    return total


def _upright(pdf: pikepdf.Pdf, page: pikepdf.Page) -> None:
    """Draw a rotated page's content upright on an unrotated page, as viewers show it:
    pdfplumber ignores /Rotate, so a turned table would come out transposed. Only the
    private copy pdfplumber reads is changed."""
    a, b, c, d, e, f = _placement(page)  # displayed coordinates -> page coordinates
    det = a * d - b * c
    inverse = (d / det, -b / det, -c / det, a / det, (c * f - d * e) / det,
               (b * e - a * f) / det)
    width, height = _displayed_size(page)
    _isolate(pdf, page)
    page.contents_add(pikepdf.Stream(pdf, ("q " + " ".join(f"{v:.6f}" for v in inverse)
                                           + " cm\n").encode()), prepend=True)
    page.contents_add(pikepdf.Stream(pdf, b"\nQ\n"))
    page.obj.MediaBox = pikepdf.Array([0, 0, width, height])
    for key in ("/Rotate", "/CropBox"):
        if key in page.obj:
            del page.obj[key]


def _prepare(data: bytes, settings: Settings, password: str | None, pages: str | None
             ) -> tuple[bytes, list[int], set[int]]:
    """Check the password and copy permission, choose the pages, find those too costly
    to read, and return the bytes decrypted for pdfminer (which never sees a password)."""
    with open_pdf(data, settings, password) as pdf:
        require_copy_permission(pdf, "extracting tables")
        total = len(pdf.pages)
        targets = parse_pages(pages, total, dedupe=True) if pages else list(range(total))
        content: dict = {}
        fonts: dict = {}
        heavy, turned = set(), False
        for i in targets:
            size = _content_size(pdf.pages[i], MAX_PAGE_CONTENT, content)
            font = _font_size(pdf.pages[i], MAX_FONT_DATA, fonts)
            if size is None or size > MAX_PAGE_CONTENT or font is None or font > MAX_FONT_DATA:
                heavy.add(i)
            elif _rotation(pdf.pages[i]):
                _upright(pdf, pdf.pages[i])
                turned = True
        if pdf.is_encrypted or turned:
            plain = io.BytesIO()
            pdf.save(plain)  # without encryption=, the copy is decrypted
            data = plain.getvalue()
    return data, targets, heavy


# --- finding tables ------------------------------------------------------------------------


def _cell(value) -> str:
    text = re.sub(r"\s+", " ", value or "").strip()
    return text if len(text) <= MAX_CELL else text[:MAX_CELL - 1] + "…"


def _find(path: Path, settings: Settings, pages: str | None, strategy: str,
          max_tables: int, password: str | None) -> dict:
    if strategy not in STRATEGIES:
        raise PdfUserError(f"strategy must be one of: {', '.join(STRATEGIES)}")
    if isinstance(max_tables, bool) or not isinstance(max_tables, int) \
            or not 1 <= max_tables <= MAX_TABLES:
        raise PdfUserError(f"max_tables must be 1 to {MAX_TABLES}")
    data, targets, heavy = _prepare(path.read_bytes(), settings, password, pages)
    tables, skipped, searched, truncated, cells = [], [], 0, False, 0
    deadline = time.monotonic() + TABLE_TIME_LIMIT_S
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for n, index in enumerate(targets):
            if n and time.monotonic() > deadline:
                break
            searched += 1
            if index in heavy:
                if len(skipped) < MAX_LISTED:
                    skipped.append(index + 1)
                continue
            page = pdf.pages[index]
            try:
                found = page.find_tables(STRATEGIES[strategy])
                for k, table in enumerate(found, 1):
                    rows = [[_cell(c) for c in row] for row in table.extract()]
                    rows = [row for row in rows if any(row)]  # drop empty spacer rows
                    if not rows:
                        continue
                    if len(tables) >= max_tables or cells + sum(map(len, rows)) > MAX_CELLS:
                        truncated = True
                        break
                    cells += sum(map(len, rows))
                    tables.append({"page": index + 1, "index": k, "rows": rows,
                                   "row_count": len(rows),
                                   "column_count": max(map(len, rows))})
            finally:
                page.close()  # pdfplumber caches each page's objects until closed
            if truncated:
                break
    return {"tables": tables, "pages_searched": searched,
            "complete": truncated or searched == len(targets), "truncated": truncated,
            "pages_skipped": skipped}


def extract_tables(path: Path, settings: Settings, pages: str | None = None,
                   strategy: str = "lines", max_tables: int = 20,
                   password: str | None = None) -> TablesResult:
    """Tables on all pages, or the pages given, as rows of cell text."""
    return {"path": str(path), **_find(path, settings, pages, strategy, max_tables, password)}


# --- CSV -----------------------------------------------------------------------------------


def _safe_cell(text: str) -> str:
    """Spreadsheets run a cell starting with = + - @ as a formula; a CSV built from an
    untrusted PDF mustn't. Such cells get a leading apostrophe, except plain numbers."""
    if text[:1] in ("=", "+", "-", "@", "\t", "\r") and not _NUMBER.fullmatch(text):
        return "'" + text
    return text


def _csv(rows: list[list[str]], escape_formulas: bool) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\r\n")
    for row in rows:
        writer.writerow([_safe_cell(c) if escape_formulas else c for c in row])
    return buf.getvalue().encode("utf-8-sig")  # the BOM makes Excel read UTF-8


def export_tables(path: Path, out_dir: Path, settings: Settings, pages: str | None = None,
                  strategy: str = "lines", max_tables: int = 20,
                  escape_formulas: bool = True, password: str | None = None,
                  overwrite: bool = False) -> ExportTablesResult:
    """Save each table as a CSV file in *out_dir* (page001-table01.csv, ...)."""
    found = _find(path, settings, pages, strategy, max_tables, password)
    planned = [(t, out_dir / f"page{t['page']:03d}-table{t['index']:02d}.csv")
               for t in found["tables"]]
    _check_all([target for _, target in planned], path, overwrite)
    files = []
    with _Writer(out_dir) as writer:
        for table, target in planned:
            _check_target(target, path, overwrite)
            writer.write(target, _csv(table["rows"], escape_formulas))
            files.append({"file": str(target), "page": table["page"], "index": table["index"],
                          "row_count": table["row_count"],
                          "column_count": table["column_count"]})
    return {"output_dir": str(out_dir), "files": files,
            "pages_searched": found["pages_searched"], "complete": found["complete"],
            "truncated": found["truncated"], "pages_skipped": found["pages_skipped"]}
