"""pdf_to_markdown / export_markdown: a PDF's text as Markdown, for reading by a model.

markdown_worker reads each page's text lines in reading order (pdfminer's layout
analysis follows columns) and its tables (pdfplumber), under the same budgets, limits
and child process as extract_tables (see tables.read_pages). This module turns them
into Markdown:

- headings: lines in a font clearly larger than the body text (the size most text
  is in, across the whole document) become #, ## and ### by size; a short bold line
  starting its own block becomes the next level down;
- lists: a line starting with a bullet or "1." / "a)", or with a bullet drawn just
  before it (often a glyph with no text of its own), becomes a list item;
- paragraphs: the lines of a text block are joined, mending words hyphenated at a
  line break;
- tables: as Markdown tables where they stand, their text left out of the paragraphs.

Everything taken from the PDF is escaped, so its text can't add links, pictures, HTML
or formatting of its own: what it says is shown as written. (A web address written out
in the text stays as written; some viewers show such addresses as links.)
"""
from __future__ import annotations

import bisect
import math
import re
import unicodedata
from collections import Counter
from pathlib import Path

from ..config import Settings
from ..errors import PdfUserError
from ..results import ExportMarkdownResult, MarkdownResult
from .extract import _write
from .render import _check_target
from .table_worker import MAX_INTERSECTIONS
from .tables import MAX_CELL, MAX_CELLS, MAX_LISTED, MAX_TABLES, _prepare, _run, read_pages

MARKDOWN_TIME_LIMIT_S = 60      # per call, enforced by killing the worker
MAX_LINES = 300_000             # text lines one call reads
MAX_TEXT = 4_000_000            # characters of text (and table cells) one call reads
MAX_CHARS = 1_000_000           # the most pdf_to_markdown may return
MAX_MARKERS = 5_000             # bullet-only lines matched to their text, per page
MAX_ROW_SCAN = 200              # lines looked at for each one
MAX_BOXES = 5_000               # text boxes put in reading order, per page
MAX_ORDER_DEPTH = 12            # columns within columns
MIN_GUTTER = 12                 # points between columns
MIN_COLUMN = 40                 # points wide, for a column (not a column of numbers)
HEADING_RATIO = 1.15            # a heading's font is at least this much larger than the body
MAX_HEADING = 150               # characters; longer lines are text, however large
MAX_BOLD_HEADING = 80
BULLETS = "•●○◦▪▫■□‣⁃∙·–—-*➢➤►▶✓✔\uf0b7\uf0a7\ufffd"
_NUMBERED = re.compile(r"(\(?\d{1,3}[.)]|\(?[a-zA-Z][.)])$")
_ITEM = re.compile(r"(?P<marker>[" + re.escape(BULLETS) + r"]|\d{1,3}[.)]|[a-zA-Z][)])\s+"
                   r"(?P<text>\S.*)")
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069]")
# Invisible characters a reader can't see but a model reads: zero-width space, marks
# that only set direction, word joiners, byte-order marks, tag characters. (Zero-width
# joiners, which some scripts and emoji need, stay.)
_INVISIBLE = re.compile("[\u200b\u200e\u200f\u061c\u2060-\u2064\ufeff\U000e0000-\U000e007f]")
_INLINE = re.compile(r"([\\`*_\[\]<|~])")
_ENTITY = re.compile(r"&(?=#?\w+;)")
_LINE_START = [
    (re.compile(r"(#{1,6})(?=\s|$)"), r"\\\1"),
    (re.compile(r"([>+=-])"), r"\\\1"),
    (re.compile(r"(\d{1,9})([.)])(?=\s|$)"), r"\1\\\2"),
]


def escape(text: str, line_start: bool = True) -> str:
    """*text* as literal Markdown: nothing in it is read as formatting, a link, a
    picture or HTML. With line_start, it is safe at the start of a line too (in a
    heading or a table cell it isn't at one)."""
    text = _INLINE.sub(r"\\\1", _CONTROL.sub(" ", _INVISIBLE.sub("", text)))
    text = _ENTITY.sub(r"\\&", text).strip()
    if not line_start:
        return re.sub(r"(#+)$", r"\\\1", text)  # a heading's closing #s would vanish
    for pattern, replacement in _LINE_START:
        if pattern.match(text):
            return pattern.sub(replacement, text, count=1)
    return text


