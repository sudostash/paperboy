"""get_text_blocks and translate_pdf: a PDF's pages in another language, laid out as they
were. Paperboy doesn't translate (it never goes online): the AI client does, from the
blocks get_text_blocks reads, and translate_pdf puts each translation where its block was.

A block is text set as one piece: a paragraph, a heading, a list item, a table cell.
Characters join a line while they run along one baseline at one size and colour; lines
join the block above them when they continue it (same size, colour and weight, under it
at the spacing its lines keep, starting within it or aligned with it on the right or in
the middle). A break between lines is kept as "\\n" (an address, a column of labels)
unless the next line goes on with the sentence; a line starting with a list marker
starts a new block. Blocks without letters (amounts, times) aren't listed: they need no
translation. Text a reader can't see (a scan's OCR layer, fully transparent text, text
off the page) is in no block. Text is measured in the frame of its direction (a
rotation, in points), so text sized by its matrix and by its font size are seen alike.

translate_pdf removes a translated block's characters from the page's content for real,
as edit_text and redaction do (pictures and drawings stay), and draws the translation
in their place, in the block's reading position, colour and size, in the closest
standard font (Helvetica, Times or Courier, bold or italic as the original; DejaVu Sans
for Greek, Cyrillic and other text those can't show). It is wrapped to the block's
width, aligned as its lines were (left, right or centred, keeping a first line's indent).
It may grow into free space below the block, keeping the gap there was; a block whose
lines are each set on their own (not wrapped prose) may also run on beside them. Other
text, pictures, drawings and form fields bound that space. A translation still needing
more room is shrunk, to no less than MIN_SHRINK of the size, and one that still won't
fit, or would run into other text, is refused. Each page is read back to confirm only
the translated blocks' characters went and each translation is there, and drawn with
and without the translations to confirm every new character shows (not cut off by a
clip, nor under something drawn later). Any problem refuses the whole call: nothing is
written.
"""
from __future__ import annotations

import bisect
import ctypes
import hashlib
import io
import math
import re
import time
import unicodedata
from collections import Counter
from contextlib import ExitStack
from itertools import islice
from pathlib import Path

import pikepdf
import pypdfium2.raw as pdfium_raw
from PIL import ImageChops
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfgen import canvas

from ..config import Settings
from ..errors import PdfUserError
from ..pagespec import parse_pages
from ..results import TextBlocksResult, TranslateResult
from .base import (
    inherit_protection,
    open_pdf,
    require_copy_permission,
    require_modify_permission,
    signature_state,
)
from .edittext import _font_for, _remove, _style, _turned
from .markdown import BULLETS, join_lines
from .redact import (
    TOLERANCE,
    _chars,
    _cut_loose,
    _glyphs,
    _lines,
    _plain,
    _strip_alt_text,
    _verify,
)
from .redact_content import Budget, Unsure, _rect
from .render import _PDFIUM_LOCK, MAX_PIXELS, _close, _open
from .search import MAX_PAGE_CONTENT, _content_size
from .stamp import _visible_box

MAX_REPLY_TEXT = 100_000     # characters of block text one get_text_blocks reply lists
MAX_PAGE_TEXT = 100_000      # characters of text on one page
MAX_BLOCK_TEXT = 4_000       # characters in one block (a longer paragraph is split)
MAX_OBJECTS = 20_000         # pictures and drawings on a page looked at for room
MAX_SEGMENTS = 200_000       # parts of the paths on a page looked at for room
MAX_TRANSLATIONS = 2_000     # blocks one translate_pdf call translates
MAX_TRANSLATION = 8_000      # characters of one block's translation
MAX_TRANSLATED = 400_000     # characters of translation one call draws
MAX_FRAMES = 8               # directions of text a page's drawings are measured in
INK_PIXELS = 10.0            # the smallest new text's size, in pixels, when checked
INK_MAX_SCALE = 4.0          # pixels per point at most, checking
INK_CONTRAST = 16            # grey levels of change that count as drawn
INK_REACH = 0.3              # of the size: how near a character's middle it must show
TRANSLATE_TIME_LIMIT_S = 180  # per call
MIN_SHRINK = 0.6             # smallest share of its size a translation is drawn at
SHRINK_STEP = 0.05
LINE = 0.2                   # of the size: baselines this close are one line
SAME_SIZE = 0.15             # sizes this close (as a share) are the same
SPLIT = 2.0                  # of the size: a wider gap along a line starts a new piece
SPACE = 0.2                  # of the size: a gap this wide between characters is a space
LEADING = 1.8                # of the size: lines further apart are separate blocks
LEADING_DRIFT = 0.2          # of the size: a block's lines keep their spacing to this
INDENT = 1.0                 # of the size: a line may start this far left of its block
ALIGNED = 0.5                # of the size: line edges this close are aligned
BREAK = 0.5                  # of the size: slack before a short line counts as a break
PROSE = 15.0                 # of the size: a block this wide may be wrapped prose
NEIGHBOUR = 0.7              # of the size: baselines this close share a band
GAP = 0.5                    # of the size: kept clear before other text on the band
EDGE = 2.0                   # points kept clear of the page's edge
ASCENT, DESCENT = 0.8, 0.2   # of the size: how far text reaches above and below its line
SINGLE_LEADING = 1.2         # of the size: line spacing of a block of one line
FUZZ = 0.02                  # of the size: measurements this close are the same
CENTRED = 0.02               # of the page's width: a line this near its middle is centred
_ID = re.compile(r"p([1-9]\d{0,5})-b([1-9]\d{0,5})")
_LANGUAGE = re.compile(r"[A-Za-z]{2,3}(-[A-Za-z0-9]{1,8}){0,4}")
_LISTED = re.compile(r"(\(?\d{1,3}[.)]|\(?[a-zA-Z][.)])\s")
_BOLD = ("bold", "black", "heavy", "semibold", "demi")
# Scripts whose letters must be shaped to be drawn (Indic, Thai, Lao, Tibetan,
# Myanmar, Khmer, Mongolian): drawing their characters one by one would misspell them.
_SHAPED = ((0x0900, 0x0DFF), (0x0E00, 0x0FFF), (0x1000, 0x109F), (0x1780, 0x18AF))
# Characters PDFium reads that aren't what they show: its mark for a soft hyphen, and
# the Symbol font's bullets.
_SYMBOLS = {0x02: "\u00ad", 0xF0B7: "\u2022", 0xF0A7: "\u25aa"}
_UNPRINTABLE = ("Cc", "Cf", "Co", "Cn", "Cs")
SCRIPT = (0.4, 0.6)          # of the size: how far below and above its line a smaller
#                              character may sit and be part of it (a footnote mark)
_INVISIBLE = 3               # text render mode: neither filled nor stroked


class _Line:
    """Characters along one baseline, in the frame of their direction (see _frame)."""

    __slots__ = ("key", "matrix", "y", "x0", "x1", "size", "colour", "bold", "parts",
                 "chars", "boxes", "fonts", "first_word", "spaced", "text", "last",
                 "square", "stretch")

    def __init__(self, key, frame: tuple, size: float, stretch: float, colour: tuple,
                 x0: float, x1: float, y: float, bold: bool) -> None:
        self.matrix, self.size, self.stretch, self.colour = frame, size, stretch, colour
        self.key, self.y, self.x0, self.x1, self.bold = key, y, x0, x1, bold
        self.last = x0           # its last character's origin
        self.square = _square(self.matrix)
        self.parts: list[str] = []
        self.chars: list[int] = []
        self.boxes: list[tuple] = []
        self.fonts: dict[tuple, int] = {}
        self.first_word = x1     # where its first word ends
        self.spaced = False
        self.text = ""

    def finish(self) -> None:
        """Its text as read: a soft hyphen ending it shown as a hyphen, and characters
        that show nothing left out."""
        while self.parts and self.parts[-1] == " ":
            self.parts.pop()
        text = "".join(self.parts)
        if text.endswith("\u00ad"):
            text = text[:-1] + "-"
        self.text = "".join(c for c in text if unicodedata.category(c) not in _UNPRINTABLE)


