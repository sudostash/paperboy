"""extract_tables / export_tables: tables on PDF pages, as rows or CSV files.

pdfplumber (on pdfminer.six) finds the tables. Untrusted PDFs can make that work
explode, so:

- pdfminer never reads the original file: pikepdf (which checks the password and the
  copy permission) writes a plain copy, with no object streams, no document info,
  and each page's boxes and rotation as plain numbers, so both read the same pages;
- pages are measured first (content, each form as often as it is drawn, fonts'
  programs and character maps) and skipped past a budget;
- pdfplumber runs in a child process (see table_worker) with CPU and memory limits,
  killed at the deadline, so neither a dense grid of lines nor anything else can
  keep the server busy. Pages finished before then still count.
"""
from __future__ import annotations

import csv
import io
import json
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pikepdf

from ..config import Settings
from ..errors import PdfUserError
from ..pagespec import parse_pages
from ..results import ExportTablesResult, TablesResult
from .base import open_pdf, require_copy_permission
from .extract import _check_all, _Writer
from .render import _check_target
from .search import _content_size
from .stamp import _box, _rotation, _visible_box
from .streams import decoded_size
from .table_worker import MAX_INTERSECTIONS

MAX_PAGE_CONTENT = 1 * 2**20    # decoded page content (forms as often as drawn) per page
MAX_FONT_DATA = 16 * 2**20      # decoded font programs and character maps per page
MAX_TABLES = 200                # tables one call may return
MAX_CELLS = 50_000              # cells one call may return
MAX_CELL = 1_000                # characters kept per cell
MAX_FORM_DEPTH = 12
TABLE_TIME_LIMIT_S = 60         # per call, enforced by killing the worker
WORKER_MEMORY = 2 * 2**30       # per worker, where the system enforces it (Linux)
MAX_LISTED = 1_000              # pages listed as skipped
STRATEGIES = ("lines", "text")
LETTER = (0.0, 0.0, 612.0, 792.0)
# A plain number, e.g. -5, +1,200.50 or -3%, which spreadsheets may show but not run.
_NUMBER = re.compile(r"[+-]?(\d{1,3}(,\d{3})+|\d+)(\.\d+)?%?")
# What spreadsheets treat as the start of a formula, including full-width forms.
_FORMULA_START = ("=", "+", "-", "@", "＝", "＋", "－", "＠", "−")


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
    further than past *cap*; None if some can't be measured. pdfminer loads each font
    once, so each counts once."""
    if not isinstance(page.obj, pikepdf.Dictionary):  # a damaged page tree (qpdf 11)
        return None
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


def _plain_copy(pdf: pikepdf.Pdf) -> bytes:
    """A copy pdfminer can read cheaply and as qpdf does: decrypted, no object streams
    (pdfminer parses each one whole), no document info (it walks it without limit),
    and each page's boxes and rotation as plain numbers on the page itself."""
    for page in pdf.pages:
        if not isinstance(page.obj, pikepdf.Dictionary):  # skipped as unmeasurable
            continue
        try:
            media = _box(page.mediabox)
        except (TypeError, ValueError):
            media = LETTER
        page.obj.MediaBox = pikepdf.Array(list(media))
        page.obj.CropBox = pikepdf.Array(list(_visible_box(page)))
        page.obj.Rotate = _rotation(page)
        for key in ("/TrimBox", "/BleedBox", "/ArtBox"):
            if key in page.obj:
                del page.obj[key]
    if "/Info" in pdf.trailer:
        del pdf.trailer.Info
    plain = io.BytesIO()
    pdf.save(plain, object_stream_mode=pikepdf.ObjectStreamMode.disable)
    return plain.getvalue()


def _prepare(data: bytes, settings: Settings, password: str | None, pages: str | None
             ) -> tuple[bytes, int, list[int], set[int]]:
    """Check the password and copy permission, choose the pages, find those too costly
    to read, and write the copy pdfminer reads (it never sees a password)."""
    with open_pdf(data, settings, password) as pdf:
        require_copy_permission(pdf, "extracting tables")
        total = len(pdf.pages)
        targets = parse_pages(pages, total, dedupe=True) if pages else list(range(total))
        content: dict = {}
        fonts: dict = {}
        heavy = set()
        for i in targets:
            size = _content_size(pdf.pages[i], MAX_PAGE_CONTENT, content)
            font = _font_size(pdf.pages[i], MAX_FONT_DATA, fonts)
            if size is None or size > MAX_PAGE_CONTENT or font is None or font > MAX_FONT_DATA:
                heavy.add(i)
        return _plain_copy(pdf), total, targets, heavy