def _wide(char: str) -> bool:
    return unicodedata.east_asian_width(char) in ("W", "F")


def join_lines(lines: list[str]) -> str:
    """The lines of a paragraph as one: a word hyphenated at a line break is mended
    (when the next line goes on in lower case), and lines of Chinese, Japanese or
    Korean text join without a space."""
    text = ""
    for line in lines:
        line = line.strip()
        if not text:
            text = line
        elif not line:
            continue
        elif len(text) > 1 and text[-1] == "-" and text[-2].isalpha() and line[0].islower():
            text = text[:-1] + line
        elif _wide(text[-1]) and _wide(line[0]):
            text += line
        else:
            text += " " + line
    return text


# --- reading the worker's records --------------------------------------------------------


def _number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) \
        and math.isfinite(value)


class Line:
    __slots__ = ("text", "x0", "top", "x1", "bottom", "size", "bold", "box", "item", "rest")

    def __init__(self, record: list):
        if not (isinstance(record, list) and len(record) == 8 and isinstance(record[0], str)
                and all(_number(v) for v in record[1:6])
                and isinstance(record[6], bool) and isinstance(record[7], int)):
            raise RuntimeError("markdown worker sent a malformed line")
        (self.text, self.x0, self.top, self.x1, self.bottom, self.size, self.bold,
         self.box) = record
        self.item: str | None = None  # the list marker this line starts with, if any
        self.rest = self.text         # its text after that marker

    @property
    def middle(self) -> float:
        return (self.top + self.bottom) / 2

    @property
    def height(self) -> float:
        return max(self.bottom - self.top, 1.0)


def _page(record: dict) -> tuple[list[Line], list[dict]]:
    lines, tables = record.get("lines"), record.get("tables")
    if not isinstance(lines, list) or not isinstance(tables, list):
        raise RuntimeError("markdown worker sent a malformed page")
    for table in tables:
        if not (isinstance(table, dict) and isinstance(table.get("bbox"), list)
                and len(table["bbox"]) == 4
                and all(_number(v) for v in table["bbox"])
                and isinstance(table.get("rows"), list) and table["rows"]
                and all(isinstance(r, list) and all(isinstance(c, str) for c in r)
                        for r in table["rows"])):
            raise RuntimeError("markdown worker sent a malformed table")
    return [Line(r) for r in lines], tables


# --- building the Markdown -------------------------------------------------------------


def _outside_tables(lines: list[Line], tables: list[dict]) -> list[Line]:
    """The lines that aren't in a table (by their middle): the table shows them."""
    rows = sorted(lines, key=lambda line: line.middle)
    middles = [line.middle for line in rows]
    inside: set[int] = set()
    for table in tables:
        x0, top, x1, bottom = table["bbox"]
        for line in rows[bisect.bisect_left(middles, top - 1):
                         bisect.bisect_right(middles, bottom + 1)]:
            if x0 - 1 <= (line.x0 + line.x1) / 2 <= x1 + 1:
                inside.add(id(line))
    return [line for line in lines if id(line) not in inside]


def prepare(lines: list[Line], tables: list[dict]) -> list[Line]:
    """A page's lines without those in its tables, in reading order, with list markers
    attached to their items."""
    return reading_order(_attach_markers(_outside_tables(lines, tables)))


def _marker(text: str) -> tuple[str, str] | None:
    """For a line made of only a bullet, a number or a letter: its list marker in
    Markdown, and what of it stays in the item's text. Markdown has no lettered lists,
    so "a)" items are bullets that keep their letter."""
    text = text.strip()
    if len(text) == 1 and text in BULLETS:
        return "-", ""
    if _NUMBERED.fullmatch(text):
        bare = text.strip("()").rstrip(".)")
        return (bare + ".", "") if bare.isdigit() else ("-", text)
    return None