class _Block:
    """Lines set as one piece. Positions are in its text space (the space of its first
    character's matrix, from the page's origin): x along the baseline, y across it.
    Duck-types edit_text's changes (page, areas, matrix, shift) for edittext._remove."""

    __slots__ = ("id", "page", "key", "matrix", "size", "colour", "bold", "lines",
                 "breaks", "leading", "x0", "x1", "font_name", "flags", "weight", "areas",
                 "shift", "text", "align", "left", "right", "font", "scale", "placed",
                 "drawn_size", "number", "wraps", "stretch", "length")

    def __init__(self, line: _Line, number: int) -> None:
        self.number, self.page, self.id = number, -1, ""
        self.key, self.matrix, self.size = line.key, line.matrix, line.size
        self.stretch = line.stretch
        self.colour, self.bold = line.colour, line.bold
        self.lines = [line]
        self.breaks: list[bool] = []    # after each line but the last: a deliberate break
        self.x0, self.x1 = line.x0, line.x1
        self.length = len(line.text)    # characters, as its lines read
        self.shift = 0.0

    def takes(self, line: _Line) -> bool:
        """Whether *line* continues this block."""
        last = self.lines[-1]
        if line.key != self.key or line.colour != self.colour or line.bold != self.bold \
                or abs(line.size - self.size) > SAME_SIZE * self.size:
            return False
        if _LISTED.match(line.text) or line.text[:1] in BULLETS:
            return False
        size, gap = self.size, last.y - line.y
        if not 0.5 * size <= gap <= LEADING * size:
            return False
        if len(self.lines) > 1 and abs(gap - self.leading) > LEADING_DRIFT * size:
            return False
        if line.x0 > self.x1 or line.x1 < self.x0:
            return False
        if line.x0 < self.x0 - INDENT * size and \
                abs(line.x1 - last.x1) > ALIGNED * size and \
                abs(line.x0 + line.x1 - last.x0 - last.x1) / 2 > ALIGNED * size:
            return False   # starting further left, not aligned on the right or middle
        return self.length + len(line.text) <= MAX_BLOCK_TEXT

    def add(self, line: _Line) -> None:
        if len(self.lines) == 1:
            self.leading = self.lines[0].y - line.y
        self.lines.append(line)
        self.length += len(line.text)
        self.x0, self.x1 = min(self.x0, line.x0), max(self.x1, line.x1)

    def finish(self) -> None:
        """Its text, alignment, font and areas, once every line is in."""
        lines = self.lines
        if len(lines) == 1:
            self.leading = SINGLE_LEADING * self.size
        self.breaks = [not self._continues(a, b) for a, b in zip(lines, lines[1:],
                                                                 strict=False)]
        self.wraps = not all(self.breaks)
        pieces, run = [], []
        for n, line in enumerate(lines):
            run.append(line.text)
            if n == len(lines) - 1 or self.breaks[n]:
                pieces.append(join_lines(run))
                run = []
        self.text = "\n".join(pieces)
        tally: dict[tuple, int] = {}
        for line in lines:
            for font, count in line.fonts.items():
                tally[font] = tally.get(font, 0) + count
        self.font_name, self.flags, self.weight = max(tally, key=tally.get)
        self.areas = [area for line in lines for area in _lines(line.boxes)]
        self.align = "left"
        if len(lines) > 1:
            size = ALIGNED * self.size
            lefts = [line.x0 for line in lines]
            rights = [line.x1 for line in lines]
            middles = [(line.x0 + line.x1) / 2 for line in lines]
            if max(lefts) - min(lefts) <= size:
                self.align = "left"
            elif max(rights) - min(rights) <= size:
                self.align = "right"
            elif max(middles) - min(middles) <= size:
                self.align = "center"

    def _continues(self, line: _Line, after: _Line) -> bool:
        """Whether *after* goes on with *line*'s sentence (the break between them is
        only where the text wrapped): it starts in lower case, or *line* ends in a
        hyphen or comma; or the block is prose (wide) and *line* was full, the next
        line's first word not fitting on it."""
        text, following = line.text, after.text
        if following[:1].islower() or text[-1:] in "-,":
            return True
        width, size = self.x1 - self.x0, self.size
        return width >= PROSE * size and (line.x1 - line.x0) + SPACE * size + \
            (after.first_word - after.x0) >= width - BREAK * size

    @property
    def top(self) -> float:
        return self.lines[0].y + ASCENT * self.size

    @property
    def bottom(self) -> float:
        return self.lines[-1].y - DESCENT * self.size

    def chars(self) -> set[int]:
        return {i for line in self.lines for i in line.chars}


class _Page:
    """A page's blocks (every one: those listed, with letters, have ids), and its text
    by band (per text space: baseline, start, end, block number) for finding room."""

    def __init__(self, index: int, deadline: float) -> None:
        self.index = index
        self.budget = Budget(deadline, action="Translating")
        self.blocks: list[_Block] = []
        self.listed: list[_Block] = []
        self.bands: dict[tuple, tuple[list, list]] = {}
        self.visible: tuple = (0.0, 0.0, 0.0, 0.0)
        self.has_text = False
        self.invisible = False
        self.text_length = 0
        self.largest: dict[tuple, float] = {}  # by direction: the largest text's size
        self.things: list[tuple] | None = []   # page boxes of pictures and drawings
        self._framed: dict[tuple, _Rows | None] = {}

    def obstacles(self, key: tuple, matrix: tuple) -> _Rows | None:
        """The pictures and drawings as boxes in the frame of direction *key*, by row;
        None if the page has too many of them, or of directions, to tell where."""
        if self.things is None:
            return None
        if key not in self._framed:
            self._framed[key] = None if len(self._framed) >= MAX_FRAMES else _Rows(
                (_framed(matrix, box) for box in self.things), self.budget)
        return self._framed[key]


class _Rows:
    """Boxes (x0, y0, x1, y1) filed by the rows ROW high that they cross, to find those
    near a height without looking at all of them; taller ones are kept apart."""

    ROW = 24.0
    MAX_ROWS = 64

    def __init__(self, boxes, budget: Budget) -> None:
        self.rows: dict[int, list] = {}
        self.tall: list = []
        for n, box in enumerate(boxes):
            if not n & 0x3FF:
                budget.check()
            low, high = math.floor(box[1] / self.ROW), math.floor(box[3] / self.ROW)
            if high - low > self.MAX_ROWS:
                self.tall.append(box)
                continue
            for row in range(low, high + 1):
                self.rows.setdefault(row, []).append(box)

    def between(self, low: float, high: float) -> list:
        """The boxes in the rows from *low* to *high* (some more than once), and the
        tall ones."""
        found = list(self.tall)
        for row in range(math.floor(low / self.ROW), math.floor(high / self.ROW) + 1):
            found += self.rows.get(row, ())
        return found


