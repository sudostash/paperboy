"""The part of pdf_to_markdown that runs pdfminer's layout analysis, in a separate process.

Like table_worker (whose limits and reporting it shares): pdfminer's work on a hostile
page can't be bounded from outside, so this runs as a child process with CPU and memory
limits, killed at the deadline, and sends one JSON line per page.

For each page it sends the text lines in pdfminer's reading order (which follows
columns), each with its position, font size, whether it is bold, and the text box it
belongs to; and, unless asked not to, the tables pdfplumber finds, with their position.
Turning that into Markdown is left to the server, which sees every page: headings are
told apart by comparing font sizes across the whole document.

Run as ``python -I -m paperboy.engine.markdown_worker``: the request is JSON on stdin.
"""
from __future__ import annotations

import json
import math
import re
import statistics
import sys

from .table_worker import _emit, _intersections, _limit, _short, clean_cell

# pdfminer gives a character it can't map to Unicode the text "(cid:N)".
_UNMAPPED = re.compile(r"\(cid:\d+\)")
_BOLD = re.compile(r"bold|black|heavy|semibold|demibold", re.IGNORECASE)
MAX_LINE = 1_000             # characters kept per line


def _line_record(line, box: int, height: float) -> list | None:
    """[text, x0, top, x1, bottom, size, bold, box] for a text line (top-down
    coordinates, as pdfplumber's), or None if it has no characters, or a position or
    size that isn't a finite number (huge numbers in the page make them infinite)."""
    from pdfminer.layout import LTChar

    chars = [c for c in line if isinstance(c, LTChar)]
    if not chars:
        return None
    # Characters pdfminer couldn't map; text that merely reads "(cid:9)" stays as it is.
    text = "".join("\ufffd" if isinstance(c, LTChar) and _UNMAPPED.fullmatch(c.get_text())
                   else c.get_text() for c in line).strip()
    if not text:
        return None
    if len(text) > MAX_LINE:
        text = text[:MAX_LINE - 1] + "…"
    size = statistics.median(round(c.size, 1) for c in chars)
    bold = sum(bool(_BOLD.search(c.fontname.split("+")[-1])) for c in chars) * 2 > len(chars)
    x0, y0, x1, y1 = line.bbox
    if not all(math.isfinite(v) for v in (x0, y0, x1, y1, size, height)):
        return None
    return [text, round(x0, 1), round(height - y1, 1), round(x1, 1), round(height - y0, 1),
            size, bold, box]


def _lines(layout, height: float, budget: dict) -> tuple[list[list], bool]:
    """The page's text lines in reading order, as many as *budget* (lines, and
    characters of text) allows, charged to it; and whether there were more. Lines
    inside a text box share its number; loose lines (pdfminer leaves a lone bullet
    outside any box) get one of their own."""
    from pdfminer.layout import LTFigure, LTTextBox, LTTextLine

    found: list[list] = []
    box = 0
    stack = [iter(layout)]
    while stack:
        item = next(stack[-1], None)
        if item is None:
            stack.pop()
            continue
        if isinstance(item, LTFigure):  # text inside forms, boxed by all_texts
            stack.append(iter(item))
            continue
        if isinstance(item, LTTextBox):
            members = list(item)
        elif isinstance(item, LTTextLine):
            members = [item]
        else:
            continue
        box += 1
        for line in members:
            record = _line_record(line, box, height)
            if record is None:
                continue
            if budget["lines"] <= 0 or len(record[0]) > budget["text"]:
                return found, True
            budget["lines"] -= 1
            budget["text"] -= len(record[0])
            found.append(record)
    return found, False


def _tables(page, request: dict, budget: dict) -> tuple[list[dict], bool]:
    """The page's tables as {"bbox": [x0, top, x1, bottom], "rows"}; and whether a
    limit stopped them. Pages with too many ruled lines to analyse get none."""
    if _intersections(page) > request["max_intersections"]:
        return [], False
    found = []
    for table in page.find_tables():
        rows = [[clean_cell(c, request["max_cell"]) for c in row] for row in table.extract()]
        rows = [row for row in rows if any(row)]
        if not rows:
            continue
        if not all(math.isfinite(v) for v in table.bbox):
            continue
        size = sum(map(len, rows))
        text = sum(len(cell) for row in rows for cell in row)
        if budget["tables"] <= 0 or size > budget["cells"] or text > budget["text"]:
            return found, True
        budget["tables"] -= 1
        budget["cells"] -= size
        budget["text"] -= text
        found.append({"bbox": [round(v, 1) for v in table.bbox], "rows": rows})
    return found, False


def run(request: dict) -> None:
    import pdfplumber

    budget = {"tables": request["max_tables"], "cells": request["max_cells"],
              "lines": request["max_lines"], "text": request["max_text"]}
    try:
        pdf = pdfplumber.open(request["path"], laparams={"all_texts": True})
    except Exception as exc:  # noqa: BLE001 - any failure to read the file at all
        _emit({"error": _short(exc)})
        return
    with pdf:
        if len(pdf.pages) != request["page_count"]:
            _emit({"error": "the page tree can't be read consistently"})
            return
        for index in request["pages"]:
            page = pdf.pages[index]
            try:
                lines, more = _lines(page.layout, float(page.height), budget)
                tables, full = _tables(page, request, budget) if request["tables"] \
                    else ([], False)
                record = {"page": index, "lines": lines, "tables": tables}
                if more or full:
                    _emit({**record, "truncated": True})
                    return
                _emit(record)
            except Exception as exc:  # noqa: BLE001 - a damaged page skips only that page
                _emit({"page": index, "skipped": f"it can't be read: {_short(exc)}"})
            finally:
                page.close()


def main() -> None:
    request = json.loads(sys.stdin.read())
    _limit(request["cpu_seconds"], request["memory_bytes"])
    sys.setrecursionlimit(5_000)
    run(request)


if __name__ == "__main__":
    main()