def _nearest(rows: list[Line], middles: list[float], middle: float, reach: float
             ) -> list[Line]:
    """Up to MAX_ROW_SCAN lines whose middles are within *reach* of *middle*, the
    closest first."""
    after = bisect.bisect_left(middles, middle)
    before = after - 1
    found: list[Line] = []
    while len(found) < MAX_ROW_SCAN:
        up = middle - middles[before] if before >= 0 else math.inf
        down = middles[after] - middle if after < len(rows) else math.inf
        if min(up, down) > reach:
            break
        if up <= down:
            found.append(rows[before])
            before -= 1
        else:
            found.append(rows[after])
            after += 1
    return found


def _attach_markers(lines: list[Line]) -> list[Line]:
    """Lines made of only a bullet or a number, just left of a line on the same row,
    become that line's list marker. A lone unreadable glyph is dropped. (Pages with
    more than MAX_MARKERS such lines keep them as text: matching is bounded.)"""
    markers = {id(line): m for line in lines if (m := _marker(line.text)) is not None}
    if len(markers) > MAX_MARKERS:
        markers = {}
    rows = sorted((line for line in lines if id(line) not in markers),
                  key=lambda line: line.middle)
    middles = [line.middle for line in rows]
    tallest = max((line.height for line in rows), default=0.0)
    kept = []
    for line in lines:
        marker = markers.get(id(line))
        if marker is None:
            kept.append(line)
            continue
        reach = max(tallest, line.height) * 0.6  # no line further off is on its row
        targets = [t for t in _nearest(rows, middles, line.middle, reach)
                   if abs(t.middle - line.middle) <= max(t.height, line.height) * 0.6
                   and line.x1 - 1 <= t.x0 <= line.x1 + 36]
        if targets:
            target = min(targets, key=lambda t: t.x0)
            target.item = marker[0]
            target.rest = f"{marker[1]} {target.text}" if marker[1] else target.text
        elif line.text.strip() != "\ufffd":
            kept.append(line)
    for line in kept:
        if line.item is None and (found := _ITEM.fullmatch(line.text.strip())):
            marker = found["marker"]
            if marker in BULLETS:
                line.item, line.rest = "-", found["text"]
            elif marker[0].isdigit():
                line.item, line.rest = marker.rstrip(".)") + ".", found["text"]
            else:  # a lettered item: a bullet that keeps its letter
                line.item, line.rest = "-", line.text.strip()
    return kept


# --- reading order -----------------------------------------------------------------------
# pdfminer orders text boxes by grouping nearby ones, which puts two columns' headings
# (side by side) before either column's text. So boxes are ordered here instead: the page
# is cut into bands across it; bands that share a gap between columns (a gutter) are one
# region, read column by column, each column the same way; other bands top to bottom.


class _Box:
    __slots__ = ("lines", "x0", "top", "x1", "bottom")

    def __init__(self, lines: list[Line]):
        self.lines = lines
        self.x0 = min(line.x0 for line in lines)
        self.x1 = max(line.x1 for line in lines)
        self.top = min(line.top for line in lines)
        self.bottom = max(line.bottom for line in lines)


def _gaps(boxes: list[_Box], left: float, right: float) -> list[tuple[float, float]]:
    """Stretches from *left* to *right* that no box covers, at least a gutter wide."""
    gaps, edge = [], left
    for box in sorted(boxes, key=lambda b: b.x0):
        if box.x0 - edge >= MIN_GUTTER:
            gaps.append((edge, box.x0))
        edge = max(edge, box.x1)
    if right - edge >= MIN_GUTTER:
        gaps.append((edge, right))
    return gaps


def _still_open(gutters: list[tuple[float, float]], boxes: list[_Box]
                ) -> list[tuple[float, float]]:
    """The gutters no box in *boxes* crosses, narrowed to what they leave open."""
    open_ = []
    for left, right in gutters:
        for box in boxes:
            if box.x1 <= left or box.x0 >= right:
                continue
            if box.x0 - left >= right - box.x1:  # keep the wider side open
                right = box.x0
            else:
                left = box.x1
        if right - left >= MIN_GUTTER:
            open_.append((left, right))
    return open_