class _Cells:
    """Boxes, each with what it belongs to, filed by the square cells they cross, to
    find those at a point or near a box quickly; very large ones are kept apart."""

    CELL = 48.0
    MAX_CELLS = 64

    def __init__(self) -> None:
        self.cells: dict[tuple, list] = {}
        self.large: list = []

    def _span(self, box) -> tuple[range, range] | None:
        x0, y0, x1, y1 = (math.floor(v / self.CELL) for v in box)
        if x1 - x0 > self.MAX_CELLS or y1 - y0 > self.MAX_CELLS:
            return None
        return range(x0, x1 + 1), range(y0, y1 + 1)

    def add(self, box: tuple, owner) -> None:
        if not all(math.isfinite(v) for v in box):
            return
        span = self._span(box)
        if span is None:
            self.large.append((box, owner))
            return
        for cx in span[0]:
            for cy in span[1]:
                self.cells.setdefault((cx, cy), []).append((box, owner))

    def near(self, box: tuple) -> list:
        """(box, owner) for the boxes in the cells *box* crosses (some more than once)."""
        span = self._span(box)
        if span is None:
            return [item for items in self.cells.values() for item in items] + self.large
        return [item for cx in span[0] for cy in span[1]
                for item in self.cells.get((cx, cy), ())] + self.large

    def at(self, x: float, y: float) -> list:
        """(box, owner) for the boxes holding point (x, y)."""
        if not (math.isfinite(x) and math.isfinite(y)):
            return []
        cell = self.cells.get((math.floor(x / self.CELL), math.floor(y / self.CELL)), ())
        return [(box, owner) for box, owner in (*cell, *self.large)
                if box[0] <= x <= box[2] and box[1] <= y <= box[3]]


def _invisible(raw, i: int) -> bool:
    """Whether character *i* is drawn neither filled nor stroked (a scan's OCR layer).
    PDFium before 6600 or so can't say; then the removal's own check refuses it."""
    if not hasattr(pdfium_raw, "FPDFText_GetTextObject"):
        return False
    obj = pdfium_raw.FPDFText_GetTextObject(raw, i)
    return bool(obj) and pdfium_raw.FPDFTextObj_GetTextRenderMode(obj) == _INVISIBLE


def _frame(matrix: tuple, size: float) -> tuple | None:
    """A character's direction, as (key, frame, size, stretch): the key names its
    direction (text sized by its matrix or by its font size alike); the frame is that
    direction as a rotation (flipped if the text is mirrored), so positions in it are
    in points; size is its height in points, stretch how much wider than that it is
    drawn (horizontal scaling). None if it is flattened to nothing."""
    a, b, c, d = matrix
    det, along = a * d - b * c, math.hypot(a, b)
    if abs(det) < 1e-9 or along < 1e-9 or not size > 0:
        return None
    flip = 1.0 if det > 0 else -1.0
    ca, sa = a / along, b / along
    across = abs(det) / along          # the height of the text space's unit, upright
    return ((round(ca, 3), round(sa, 3), flip), (ca, sa, -flip * sa, flip * ca),
            size * across, along / across)


def _square(matrix: tuple) -> bool:
    """Whether text space *matrix* runs along or across the page (at a right angle),
    where PDFium's boxes fit its characters."""
    a, b, c, d = matrix
    return (abs(b) <= 1e-3 * abs(a) and abs(c) <= 1e-3 * abs(d)) or \
        (abs(a) <= 1e-3 * abs(b) and abs(d) <= 1e-3 * abs(c))


def _on(line: _Line, y: float, size: float) -> bool:
    """Whether a character at baseline *y* and *size* is on *line*: the same size on
    the same baseline, or smaller, raised or lowered within it (a footnote mark)."""
    if abs(size - line.size) <= SAME_SIZE * line.size:
        return abs(y - line.y) <= LINE * line.size
    return size < line.size and \
        line.y - SCRIPT[0] * line.size <= y <= line.y + SCRIPT[1] * line.size


def _framed(matrix: tuple, box: tuple) -> tuple[float, float, float, float]:
    """A page box as a box in text space *matrix*: (x0, y0, x1, y1)."""
    points = [_turned(matrix, px, py) for px in (box[0], box[2]) for py in (box[1], box[3])]
    return (min(p[0] for p in points), min(p[1] for p in points),
            max(p[0] for p in points), max(p[1] for p in points))


def _extent(matrix: tuple, box: tuple) -> tuple[float, float]:
    """A page box's start and end along the baseline of text space *matrix*."""
    framed = _framed(matrix, box)
    return framed[0], framed[2]


def _letters(text: str) -> bool:
    return any(unicodedata.category(c).startswith("L") for c in text)


def _read_page(doc, index: int, visible: tuple, deadline: float,
               obstacles: bool = False) -> _Page:
    """The blocks of page *index* of PDFium document *doc*; with *obstacles*, where its
    pictures and drawings are too."""
    page = _Page(index, deadline)
    page.visible = visible
    lines: list[_Line] = []
    with _PDFIUM_LOCK:
        pdf_page = doc[index]
        try:
            textpage = pdf_page.get_textpage()
            try:
                raw = textpage.raw
                chars = _chars(textpage, union=True)
                current: _Line | None = None
                space = False
                for i, (u, box, generated) in enumerate(chars):
                    if not i & 0x3FF and time.monotonic() > deadline:
                        raise PdfUserError(f"Reading the text took longer than "
                                           f"{TRANSLATE_TIME_LIMIT_S} s. Try fewer pages")
                    text = chr(u) if u < 0x110000 else "�"
                    if generated:   # a space or line break PDFium saw: where text goes
                        space = True   # on is told by where it is (a raised mark isn't
                        continue       # a new line)
                    placed = box[2] > box[0] and box[3] > box[1]
                    if text.isspace():
                        space = True
                        if current is not None and placed:
                            current.chars.append(i)
                            current.boxes.append(box)
                        continue
                    page.has_text = True
                    text = _SYMBOLS.get(u, text)
                    middle = ((box[0] + box[2]) / 2, (box[1] + box[3]) / 2)
                    if not placed or not (visible[0] <= middle[0] <= visible[2] and
                                          visible[1] <= middle[1] <= visible[3]):
                        current = None    # off the page: no block's
                        continue
                    if _invisible(raw, i):
                        page.invisible, current = True, None
                        continue
                    style = _style(raw, i)
                    origin, colour = style[0], style[3]
                    if colour[3] == 0:    # drawn fully transparent: invisible
                        page.invisible, current = True, None
                        continue
                    found = _frame(style[1], style[2])
                    if found is None:
                        current = None    # flattened to nothing
                        continue
                    key, matrix, size, stretch = found
                    x, y = _turned(matrix, *origin)
                    x0, x1 = _extent(matrix, box)
                    if not _square(matrix):   # the box, turned back, is far too wide:
                        x0, x1 = x, x + min(x1 - x0, size)   # from its origin, an em at most
                    nearby = current is not None and current.key == key and \
                        current.colour == colour and _on(current, y, size)
                    if nearby and current.square:
                        nearby = current.x1 - 0.5 * current.size <= x0 <= \
                            current.x1 + SPLIT * current.size
                        space = space or x0 - current.x1 > SPACE * current.size
                    elif nearby:   # going on along the line from the last character
                        nearby = current.last < x <= current.last + SPLIT * current.size
                    font = style[4:]
                    bold = any(k in font[0].lower() for k in _BOLD) or font[2] >= 600
                    if nearby:
                        if space and current.parts[-1] != " ":
                            current.parts.append(" ")
                            current.spaced = True
                        current.x1 = max(current.x1, x1)
                    else:
                        current = _Line(key, matrix, size, stretch, colour, x0, x1, y,
                                        bold)
                        lines.append(current)
                    current.parts.append(text)
                    current.last = x
                    current.chars.append(i)
                    current.boxes.append(box)
                    current.fonts[font] = current.fonts.get(font, 0) + 1
                    if not current.spaced:
                        current.first_word = current.x1
                    space = False
                    page.text_length += 1
                    if page.text_length > MAX_PAGE_TEXT:
                        raise PdfUserError(f"Page {index + 1} has more than "
                                           f"{MAX_PAGE_TEXT:,} characters of text; it "
                                           "can't be translated")
            finally:
                textpage.close()
            if obstacles:
                page.things = _things(pdf_page.raw, page.budget)
        finally:
            pdf_page.close()
    _gather(page, lines)
    return page