# --- running the worker ---------------------------------------------------------------------


def _run_worker(path: Path, total: int, pages: list[int], strategy: str, tables_left: int,
                cells_left: int, seconds: float) -> tuple[list[dict], bool]:
    """Run table_worker on *pages*; return the records it sent and whether it finished
    (rather than being killed at the deadline or by its limits)."""
    request = {"path": str(path), "page_count": total, "pages": pages, "strategy": strategy,
               "max_tables": tables_left, "max_cells": cells_left, "max_cell": MAX_CELL,
               "max_intersections": MAX_INTERSECTIONS,
               "cpu_seconds": max(1, int(seconds) + 2), "memory_bytes": WORKER_MEMORY}
    try:
        proc = subprocess.run([sys.executable, "-I", "-m", "paperboy.engine.table_worker"],
                              input=json.dumps(request).encode(), capture_output=True,
                              timeout=max(seconds, 0.1))
        out, finished = proc.stdout, proc.returncode == 0
        failure = proc.stderr.decode("utf-8", "replace").strip().splitlines()[-1:] or [""]
        if proc.returncode == 1 and not failure[0].startswith(("MemoryError", "RecursionError")):
            # A Python error in the worker itself (not a limit a page hit) is a bug.
            raise RuntimeError(f"table worker failed: {failure[0][:200]}")
    except subprocess.TimeoutExpired as exc:
        out, finished = exc.stdout or b"", False
    records = []
    for line in out.decode("utf-8", "replace").splitlines():
        try:
            records.append(json.loads(line))
        except ValueError:  # the line being written when the worker was stopped
            break
    return records, finished


def _find(path: Path, settings: Settings, pages: str | None, strategy: str,
          max_tables: int, password: str | None) -> dict:
    if strategy not in STRATEGIES:
        raise PdfUserError(f"strategy must be one of: {', '.join(STRATEGIES)}")
    if isinstance(max_tables, bool) or not isinstance(max_tables, int) \
            or not 1 <= max_tables <= MAX_TABLES:
        raise PdfUserError(f"max_tables must be 1 to {MAX_TABLES}")
    data, total, targets, heavy = _prepare(path.read_bytes(), settings, password, pages)
    deadline = time.monotonic() + TABLE_TIME_LIMIT_S
    tables, skipped, truncated, cells = [], [], False, 0
    done: set[int] = set()

    def skip(index: int, reason: str) -> None:
        done.add(index)
        if len(skipped) < MAX_LISTED:
            skipped.append({"page": index + 1, "reason": reason})

    for index in targets:
        if index in heavy:
            skip(index, "too much content to read safely")
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / "plain.pdf"
        copy.write_bytes(data)
        todo = [i for i in targets if i not in heavy]
        while todo and not truncated and time.monotonic() < deadline:
            records, finished = _run_worker(copy, total, todo, strategy,
                                            max_tables - len(tables), MAX_CELLS - cells,
                                            deadline - time.monotonic())
            for record in records:
                if "error" in record:  # the file as a whole can't be read
                    raise PdfUserError(f"Could not read the PDF's layout: {record['error']}")
                index = record["page"]
                if "skipped" in record:
                    skip(index, record["skipped"])
                    continue
                done.add(index)
                for k, rows in enumerate(record["tables"], 1):
                    cells += sum(map(len, rows))
                    tables.append({"page": index + 1, "index": k, "rows": rows,
                                   "row_count": len(rows),
                                   "column_count": max(map(len, rows))})
                truncated = truncated or record.get("truncated", False)
            todo = [i for i in todo if i not in done]
            if finished or not todo:
                break
            # The worker stopped on the first page it didn't report: skip that page and
            # carry on with the rest while time remains.
            reason = ("it took too long to read" if time.monotonic() >= deadline
                      else "it needs too much time or memory to read")
            skip(todo.pop(0), reason)
    searched = len(done)
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
    """Spreadsheets run a cell starting with = + - @ (or their full-width forms) as a
    formula; a CSV built from an untrusted PDF mustn't. Such cells get a leading
    apostrophe, except plain numbers. (Cells are already trimmed of whitespace.)"""
    if text.startswith(_FORMULA_START) and not _NUMBER.fullmatch(text):
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