def _order(boxes: list[_Box], depth: int = 0) -> list[_Box]:
    if len(boxes) <= 1 or depth > MAX_ORDER_DEPTH:
        return sorted(boxes, key=lambda b: (b.top, b.x0))
    bands: list[list[_Box]] = []
    bottom = None
    for box in sorted(boxes, key=lambda b: (b.top, b.x0)):
        if bands and box.top < bottom:
            bands[-1].append(box)
            bottom = max(bottom, box.bottom)
        else:
            bands.append([box])
            bottom = box.bottom
    regions: list[tuple[list[_Box], list[tuple[float, float]]]] = []
    for band in bands:
        left, right = min(b.x0 for b in band), max(b.x1 for b in band)
        own = [g for g in _gaps(band, left, right)
               if g[0] - left >= MIN_COLUMN and right - g[1] >= MIN_COLUMN]
        if regions and regions[-1][1]:
            region, gutters = regions[-1]
            open_ = _still_open(gutters, band)
            # The columns go on: the band has columns of its own, or it is close below.
            near = min(b.top for b in band) - max(b.bottom for b in region) <= \
                max(MIN_GUTTER, 2 * max(line.height for b in band for line in b.lines))
            if open_ and (own or near):
                regions[-1] = (region + band, open_)
                continue
        regions.append((band, own))
    ordered: list[_Box] = []
    for region, gutters in regions:
        if not gutters:
            ordered += sorted(region, key=lambda b: (b.top, b.x0))
            continue
        cuts = [(left + right) / 2 for left, right in gutters]
        columns: list[list[_Box]] = [[] for _ in range(len(cuts) + 1)]
        for box in region:
            columns[bisect.bisect(cuts, (box.x0 + box.x1) / 2)].append(box)
        for column in columns:
            ordered += _order(column, depth + 1) if len(column) < len(region) else \
                sorted(column, key=lambda b: (b.top, b.x0))
    return ordered


def reading_order(lines: list[Line]) -> list[Line]:
    """The page's lines, box by box in reading order (see above); each box's lines as
    pdfminer gave them. Pages with very many boxes keep pdfminer's order."""
    boxes: dict[int, list[Line]] = {}
    for line in lines:
        boxes.setdefault(line.box, []).append(line)
    if len(boxes) > MAX_BOXES:
        return lines
    return [line for box in _order([_Box(b) for b in boxes.values()]) for line in box.lines]


def body_size(pages: list[list[Line]]) -> float | None:
    """The font size most text is in (by characters), across the document."""
    sizes: Counter = Counter()
    for lines in pages:
        for line in lines:
            sizes[round(line.size * 2) / 2] += len(line.text)
    return sizes.most_common(1)[0][0] if sizes else None


def heading_levels(pages: list[list[Line]], body: float | None) -> dict[float, int]:
    """Font sizes that make a line a heading, largest first: #, ## and ###."""
    if body is None:
        return {}
    sizes = sorted({round(line.size * 2) / 2 for lines in pages for line in lines
                    if _looks_like_heading(line, body)}, reverse=True)
    return {size: min(n, 3) for n, size in enumerate(sizes, 1)}


def _looks_like_heading(line: Line, body: float) -> bool:
    return (line.size >= body * HEADING_RATIO and line.size - body >= 1
            and len(line.text) <= MAX_HEADING
            and sum(c.isalnum() for c in line.text) >= 2)


def _blocks(lines: list[Line], levels: dict[float, int], body: float | None
            ) -> list[tuple[str, int, list[Line]]]:
    """(kind, level, lines): kind is "heading", "item" or "text"."""
    bold_level = min(len(set(levels.values())) + 1, 4)
    blocks: list[tuple[str, int, list[Line]]] = []
    for n, line in enumerate(lines):
        prev = lines[n - 1] if n else None
        nxt = lines[n + 1] if n + 1 < len(lines) else None
        level = levels.get(round(line.size * 2) / 2) if body is not None and \
            _looks_like_heading(line, body) else None
        if level is None and body is not None and line.bold and line.item is None \
                and abs(line.size - body) < 1 and len(line.text) <= MAX_BOLD_HEADING \
                and not line.text.rstrip().endswith((".", ",", ";")) \
                and (prev is None or prev.box != line.box) \
                and (nxt is None or nxt.box != line.box or not nxt.bold):
            level = bold_level
        if level is not None:
            if blocks and blocks[-1][0] == "heading" and blocks[-1][1] == level \
                    and blocks[-1][2][-1].box == line.box:
                blocks[-1][2].append(line)  # a heading wrapped onto two lines
            else:
                blocks.append(("heading", level, [line]))
        elif line.item is not None:
            blocks.append(("item", 0, [line]))
        elif blocks and blocks[-1][0] != "heading" and blocks[-1][2][-1].box == line.box:
            blocks[-1][2].append(line)
        else:
            blocks.append(("text", 0, [line]))
    return [split for block in blocks for split in _numbered(block)]