def _things(raw, budget: Budget) -> list[tuple] | None:
    """Page boxes of a page's objects other than text (pictures, drawings, forms), a
    path's by its parts (one path may draw many boxes, or a table's lines); None if it
    has more than MAX_OBJECTS objects, or its paths more than MAX_SEGMENTS parts."""
    count = pdfium_raw.FPDFPage_CountObjects(raw)
    if count > MAX_OBJECTS:
        return None
    found: list[tuple] = []
    segments = [MAX_SEGMENTS]
    edges = [ctypes.c_float() for _ in range(4)]
    for k in range(count):
        if not k & 0xFF:
            budget.check()
        obj = pdfium_raw.FPDFPage_GetObject(raw, k)
        kind = pdfium_raw.FPDFPageObj_GetType(obj)
        if kind == pdfium_raw.FPDF_PAGEOBJ_TEXT:
            continue
        if kind == pdfium_raw.FPDF_PAGEOBJ_PATH:
            parts = _parts(obj, segments, budget)
            if parts is None:
                return None
            if parts:
                found += parts
                continue
        if pdfium_raw.FPDFPageObj_GetBounds(obj, *edges):
            box = tuple(e.value for e in edges)
            if all(math.isfinite(v) for v in box):
                found.append(box)
    return found


def _parts(obj, segments: list[int], budget: Budget) -> list[tuple] | None:
    """Page boxes of a path's parts (each from a move to the next); [] if they can't be
    read (the path's bounds stand for it then); None past the *segments* left."""
    count = pdfium_raw.FPDFPath_CountSegments(obj)
    segments[0] -= max(count, 0)
    if segments[0] < 0:
        return None
    m = pdfium_raw.FS_MATRIX()
    if count <= 0 or not pdfium_raw.FPDFPageObj_GetMatrix(obj, m):
        return []
    parts, points = [], []
    x, y = ctypes.c_float(), ctypes.c_float()
    for n in range(count):
        if not n & 0xFFF:
            budget.check()
        segment = pdfium_raw.FPDFPath_GetPathSegment(obj, n)
        if not segment or not pdfium_raw.FPDFPathSegment_GetPoint(segment, x, y):
            return []
        if pdfium_raw.FPDFPathSegment_GetType(segment) == pdfium_raw.FPDF_SEGMENT_MOVETO \
                and points:
            parts.append(points)
            points = []
        points.append((m.a * x.value + m.c * y.value + m.e,
                       m.b * x.value + m.d * y.value + m.f))
    parts.append(points)
    boxes = []
    for points in parts:
        xs, ys = [p[0] for p in points], [p[1] for p in points]
        box = (min(xs), min(ys), max(xs), max(ys))
        if len(points) > 1 and all(math.isfinite(v) for v in box):
            boxes.append(box)
    return boxes


def _gather(page: _Page, lines: list[_Line]) -> None:
    """Join the page's lines into blocks, give the listed ones ids, and file the page's
    text by band."""
    for n, line in enumerate(lines):
        if not n & 0xFF:
            page.budget.check()
        line.finish()
        page.largest[line.key] = max(page.largest.get(line.key, 0.0), line.size)
        target = next((b for b in reversed(page.blocks[-4:]) if b.takes(line)), None)
        if target is None:
            page.blocks.append(_Block(line, len(page.blocks)))
        else:
            target.add(line)
    for block in page.blocks:
        block.page = page.index
        block.finish()
        if _letters(block.text):
            page.listed.append(block)
            block.id = f"p{page.index + 1}-b{len(page.listed)}"
        for line in block.lines:
            heights, found = page.bands.setdefault(block.key, ([], []))
            found.append((line.y, line.x0, line.x1, block.number))
    for heights, found in page.bands.values():
        found.sort()
        heights.extend(f[0] for f in found)


# --- get_text_blocks ---------------------------------------------------------------------


def _pages(pdf: pikepdf.Pdf, pages: str | None) -> list[int]:
    total = len(pdf.pages)
    targets = parse_pages(pages, total, dedupe=True) if pages else list(range(total))
    sizes: dict = {}
    too_big = [i + 1 for i in targets
               if (_content_size(pdf.pages[i], MAX_PAGE_CONTENT, sizes) or
                   MAX_PAGE_CONTENT + 1) > MAX_PAGE_CONTENT]
    if too_big:
        listed = ", ".join(map(str, too_big[:20])) + (" …" if len(too_big) > 20 else "")
        raise PdfUserError(f"Pages {listed} have too much content to read safely; leave "
                           "them out with pages")
    return targets


def get_text_blocks(path: Path, settings: Settings, pages: str | None = None,
                    password: str | None = None) -> TextBlocksResult:
    """The text of *pages* (default all) as blocks to translate, in reading order, as
    many pages as MAX_REPLY_TEXT allows (next_page says where the rest start)."""
    deadline = time.monotonic() + TRANSLATE_TIME_LIMIT_S
    data = path.read_bytes()
    with ExitStack() as stack:
        pdf = stack.enter_context(open_pdf(data, settings, password))
        require_copy_permission(pdf, "reading the text to translate")
        targets = _pages(pdf, pages)
        visible = {i: _visible_box(pdf.pages[i]) for i in targets}
        page_count = len(pdf.pages)
        doc = _open(_plain(pdf))
        stack.callback(_close, doc)
        blocks, read, without, invisible = [], [], [], []
        total, next_page = 0, None
        for index in targets:
            page = _read_page(doc, index, visible[index], deadline)
            length = sum(len(b.text) for b in page.listed)
            if read and total + length > MAX_REPLY_TEXT:
                next_page = index + 1
                break
            total += length
            read.append(index + 1)
            if not page.has_text:
                without.append(index + 1)
            if page.invisible:
                invisible.append(index + 1)
            blocks += [{"id": b.id, "page": index + 1, "text": b.text, "lines": len(b.lines)}
                       for b in page.listed]
    return {"digest": hashlib.sha256(data).hexdigest(), "page_count": page_count,
            "pages_read": read, "blocks": blocks, "next_page": next_page,
            "pages_without_text": without, "pages_with_invisible_text": invisible}


# --- translate_pdf: checking what was given --------------------------------------------


def _translation(text, block: str) -> str:
    """A translation as it will be drawn: line breaks kept, no control characters."""
    if not isinstance(text, str):
        raise PdfUserError(f"Block {block}'s text must be text")
    if len(text) > MAX_TRANSLATION:
        raise PdfUserError(f"Block {block}'s text must be at most {MAX_TRANSLATION:,} "
                           "characters")
    text = unicodedata.normalize("NFC", text.replace("\r\n", "\n").replace("\r", "\n"))
    # Every kind of space as a space, every line or paragraph separator as a line break.
    text = "".join(" " if unicodedata.category(c) == "Zs" or c == "\t" else
                   "\n" if unicodedata.category(c) in ("Zl", "Zp") else c for c in text)
    text = "\n".join(re.sub(r"[ \t]+", " ", line).strip() for line in text.split("\n"))
    text = text.strip("\n")
    if not text.strip():
        raise PdfUserError(f"Block {block}'s text is empty; leave a block out to keep "
                           "it as it is")
    if any(unicodedata.category(c) in ("Cc", "Cf", "Cs", "Co", "Cn") and c != "\n"
           for c in text):
        raise PdfUserError(f"Block {block}'s text contains control or invalid characters")
    if any(ord(c) > 0xFFFF for c in text):
        raise PdfUserError(f"Block {block}'s text can't contain emoji or other characters "
                           "beyond U+FFFF")
    if any(low <= ord(c) <= high for c in text for low, high in _SHAPED):
        raise PdfUserError("translate_pdf can't lay out scripts whose letters join or "
                           f"reorder (such as Hindi or Thai), as block {block}'s do. "
                           "Nothing was written")
    if any(unicodedata.bidirectional(c) in ("R", "AL") for c in text):
        raise PdfUserError("translate_pdf can't lay out right-to-left text (such as Arabic "
                           f"or Hebrew), as block {block}'s is. Nothing was written")
    return text


def _check(translations) -> dict[str, str]:
    """The translations as {block id: text}."""
    if not isinstance(translations, list) or not 1 <= len(translations) <= MAX_TRANSLATIONS:
        raise PdfUserError(f"Give 1 to {MAX_TRANSLATIONS:,} translations")
    found: dict[str, str] = {}
    for n, item in enumerate(translations, 1):
        if not isinstance(item, dict) or set(item) != {"block", "text"} or \
                not isinstance(item["block"], str):
            raise PdfUserError(f"Translation {n} must be {{\"block\": ..., \"text\": ...}}")
        block = item["block"]
        if not _ID.fullmatch(block):
            raise PdfUserError(f"Translation {n}'s block must be an id from "
                               "get_text_blocks, like \"p1-b2\"")
        if block in found:
            raise PdfUserError(f"Block {block} is translated twice; give it once")
        found[block] = _translation(item["text"], block)
    if sum(map(len, found.values())) > MAX_TRANSLATED:
        raise PdfUserError(f"The translations come to more than {MAX_TRANSLATED:,} "
                           "characters; translate fewer pages at a time")
    return found


def _language(language) -> str | None:
    if language is None:
        return None
    if not isinstance(language, str) or not _LANGUAGE.fullmatch(language):
        raise PdfUserError("language must be a language tag, like \"de\", \"fr\" or "
                           "\"pt-BR\"")
    return language


# --- fitting ----------------------------------------------------------------------------


def _wrap(text: str, font: str, size: float, widths: list[float], rest: float,
          stretch: float = 1.0) -> list[tuple[str, float]] | None:
    """*text* as lines (with their widths) no wider than *widths* (one each) and *rest*
    for those after, breaking at spaces and at its own line breaks; None if a word is
    wider than its line. *stretch* widens every character (horizontal scaling)."""
    lines: list[tuple[str, float]] = []
    for paragraph in text.split("\n"):
        line, drawn = "", 0.0
        for word in paragraph.split(" "):
            width = widths[len(lines)] if len(lines) < len(widths) else rest
            trial = word if not line else f"{line} {word}"
            wide = pdfmetrics.stringWidth(trial, font, size) * stretch
            if wide <= width:
                line, drawn = trial, wide
                continue
            if not line:
                return None
            lines.append((line, drawn))
            line, drawn = word, pdfmetrics.stringWidth(word, font, size) * stretch
            if drawn > (widths[len(lines)] if len(lines) < len(widths) else rest):
                return None
        lines.append((line, drawn))
    return lines


def _room(block: _Block, page: _Page, translated: set[int]) -> tuple[float, float]:
    """How far the lines of a block that doesn't wrap may run back and on: to the
    page's edge, or other text beside any of them (half way to text that may run on
    too)."""
    a, b = block.matrix[0], block.matrix[1]
    margin = EDGE / math.hypot(a, b)
    edge_left, edge_right = _extent(block.matrix, page.visible)
    left, right = edge_left + margin, edge_right - margin
    size = block.size
    heights, found = page.bands[block.key]
    low = bisect.bisect_left(heights, block.lines[-1].y - NEIGHBOUR * size)
    high = bisect.bisect_right(heights, block.lines[0].y + NEIGHBOUR * size)
    for n, (_, x0, x1, owner) in enumerate(found[low:high]):
        if not n & 0x3FF:
            page.budget.check()
        if owner == block.number:
            continue
        halfway = owner in translated and not page.blocks[owner].wraps
        if x0 >= block.x1 - LINE * size:
            right = min(right, block.x1 + (x0 - block.x1) / 2 if halfway
                        else x0 - GAP * size)
        elif x1 <= block.x0 + LINE * size:
            left = max(left, block.x0 - (block.x0 - x1) / 2 if halfway
                       else x1 + GAP * size)
        else:   # text across it
            left, right = max(left, block.x0), min(right, block.x1)
    rows = page.obstacles(block.key, block.matrix)
    if rows is None:
        return block.x0, block.x1
    for n, (x0, y0, x1, y1) in enumerate(rows.between(block.bottom, block.top)):
        if not n & 0x3FF:
            page.budget.check()
        if y1 <= block.bottom or y0 >= block.top or _behind(block, (x0, y0, x1, y1)):
            continue
        if x0 >= block.x1 - LINE * size:
            right = min(right, x0 - GAP * size)
        elif x1 <= block.x0 + LINE * size:
            left = max(left, x1 + GAP * size)
        else:   # a picture or drawing across it
            left, right = max(left, block.x0), min(right, block.x1)
    return min(left, block.x0), max(right, block.x1)


def _behind(block: _Block, box: tuple) -> bool:
    """Whether a picture or drawing (a box in the block's text space) holds the whole
    block: a background, or a frame around it."""
    fuzz = FUZZ * block.size
    return box[0] <= block.x0 + fuzz and box[1] <= block.bottom + fuzz and \
        box[2] >= block.x1 - fuzz and box[3] >= block.top - fuzz


def _annotation_boxes(page: pikepdf.Page) -> list[tuple]:
    """Where a page's form fields, comments and other annotations show (not links,
    which show nothing)."""
    annots = page.obj.get("/Annots")
    found = []
    for annot in islice(annots, MAX_OBJECTS) if isinstance(annots, pikepdf.Array) else ():
        if not isinstance(annot, pikepdf.Dictionary) or \
                annot.get("/Subtype") in (pikepdf.Name.Link, pikepdf.Name.Popup):
            continue
        rect = _rect(annot.get("/Rect"))
        if rect is not None:
            found.append(rect)
    return found


def _next_beside(block: _Block, line: _Line, page: _Page) -> float:
    """How far *line* may run on when other text follows it on its band: only a little
    past where it ended, as what lies between is unknown (a box, a blank to fill in);
    infinite if nothing follows it."""
    size = block.size
    heights, found = page.bands[block.key]
    low = bisect.bisect_left(heights, line.y - NEIGHBOUR * size)
    high = bisect.bisect_right(heights, line.y + NEIGHBOUR * size)
    after = min((x0 - GAP * size for _, x0, _, owner in found[low:high]
                 if owner != block.number and x0 >= line.x1 - LINE * size), default=math.inf)
    return after if after == math.inf else min(after, line.x1 + GAP * size)