_BARE_NUMBER = re.compile(r"(\d{1,3})\s+(\S.*)")


def _numbered(block: tuple[str, int, list[Line]]) -> list[tuple[str, int, list[Line]]]:
    """A text block whose every line starts 1, 2, 3, ... (a list numbered without
    dots) as list items."""
    kind, _, members = block
    found = [_BARE_NUMBER.fullmatch(m.text) for m in members]
    if kind != "text" or len(members) < 2 or not all(found):
        return [block]
    numbers = [int(f[1]) for f in found]
    if numbers != list(range(numbers[0], numbers[0] + len(numbers))):
        return [block]
    for member, match in zip(members, found, strict=True):
        member.item, member.rest = f"{match[1]}.", match[2]
    return [("item", 0, [m]) for m in members]


def table_markdown(rows: list[list[str]]) -> str:
    width = max(map(len, rows))
    cells = [[escape(c, line_start=False) for c in row] + [""] * (width - len(row)) for row in rows]
    out = ["| " + " | ".join(cells[0]) + " |", "|" + " --- |" * width]
    out += ["| " + " | ".join(row) + " |" for row in cells[1:]]
    return "\n".join(out)


def page_markdown(lines: list[Line], tables: list[dict], levels: dict[float, int],
                  body: float | None) -> tuple[list[str], int]:
    """The page's Markdown, as blocks in reading order; and how many are headings.
    *lines* are as prepare() leaves them."""
    placed = sorted(tables, key=lambda t: t["bbox"][1])
    out: list[str] = []
    headings = 0
    in_list: bool | None = None  # the last block is a list (of bullets?): items follow
                                 # it without a blank line
    for kind, level, members in _blocks(lines, levels, body):
        while placed and placed[0]["bbox"][1] <= members[0].top:
            out.append(table_markdown(placed.pop(0)["rows"]))
            in_list = None
        first = members[0].rest if kind == "item" else members[0].text
        text = escape(join_lines([first] + [m.text for m in members[1:]]),
                      line_start=kind != "heading")
        marker = members[0].item
        if kind == "heading":
            headings += 1
            out.append("#" * level + " " + text)
        elif kind == "item" and in_list == (marker == "-"):
            out[-1] += f"\n{marker} {text}"
        elif kind == "item":
            out.append(f"{marker} {text}")
        else:
            out.append(text)
        # A list goes on while items of its kind (bullets or numbers) follow.
        in_list = (marker == "-") if kind == "item" else None
    out += [table_markdown(t["rows"]) for t in placed]
    return out, headings


# --- the tools -------------------------------------------------------------------------


def _convert(path: Path, settings: Settings, pages: str | None, include_tables: bool,
             page_markers: bool, password: str | None, action: str) -> dict:
    data, total, targets, heavy = _prepare(path.read_bytes(), settings, password, pages,
                                           action)
    read: dict[int, tuple[list[Line], list[dict]]] = {}
    budget = {"lines": MAX_LINES, "tables": MAX_TABLES, "cells": MAX_CELLS,
              "text": MAX_TEXT}

    def run(copy, todo, seconds):
        return _run("paperboy.engine.markdown_worker",
                    {"path": str(copy), "page_count": total, "pages": todo,
                     "tables": include_tables, "max_tables": budget["tables"],
                     "max_cells": budget["cells"], "max_cell": MAX_CELL,
                     "max_lines": budget["lines"], "max_text": budget["text"],
                     "max_intersections": MAX_INTERSECTIONS}, seconds)

    def take(record) -> bool:
        lines, tables = _page(record)
        budget["lines"] -= len(lines)
        budget["tables"] -= len(tables)
        budget["cells"] -= sum(len(r) for t in tables for r in t["rows"])
        budget["text"] -= sum(len(line.text) for line in lines) + sum(
            len(c) for t in tables for r in t["rows"] for c in r)
        read[record["page"]] = (prepare(lines, tables), tables)
        return bool(record.get("truncated"))

    count, truncated, skipped, timed_out = read_pages(data, targets, heavy,
                                                      MARKDOWN_TIME_LIMIT_S, run, take)
    ordered = [i for i in targets if i in read]
    body = body_size([read[i][0] for i in ordered])
    levels = heading_levels([read[i][0] for i in ordered], body)
    blocks: list[str] = []
    headings = tables = 0
    empty = []
    for i in ordered:
        lines, found = read[i]
        if not lines and not found:
            if len(empty) < MAX_LISTED:
                empty.append(i + 1)
        page_blocks, page_headings = page_markdown(lines, found, levels, body)
        headings += page_headings
        tables += len(found)
        if page_markers:
            blocks.append(f"<!-- page {i + 1} -->")
        blocks += page_blocks
    return {"blocks": blocks, "pages_read": count, "headings": headings, "tables": tables,
            "truncated": truncated, "complete": truncated or not timed_out,
            "pages_skipped": skipped, "pages_without_text": empty}