def _floor(block: _Block, page: _Page, left: float, right: float) -> float:
    """How far down a block's text may reach, between *left* and *right* in its frame:
    to the first text, picture, drawing or form field below it (that doesn't hold the
    whole block, like a background), keeping the gap there was before it (up to a
    line's spacing); or to the page's edge."""
    size, budget = block.size, page.budget
    c, d = block.matrix[2:]
    edge = _framed(block.matrix, page.visible)[1] + EDGE / math.hypot(c, d)
    rows = page.obstacles(block.key, block.matrix)
    if rows is None:
        return block.bottom
    top = -math.inf
    # Text below, nearest first: none further down can reach higher than the largest
    # text in this direction.
    heights, found = page.bands[block.key]
    reach = ASCENT * page.largest[block.key]
    for n in range(bisect.bisect_left(heights, block.lines[-1].y - LINE * size) - 1, -1, -1):
        if not n & 0x3FF:
            budget.check()
        y, x0, x1, owner = found[n]
        if y + reach < top:
            break
        if owner != block.number and x1 > left and x0 < right:
            top = max(top, y + ASCENT * page.blocks[owner].size)

    def lower(box: tuple) -> None:
        nonlocal top
        x0, y0, x1, y1 = box
        if x1 > left and x0 < right and y0 < block.bottom and not _behind(block, box):
            top = max(top, y1)
    for box in rows.tall:
        lower(box)
    # Pictures and drawings below, row by row down: once one reaches into a row, those
    # filed only lower down can't reach as high.
    row = math.floor(block.bottom / _Rows.ROW)
    while row >= math.floor(edge / _Rows.ROW) and top < (row + 1) * _Rows.ROW:
        budget.check()
        for box in rows.rows.get(row, ()):
            lower(box)
        row -= 1
    if top == -math.inf:
        return min(edge, block.bottom)
    if top >= block.bottom:
        return block.bottom
    gap = max(GAP * size, min(block.bottom - top, block.leading))
    return max(edge, min(top + gap, block.bottom))


def _fit(block: _Block, text: str, page: _Page, translated: set[int]) -> bool:
    """Lay *text* out in *block*'s place (block.placed: (x, baseline, line) in its text
    space); False if it won't fit even at MIN_SHRINK of its size."""
    try:
        block.font = _font_for(block.font_name, block.flags, block.weight,
                               text.replace("\n", " "))
    except PdfUserError:
        raise PdfUserError(f"Block {block.id}'s translation has characters no available "
                           "font can show (translate_pdf can draw Latin, Greek and "
                           "Cyrillic text). Nothing was written") from None
    middle = (block.x0 + block.x1) / 2
    align = block.align
    width = block.x1 - block.x0
    left, right = block.x0, block.x1
    if not block.wraps:
        left, right = _room(block, page, translated)
        visible = _extent(block.matrix, page.visible)
        if len(block.lines) == 1 and abs(middle - (visible[0] + visible[1]) / 2) <= \
                CENTRED * (visible[1] - visible[0]):
            align = "center"
        width = {"left": right - block.x0, "right": block.x1 - left,
                 "center": 2 * min(middle - left, right - middle)}[align]
    floor = _floor(block, page, left, right)
    # Left-aligned lines start where the block's did: its first line may be indented,
    # or hang out before the rest (a list item).
    first_x = block.lines[0].x0
    rest_x = min(line.x0 for line in block.lines[1:]) if len(block.lines) > 1 else first_x
    if align != "left":
        first_x = rest_x = block.x0
    end = block.x0 + width
    # A wrapped block's lines end where the text did, before other text beside them
    # (a paragraph starting or ending part way along a line); lines it gains, at its end.
    ends = [min(end, _next_beside(block, line, page)) for line in block.lines] \
        if block.wraps else []
    steps = round((1 - MIN_SHRINK) / SHRINK_STEP)
    for step in range(steps + 1):
        scale = 1 - step * SHRINK_STEP
        size = block.size * scale
        fuzz = FUZZ * size
        lines = _wrap(text, block.font, size,
                      [e - (rest_x if n else first_x) + fuzz for n, e in enumerate(ends)],
                      end - rest_x + fuzz, block.stretch) if ends else \
            _wrap(text, block.font, size, [width - (first_x - block.x0) + fuzz],
                  width - (rest_x - block.x0) + fuzz, block.stretch)
        if lines is None:
            continue
        first = block.top - ASCENT * size
        last = first - (len(lines) - 1) * block.leading * scale
        if last - DESCENT * size < floor - FUZZ * block.size:
            continue
        block.scale, block.drawn_size = scale, size
        block.placed = []
        for n, (line, drawn) in enumerate(lines):
            x = {"left": rest_x if n else first_x, "right": block.x1 - drawn,
                 "center": middle - drawn / 2}[align]
            block.placed.append((x, first - n * block.leading * scale, line, drawn))
        return True
    return False


# --- drawing ----------------------------------------------------------------------------


def _drawings(pdf: pikepdf.Pdf, blocks: list[_Block]
              ) -> tuple[pikepdf.Pdf, dict[int, pikepdf.Stream], dict[int, bytes]]:
    """A form drawing each block's translation (by its place in *blocks*), and the
    content each form gets once the removal is checked (they are empty until then).
    Returns the Pdf the forms came from too: keep it open until saved."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    for block in blocks:
        r, g, b, alpha = block.colour
        c.setPageSize((100, 100))   # each form's box is set to its page's below
        c.saveState()
        c.transform(*block.matrix, 0, 0)
        c.setFillColorRGB(r / 255, g / 255, b / 255)
        c.setFillAlpha(alpha / 255)
        for x, y, line, _ in block.placed:
            text = c.beginText(x, y)
            text.setFont(block.font, block.drawn_size)
            text.setHorizScale(block.stretch * 100)
            text.textOut(line)
            c.drawText(text)
        c.restoreState()
        c.showPage()
    c.save()
    source = pikepdf.open(buf)
    forms, content = {}, {}
    reset = b"0 Tc 0 Tw 100 Tz 0 Ts 0 Tr\n"   # a form inherits the page's text state
    for n, block in enumerate(blocks):
        forms[n] = pdf.copy_foreign(source.pages[n].as_form_xobject())
        content[n] = reset + forms[n].read_bytes()
        forms[n].BBox = pikepdf.Array([float(v) for v in pdf.pages[block.page].mediabox])
        forms[n].write(b"")
    return source, forms, content


def _clashes(page: _Page, blocks: list[_Block]) -> None:
    """Refuse translations that would be drawn over each other, or over text left as it
    is in the same direction (text in another direction, like a watermark, may cross
    them, as it crossed the original)."""
    translated = {b.number for b in blocks}
    cells: dict[tuple, _Cells] = {}
    for block in page.blocks:
        if block.number not in translated:
            for line in block.lines:
                cells.setdefault(block.key, _Cells()).add(
                    (line.x0, line.y - DESCENT * block.size, line.x1,
                     line.y + ASCENT * block.size), block)
    drawn = []
    for block in blocks:
        size = block.drawn_size
        for x, y, _, wide in block.placed:
            box = (x, y - DESCENT * size, x + wide, y + ASCENT * size)
            cells.setdefault(block.key, _Cells()).add(box, block)
            drawn.append((box, block))
    looked = 0
    for (x0, y0, x1, y1), block in drawn:
        for (ox0, oy0, ox1, oy1), other in cells[block.key].near((x0, y0, x1, y1)):
            looked += 1
            if not looked & 0x3FF:
                page.budget.check()
            fuzz = FUZZ * max(block.size, other.size) * 5
            if other is not block and ox0 < x1 - fuzz and ox1 > x0 + fuzz and \
                    oy0 < y1 - fuzz and oy1 > y0 + fuzz:
                raise PdfUserError(f"Block {block.id}'s translation would run into other "
                                   f"text on page {page.index + 1}; shorten it. Nothing "
                                   "was written")


def _drawn_areas(block: _Block) -> list[tuple]:
    """Page boxes around each line *block*'s translation is drawn on."""
    a, b, c, d = block.matrix
    size = block.drawn_size
    found = []
    for x, y, _, drawn in block.placed:
        corners = [(a * px + c * py, b * px + d * py)
                   for px in (x, x + drawn) for py in (y - DESCENT * size, y + ASCENT * size)]
        found.append((min(p[0] for p in corners), min(p[1] for p in corners),
                      max(p[0] for p in corners), max(p[1] for p in corners)))
    return found


def _holds(block: _Block, x: float, y: float) -> bool:
    """Whether page point (x, y) is on one of the lines *block*'s translation is drawn
    on (with TOLERANCE)."""
    fx, fy = _turned(block.matrix, x, y)
    size, slack = block.drawn_size, TOLERANCE / math.hypot(*block.matrix[:2])
    return any(left - slack <= fx <= left + drawn + slack and
               base - DESCENT * size - slack <= fy <= base + ASCENT * size + slack
               for left, base, _, drawn in block.placed)


def _bare(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKC", text) if not c.isspace())


def _not_translated(pdf: pikepdf.Pdf) -> list[str]:
    """Where else the file holds text translate_pdf leaves as it is."""
    places = []
    info = pdf.trailer.get("/Info")
    if isinstance(info, pikepdf.Dictionary) and any(
            isinstance(info.get(k), pikepdf.String) and str(info[k]).strip()
            for k in ("/Title", "/Subject", "/Keywords")):
        places.append("document info")
    if isinstance(pdf.Root.get("/Metadata"), pikepdf.Stream):
        places.append("XMP metadata")
    outlines = pdf.Root.get("/Outlines")
    if isinstance(outlines, pikepdf.Dictionary) and \
            isinstance(outlines.get("/First"), pikepdf.Dictionary):
        places.append("bookmarks")
    comments = False
    for page in pdf.pages:
        annots = page.obj.get("/Annots")
        for annot in islice(annots, 10_000) if isinstance(annots, pikepdf.Array) else ():
            if isinstance(annot, pikepdf.Dictionary) and \
                    annot.get("/Subtype") not in (pikepdf.Name.Widget, pikepdf.Name.Link) \
                    and isinstance(annot.get("/Contents"), pikepdf.String) \
                    and str(annot.Contents).strip():
                comments = True
                break
        if comments:
            break
    places += ["comments"] * comments
    form = pdf.Root.get("/AcroForm")
    if isinstance(form, pikepdf.Dictionary) and isinstance(form.get("/Fields"), pikepdf.Array) \
            and len(form.Fields):
        places.append("form fields")
    return places


# --- translate_pdf ----------------------------------------------------------------------


def translate_pdf(path: Path, out: Path, settings: Settings, digest: str,
                  translations: list[dict], language: str | None = None,
                  password: str | None = None) -> TranslateResult:
    """Write a copy with each given block (by get_text_blocks's id) showing its
    translation instead; blocks left out stay as they are."""
    wanted = _check(translations)
    language = _language(language)
    deadline = time.monotonic() + TRANSLATE_TIME_LIMIT_S
    data = path.read_bytes()
    if not isinstance(digest, str) or hashlib.sha256(data).hexdigest() != digest.lower():
        raise PdfUserError("The PDF isn't the one get_text_blocks read (it has changed, or "
                           "the digest is another file's); run get_text_blocks again. "
                           "Nothing was written")
    by_page: dict[int, list[str]] = {}
    for block_id in wanted:
        by_page.setdefault(int(_ID.fullmatch(block_id)[1]) - 1, []).append(block_id)
    with ExitStack() as stack:
        pdf = stack.enter_context(open_pdf(data, settings, password))
        require_modify_permission(pdf)
        require_copy_permission(pdf, "reading the text to translate")
        signed, usage_rights = signature_state(pdf)
        if signed:
            raise PdfUserError("This PDF is digitally signed; translating it would "
                               "invalidate the signature")
        if max(by_page) >= len(pdf.pages):
            raise PdfUserError(f"The PDF has {len(pdf.pages)} pages; block ids must be "
                               "from get_text_blocks. Nothing was written")
        targets = _pages(pdf, ",".join(str(i + 1) for i in sorted(by_page)))
        doc = _open(_plain(pdf))
        stack.callback(_close, doc)
        pages: dict[int, _Page] = {}
        chosen: list[_Block] = []
        for index in targets:
            page = pages[index] = _read_page(doc, index, _visible_box(pdf.pages[index]),
                                             deadline, obstacles=True)
            if page.things is not None:
                page.things += _annotation_boxes(pdf.pages[index])
            ids = {b.id: b for b in page.listed}
            unknown = [i for i in by_page[index] if i not in ids]
            if unknown:
                raise PdfUserError(f"Page {index + 1} has no block {', '.join(unknown[:10])}"
                                   "; use the ids get_text_blocks gives. Nothing was "
                                   "written")
            chosen += sorted((ids[i] for i in by_page[index]), key=lambda b: b.number)
        no_room = []
        for index, page in pages.items():
            translated = {b.number for b in chosen if b.page == index}
            for block in (b for b in chosen if b.page == index):
                page.budget.check()
                if not _fit(block, wanted[block.id], page, translated):
                    no_room.append(block.id)
        if no_room:
            listed = ", ".join(no_room[:20]) + (" …" if len(no_room) > 20 else "")
            raise PdfUserError(f"Blocks {listed} have no room for their translations, even "
                               f"at {MIN_SHRINK:.0%} of their size; shorten them. Nothing "
                               "was written")
        for index, page in pages.items():
            _clashes(page, [b for b in chosen if b.page == index])
        source, forms, content = _drawings(pdf, chosen)
        stack.callback(source.close)

        budget = Budget(deadline, action="Translating")
        fonts: dict = {}
        gone: set = set()
        touched: dict = {}
        areas: dict[int, list] = {}
        untagged = False
        for index in sorted(pages):
            ns = [n for n, b in enumerate(chosen) if b.page == index]
            _overlapping(doc, index, [chosen[n] for n in ns], budget)
            try:
                redactor, areas[index], end = _remove(pdf, index, ns, chosen, forms,
                                                      budget, fonts)
            except Unsure as exc:
                raise PdfUserError(f"The text on page {index + 1} can't be translated "
                                   f"exactly: {exc}. Nothing was written") from None
            untagged = untagged or end
            gone |= redactor.gone
            if _INVISIBLE in redactor.modes:
                raise PdfUserError(
                    f"Text to translate on page {index + 1} is invisible: it is the "
                    "searchable layer over a scanned picture, whose visible words are "
                    "pixels translate_pdf can't change. Nothing was written")
            if redactor.modes - {0}:
                raise PdfUserError(
                    f"Text to translate on page {index + 1} is drawn in outline; "
                    "translate_pdf can only draw filled text. Nothing was written")
            touched[pdf.pages[index].obj.objgen] = redactor.mcids
        check = _open(_plain(pdf))   # the pages without their old text, nor the new
        stack.callback(_close, check)
        for index in sorted(areas):
            budget.check()
            if _verify(_glyphs(doc, index), _glyphs(check, index), areas[index]):
                raise PdfUserError(f"The text on page {index + 1} can't be translated "
                                   "exactly: text outside the translated blocks would "
                                   "have changed. Nothing was written")
        before = {index: _glyphs(check, index) for index in areas}

        _strip_alt_text(pdf, touched)
        for index in areas:
            obj = pdf.pages[index].obj
            for key in ("/Thumb", "/PieceInfo", "/Metadata"):  # they'd show the old text
                if key in obj:
                    del obj[key]
        if usage_rights:  # Reader's usage rights are a signature this would break
            del pdf.Root.Perms
        originals_kept = _cut_loose(pdf, gone, deadline)
        for n, form in forms.items():
            form.write(content[n])
        final = _open(_plain(pdf))
        stack.callback(_close, final)
        for index in sorted(areas):
            budget.check()
            drawn = _confirm(index, before[index], _glyphs(final, index),
                             [b for b in chosen if b.page == index], wanted, budget)
            if not _shown(check, final, index, drawn, budget):
                raise PdfUserError(f"A translation on page {index + 1} would be partly "
                                   "hidden: cut off where the page clips its text, or under "
                                   "something drawn over it. Nothing was written")
        if language:
            pdf.Root.Lang = pikepdf.String(language)
        protection = inherit_protection(pdf, password)
        pdf.save(str(out), encryption=protection.encryption,
                 deterministic_id=not protection.encryption,
                 fix_metadata_version=False)
        tagged = isinstance(pdf.Root.get("/StructTreeRoot"), pikepdf.Dictionary)
        elsewhere = _not_translated(pdf)

    return protection.report({
        "output": str(out),
        "blocks_translated": len(chosen),
        "pages_translated": [i + 1 for i in sorted(areas)],
        "shrunk": [{"block": b.id, "scale": round(b.scale, 2)} for b in chosen
                   if b.scale < 1],
        "fonts": sorted({b.font for b in chosen}),
        "language": language,
        "new_text_untagged": tagged and untagged,
        "not_translated": elsewhere,
        "originals_kept": originals_kept,
    })


def _overlapping(doc, index: int, blocks: list[_Block], budget: Budget) -> None:
    """Refuse a block whose areas hold other text, which removing it would take too."""
    own = set().union(*(b.chars() for b in blocks))
    inner = _Cells()
    for block in blocks:
        for a in block.areas:
            if a[2] - a[0] > 2 and a[3] - a[1] > 2:
                inner.add((a[0] + 1, a[1] + 1, a[2] - 1, a[3] - 1), block)
    with _PDFIUM_LOCK:
        pdf_page = doc[index]
        try:
            textpage = pdf_page.get_textpage()
            try:
                chars = _chars(textpage)
            finally:
                textpage.close()
        finally:
            pdf_page.close()
    for i, (u, box, generated) in enumerate(chars):
        if not i & 0x3FF:
            budget.check()
        if i in own or generated or (u < 0x110000 and chr(u).isspace()):
            continue
        under = inner.at((box[0] + box[2]) / 2, (box[1] + box[3]) / 2)
        if under:
            raise PdfUserError(f"Block {under[0][1].id} on page {index + 1} has other text "
                               "drawn over it, which translating it would remove; leave it "
                               "out. Nothing was written")


def _confirm(index: int, before: list, after: list, blocks: list[_Block],
             wanted: dict[str, str], budget: Budget) -> list[tuple]:
    """The text drawn on a page must be the translations, no more and no less, each
    read back where it was drawn (text there already, like a watermark, aside).
    Returns the new characters' centres with the size each was drawn at."""
    there: dict[tuple, list] = {}   # by character and cell of TOLERANCE
    for u, x, y in before:
        there.setdefault((u, math.floor(x / TOLERANCE), math.floor(y / TOLERANCE)),
                         []).append((x, y))

    def known(u: int, x: float, y: float) -> bool:
        cx, cy = math.floor(x / TOLERANCE), math.floor(y / TOLERANCE)
        return any(abs(x - px) <= TOLERANCE and abs(y - py) <= TOLERANCE
                   for dx in (-1, 0, 1) for dy in (-1, 0, 1)
                   for px, py in there.get((u, cx + dx, cy + dy), ()))

    lines = _Cells()
    for block in blocks:
        for area in _drawn_areas(block):
            lines.add((area[0] - TOLERANCE, area[1] - TOLERANCE, area[2] + TOLERANCE,
                       area[3] + TOLERANCE), block)
    # What each block still needs to be read: a character drawn where two blocks'
    # lines cross (text over a watermark) counts for the one it is part of.
    need = {block.number: Counter(_bare(wanted[block.id])) for block in blocks}
    new, drawn = [], []
    for n, (u, x, y) in enumerate(after):
        if not n & 0x3FF:
            budget.check()
        if known(u, x, y):
            continue
        new.append(chr(u))
        owners = [block for _, block in lines.at(x, y) if _holds(block, x, y)]
        if not owners:
            continue
        read = _bare(chr(u))
        owner = next((block for block in owners
                      if all(need[block.number][c] > 0 for c in read)), owners[0])
        need[owner.number].subtract(read)
        drawn.append((x, y, owner.drawn_size))
    expected: Counter = Counter()
    for block in blocks:
        expected += Counter(_bare(wanted[block.id]))
        if any(left > 0 for left in need[block.number].values()):
            raise PdfUserError(f"The translation of block {block.id} on page {index + 1} "
                               "couldn't be confirmed; nothing was written")
    if Counter(_bare("".join(new))) != expected:
        raise PdfUserError(f"The translations on page {index + 1} couldn't be confirmed; "
                           "nothing was written")
    return drawn


def _picture(doc, index: int, points: list[tuple]) -> tuple:
    """Page *index* of *doc* rendered in grey, at a scale where the smallest of the
    *points*' sizes is INK_PIXELS high (within MAX_PIXELS), with each point's place in
    the picture and its size there in pixels."""
    smallest = min(size for _, _, size in points)
    with _PDFIUM_LOCK:
        page = doc[index]
        try:
            width, height = page.get_size()
            scale = min(INK_MAX_SCALE, max(1.0, INK_PIXELS / smallest),
                        math.sqrt(MAX_PIXELS / max(width * height, 1.0)))
            bitmap = page.render(scale=scale, grayscale=True)
            image = bitmap.to_pil().convert("L").copy()
            dx, dy = ctypes.c_int(), ctypes.c_int()
            placed = []
            for x, y, size in points:
                pdfium_raw.FPDF_PageToDevice(page.raw, 0, 0, image.width, image.height, 0,
                                             x, y, dx, dy)
                placed.append((dx.value, dy.value, size * scale))
        finally:
            page.close()
    return image, placed


def _shown(check, final, index: int, drawn: list[tuple], budget: Budget) -> bool:
    """Whether every new character on page *index* changes the picture where it was
    drawn: not cut off by a clipping path, nor hidden under something drawn later."""
    budget.check()   # (_confirm found each block's characters: there are some)
    before, _ = _picture(check, index, drawn)
    budget.check()
    after, placed = _picture(final, index, drawn)
    changed = ImageChops.difference(before, after).point(
        lambda v: 255 if v > INK_CONTRAST else 0)
    for n, (x, y, size) in enumerate(placed):
        if not n & 0x3FF:
            budget.check()
        r = max(1, round(INK_REACH * size))
        if changed.crop((x - r, y - r, x + r + 1, y + r + 1)).getbbox() is None:
            return False
    return True