def _part(block: str, room: int) -> str | None:
    """The start of a text block, cut at a space to fit *room*; None for a table, a
    page marker, or when too little would fit to read."""
    if block.startswith(("|", "<!--")) or room < 40:
        return None
    part = block[:room]
    if " " in part[room // 2:]:
        part = part[:part.rindex(" ")]
    if (len(part) - len(part.rstrip("\\"))) % 2:  # half of an escape
        part = part[:-1]
    lines = part.rstrip().split("\n")
    if len(lines) > 1 and re.fullmatch(r"-|\d{1,3}\.", lines[-1]):  # a list item's marker
        lines.pop()
    return "\n".join(lines)


def _check_options(include_tables: bool, page_markers: bool) -> None:
    for name, value in (("include_tables", include_tables), ("page_markers", page_markers)):
        if not isinstance(value, bool):
            raise PdfUserError(f"{name} must be true or false")


def _summary(found: dict) -> dict:
    return {key: found[key] for key in ("pages_read", "headings", "tables", "truncated",
                                        "complete", "pages_skipped", "pages_without_text")}


def pdf_to_markdown(path: Path, settings: Settings, pages: str | None = None,
                    include_tables: bool = True, page_markers: bool = True,
                    max_chars: int = 50_000, password: str | None = None) -> MarkdownResult:
    """The PDF's text as Markdown, cut after *max_chars* at a block's end."""
    if isinstance(max_chars, bool) or not isinstance(max_chars, int) \
            or not 1 <= max_chars <= MAX_CHARS:
        raise PdfUserError(f"max_chars must be 1 to {MAX_CHARS:,}")
    _check_options(include_tables, page_markers)
    found = _convert(path, settings, pages, include_tables, page_markers, password,
                     "converting its text to Markdown")
    kept: list[str] = []
    size, cut = 0, False
    for block in found["blocks"]:
        room = max_chars - size - (2 if kept else 0)
        if len(block) > room:
            if part := _part(block, room):  # as much of a long block as fits
                kept.append(part)
            cut = True
            break
        size = max_chars - room + len(block)
        kept.append(block)
    markdown = "\n\n".join(kept)
    if cut:
        markdown += ("\n\n" if kept else "") + "[truncated]"
    return {"path": str(path), "markdown": markdown, "characters": len(markdown),
            **_summary(found), "truncated": found["truncated"] or cut}


def export_markdown(path: Path, out: Path, settings: Settings, pages: str | None = None,
                    include_tables: bool = True, page_markers: bool = True,
                    password: str | None = None, overwrite: bool = False
                    ) -> ExportMarkdownResult:
    """Save the PDF's text as a Markdown file (UTF-8)."""
    _check_options(include_tables, page_markers)
    found = _convert(path, settings, pages, include_tables, page_markers, password,
                     "converting its text to Markdown")
    markdown = "\n\n".join(found["blocks"]) + "\n"
    _check_target(out, path, overwrite)
    _write(out, markdown.encode("utf-8"))
    return {"output": str(out), "characters": len(markdown), **_summary(found)}
