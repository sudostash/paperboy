"""edit_text: change words on a page. The old words are taken out of the page's content
for real, as redaction takes them out (pictures and drawings under them stay), and the
new ones are drawn in their place: on the same baseline, at the same size, angle and
colour. They are drawn in the page's own font when it has every character they need
(each one is drawn somewhere on the page in that font, so it is in the embedded copy),
and otherwise in the closest standard font (Helvetica, Times or Courier, bold or italic
as the original; the bundled DejaVu fonts for text those can't show).

Every match is checked before anything changes: it must lie on one line and must not
overlap another edit's. New text starting where the old did keeps the gap there was
before the rest of the line, which moves along (either way) when it is drawn by the same
operator and still ends on the page; other text must fit before the next text on its
line and the page's edge, squeezed if need be (to no less than MIN_SQUEEZE of its
width). Each rewritten page is read back to confirm only the matched characters went
and the rest of the page stayed (or moved as planned); a line that couldn't be moved is
tried again without moving it. The new text is searched for once it is drawn. Any
problem refuses the whole call, so a file is never left half edited.
"""
from __future__ import annotations

import bisect
import ctypes
import io
import math
import re
import time
import unicodedata
from contextlib import ExitStack
from itertools import islice
from pathlib import Path

import pikepdf
import pypdfium2.raw as pdfium_raw
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfgen import canvas

from ..config import Settings
from ..errors import PdfUserError
from ..pagespec import parse_pages
from ..results import EditTextResult
from .base import (
    inherit_protection,
    open_pdf,
    require_copy_permission,
    require_modify_permission,
    signature_state,
)
from .extract import _page_resources
from .markdown_pdf import _dejavu
from .redact import (
    TOLERANCE,
    _chars,
    _cut_loose,
    _glyphs,
    _match_boxes,
    _plain,
    _strip_alt_text,
)
from .redact_content import (
    _IDENTITY,
    Areas,
    Budget,
    Font,
    Redactor,
    Resources,
    Unsure,
    _State,
    content_streams,
    parse,
)
from .render import _PDFIUM_LOCK, _close, _open
from .search import MAX_PAGE_CONTENT, _content_size, _query
from .stamp import _isolate, _own_resources, _visible_box
from .streams import decode

MAX_EDITS = 50
MAX_REPLACE = 500          # characters of new text per edit
MAX_CHANGES = 500          # places one call may change
MAX_CMAP = 2**20           # bytes of a font's ToUnicode map read
MAX_MAP_TOKENS = 200_000   # tokens read from one
MAX_RANGE_LIST = 256       # texts in one range's list (a range is within a byte's codes)
MAX_CELL = 64              # characters filed at one spot (more are stacked: hostile)
MAX_LOOKED = 10_000        # bookmarks, annotations or info entries looked at for old text
MAX_XMP = 4 * 2**20        # bytes of XMP metadata looked at for old text
EDIT_TIME_LIMIT_S = 120    # per call
MIN_SQUEEZE = 0.8          # narrowest new text may be made to fit (of its natural width)
GAP = 0.25                 # of the font size: kept clear before the next text on the line
EDGE = 2.0                 # points kept clear of the page's edge
LINE = 0.2                 # of the font size: baselines this close are the same line
NEIGHBOUR = 0.5            # of the font size: text this close to the line's middle is on it
STILL = 0.01               # of the font size: a line moved less than this stays put
ALIGNS = ("left", "right", "center")
_SUBSET = re.compile(r"^[A-Z]{6}\+")
_MONO = ("courier", "mono", "consol", "menlo", "typewriter", "fixed", "code")
_SANS = ("sans", "helvetica", "arial", "verdana", "tahoma", "calibri", "segoe", "gothic",
         "frutiger", "univers", "futura", "gill", "lato", "roboto", "inter", "aptos",
         "trebuchet", "candara", "corbel", "myriad", "noto sans", "opensans")
_SERIF = ("times", "serif", "georgia", "garamond", "cambria", "minion", "palatino",
          "baskerville", "bookman", "century", "caslon", "didot", "bodoni", "roman",
          "antiqua", "schoolbook", "constantia", "charter")
_BOLD = ("bold", "black", "heavy", "semibold", "demi")
_STANDARD = {"sans": ("Helvetica", "Helvetica-Bold", "Helvetica-Oblique",
                      "Helvetica-BoldOblique"),
             "serif": ("Times-Roman", "Times-Bold", "Times-Italic", "Times-BoldItalic"),
             "mono": ("Courier", "Courier-Bold", "Courier-Oblique", "Courier-BoldOblique")}
_UNICODE = {"sans": ("PaperboySans", "PaperboySans-Bold", "PaperboySans-Oblique",
                     "PaperboySans-Bold"),
            "mono": ("PaperboyMono",) * 4}
_FLAG_SERIF, _FLAG_ITALIC, _FLAG_BOLD = 2, 64, 1 << 18
_EMBEDDED = ("/FontFile", "/FontFile2", "/FontFile3")
_MARK = re.compile(rb"(begin|end)(bfchar|bfrange)")
# A ToUnicode token: a hex string (no longer than a text needs), or a list's bracket.
_TOKEN = re.compile(rb"<([0-9A-Fa-f\s]{0,128})>|\[|\]")


class _NoRoom(Exception):
    """A line couldn't be moved as planned; try again without moving lines."""


def _replacement(text) -> str:
    """New text as it will be drawn: one line, no control characters."""
    if not isinstance(text, str) or len(text) > MAX_REPLACE:
        raise PdfUserError(f"replace must be text of at most {MAX_REPLACE} characters")
    text = unicodedata.normalize("NFC", text)
    if "\n" in text or "\r" in text:
        raise PdfUserError("replace must be one line: edit_text changes text in place, "
                           "it can't add lines")
    if any(unicodedata.category(c) in ("Cc", "Cf", "Cs", "Co", "Cn") for c in text):
        raise PdfUserError("replace must not contain control or invalid characters")
    if any(ord(c) > 0xFFFF for c in text):
        raise PdfUserError("replace can't contain emoji or other characters beyond "
                           "U+FFFF")
    if any(unicodedata.bidirectional(c) in ("R", "AL") for c in text):
        raise PdfUserError("replace can't contain right-to-left text (such as Hebrew or "
                           "Arabic): edit_text can't lay it out")
    return text


def _check_edits(edits) -> list[tuple[str, str, str]]:
    """The edits as (find, replace, align)."""
    if not isinstance(edits, list) or not 1 <= len(edits) <= MAX_EDITS:
        raise PdfUserError(f"Give 1 to {MAX_EDITS} edits")
    checked = []
    for n, edit in enumerate(edits, 1):
        if not isinstance(edit, dict) or not {"find", "replace"} <= set(edit) or \
                not set(edit) <= {"find", "replace", "align"}:
            raise PdfUserError(f"Edit {n} must be {{\"find\": ..., \"replace\": ...}}, "
                               "with \"align\" optional")
        align = edit.get("align", "left")
        if align not in ALIGNS:
            raise PdfUserError(f"Edit {n}'s align must be one of: {', '.join(ALIGNS)}")
        try:
            find = _query(edit["find"])
        except PdfUserError as exc:
            raise PdfUserError(f"Edit {n}'s find: {exc}") from None
        checked.append((find, _replacement(edit["replace"]), align))
    return checked


# --- fonts ------------------------------------------------------------------------------


def _font_for(name: str, flags: int, weight: int, text: str) -> str:
    """The standard (or bundled) font closest to the original, that can show *text*."""
    lower = name.lower().replace("-", " ").replace(",", " ")
    if any(k in lower for k in _MONO) or flags & 1:
        family = "mono"
    elif any(k in lower for k in _SANS):
        family = "sans"
    elif any(k in lower for k in _SERIF) or flags & _FLAG_SERIF:
        family = "serif"
    else:
        family = "sans"
    bold = any(k in lower for k in _BOLD) or weight >= 600 or bool(flags & _FLAG_BOLD)
    italic = "italic" in lower or "oblique" in lower or bool(flags & _FLAG_ITALIC)
    face = bold + 2 * italic
    try:
        text.encode("cp1252")
        return _STANDARD[family][face]
    except UnicodeEncodeError:
        pass
    text_chars, code_chars = _dejavu()
    if family == "mono":
        have, font = code_chars, _UNICODE["mono"][face]
    else:
        have, font = text_chars, _UNICODE["sans"][face]
    missing = sorted({c for c in text if ord(c) not in have and not c.isspace()})
    if missing:
        raise PdfUserError(f"The new text has characters no available font can show: "
                           f"{''.join(missing[:20])}. Nothing was written")
    return font


def _hex(digits: bytes) -> bytes:
    digits = bytes(c for c in digits if c not in b" \t\r\n\f\0")
    return bytes.fromhex((digits + b"0" * (len(digits) % 2)).decode())


def _text(digits: bytes) -> str | None:
    try:
        return _hex(digits).decode("utf-16-be")
    except UnicodeDecodeError:
        return None


def _sections(data: bytes) -> list[tuple[bytes, bytes]]:
    """The bfchar and bfrange sections of a ToUnicode map, found in one pass (a damaged
    map with many unclosed sections costs no more to read)."""
    found, kind, start = [], None, 0
    for m in _MARK.finditer(data):
        if m.group(1) == b"begin":
            kind, start = m.group(2), m.end()
        elif m.group(2) == kind:
            found.append((kind, data[start:m.start()]))
            kind = None
    return found


class _UnicodeMap:
    """What a font's codes stand for, read once from its ToUnicode map (or, for a simple
    font in WinAnsiEncoding without one, from the encoding): single codes, and ranges as
    (code length, first, last, first text as a number with its length, or a list of
    texts). The map is read in one pass of at most MAX_MAP_TOKENS tokens; a map too
    large or long is treated as empty (the font isn't used)."""

    def __init__(self, font: pikepdf.Dictionary, budget: Budget) -> None:
        self.single: dict[bytes, str] = {}
        self.ranges: list = []
        self.winansi = False
        tounicode = font.get("/ToUnicode")
        if not isinstance(tounicode, pikepdf.Stream):
            self.winansi = font.get("/Encoding") == pikepdf.Name.WinAnsiEncoding and \
                font.get("/Subtype") != pikepdf.Name.Type0
            return
        data = decode(tounicode, MAX_CMAP + 1)
        if data is None or len(data) > MAX_CMAP:
            return
        budget.spend_font(len(data))
        tokens = 0
        for kind, body in _sections(data):
            found: list = []   # hex digits (bytes), or "[" / "]"
            for m in _TOKEN.finditer(body):
                tokens += 1
                if tokens > MAX_MAP_TOKENS:
                    self.single, self.ranges = {}, []
                    return
                found.append(m.group(1) if m.group(1) is not None else m.group(0).decode())
            budget.check()
            if kind == b"bfchar":
                self._pairs(found)
            else:
                self._ranges(found)

    def _pairs(self, found: list) -> None:
        for src, dst in zip(found[::2], found[1::2], strict=False):
            if isinstance(src, bytes) and isinstance(dst, bytes) and \
                    (text := _text(dst)) is not None:
                self.single[_hex(src)] = text

    def _ranges(self, found: list) -> None:
        k = 0
        while k + 2 < len(found):
            low, high, target = found[k:k + 3]
            k += 3
            if not (isinstance(low, bytes) and isinstance(high, bytes)):
                continue
            lo = _hex(low)
            first, last = int.from_bytes(lo, "big"), int.from_bytes(_hex(high), "big")
            if target == "[":   # a list of texts, one per code
                texts = []
                while k < len(found) and found[k] != "]":
                    if isinstance(found[k], bytes) and len(texts) < MAX_RANGE_LIST:
                        texts.append(found[k])
                    k += 1
                k += 1
                self.ranges.append((len(lo), first, min(last, first + len(texts) - 1),
                                    texts))
            elif isinstance(target, bytes):
                start = _hex(target)
                self.ranges.append((len(lo), first, last,
                                    (int.from_bytes(start, "big"), max(len(start), 2))))

    def lookup(self, used: set[bytes], budget: Budget) -> dict[bytes, str]:
        """What the *used* codes stand for. Each code is settled once: by its own entry,
        else by the last range holding it (found by bisection, skipping codes already
        settled), so a map of many overlapping ranges costs no more."""
        if self.winansi:
            return {code: code.decode("cp1252") for code in used
                    if len(code) == 1 and code not in b"\x81\x8d\x8f\x90\x9d"}
        found = {code: self.single[code] for code in used if code in self.single}
        by_length: dict[int, list[int]] = {}
        for code in used:
            if code not in found:
                by_length.setdefault(len(code), []).append(int.from_bytes(code, "big"))
        nexts = {}   # per length: index -> next index not yet settled (path-halved)
        for length, values in by_length.items():
            values.sort()
            nexts[length] = list(range(len(values) + 1))

        def unsettled(jumps: list, i: int) -> int:
            while jumps[i] != i:
                jumps[i] = jumps[jumps[i]]
                i = jumps[i]
            return i

        for n, (length, first, last, target) in enumerate(reversed(self.ranges)):
            if not n & 0x3FF:
                budget.check()
            values = by_length.get(length)
            if not values:
                continue
            jumps = nexts[length]
            i = unsettled(jumps, bisect.bisect_left(values, first))
            while i < len(values) and values[i] <= last:
                k = values[i] - first
                if isinstance(target, list):
                    text = _text(target[k])
                else:
                    text = _text(f"{target[0] + k:0{2 * target[1]}x}".encode())
                if text is not None:
                    found[values[i].to_bytes(length, "big")] = text
                jumps[i] = i + 1
                i = unsettled(jumps, i + 1)
        return found


class _Glyphs:
    """The characters a page's content draws, filed by where they are (at most
    MAX_CELL in a place: more are stacked on one spot), with the codes each font draws."""

    CELL = 8.0    # points
    REACH = 4     # cells looked through each way

    def __init__(self, found: list) -> None:
        self.cells: dict[tuple[int, int], list] = {}
        self.codes: dict = {}
        self.meanings: dict = {}   # per font: what its codes on this page stand for
        for glyph in found:
            x, y = glyph[3], glyph[4]
            if math.isfinite(x) and math.isfinite(y):
                held = self.cells.setdefault(
                    (math.floor(x / self.CELL), math.floor(y / self.CELL)), [])
                if len(held) < MAX_CELL:
                    held.append(glyph)
                self.codes.setdefault(glyph[0], set()).add(glyph[2])

    def near(self, x: float, y: float, tolerance: float) -> tuple | None:
        """The character drawn nearest (x, y), within *tolerance* each way."""
        tolerance = min(tolerance, self.REACH * self.CELL)
        cx, cy = math.floor(x / self.CELL), math.floor(y / self.CELL)
        best, distance = None, math.inf
        for i in range(cx - self.REACH, cx + self.REACH + 1):
            for j in range(cy - self.REACH, cy + self.REACH + 1):
                for glyph in self.cells.get((i, j), ()):
                    dx, dy = abs(glyph[3] - x), abs(glyph[4] - y)
                    if dx <= tolerance and dy <= tolerance and dx + dy < distance:
                        best, distance = glyph, dx + dy
        return best


def _survey(pdf: pikepdf.Pdf, index: int, budget: Budget, fonts: dict) -> _Glyphs:
    """The characters a page's own content draws (not forms): (font key, font object,
    code, centre x, centre y, end x, end y). None of them if the page can't be read
    exactly."""
    found: list = []
    reader = Redactor(pdf, [], budget, fonts, text_only=True)
    reader.seen = lambda font, code, x, y, ex, ey: found.append(
        (font[0], font[1], code, x, y, ex, ey))
    obj = pdf.pages[index].obj
    try:
        reader.content(parse(obj, content_streams(obj), budget),
                       Resources(_page_resources(obj)), _State(_IDENTITY), 0, frozenset(),
                       top=True)
    except Unsure:
        found = []
    return _Glyphs(found)


# --- finding and measuring --------------------------------------------------------------


class _Change:
    """One place to edit, measured in the text space of its first character: x along
    the baseline from that character's origin, in the same units as the font size."""

    __slots__ = ("page", "edit", "start", "count", "old", "areas", "origin", "matrix",
                 "size", "colour", "font_name", "flags", "weight", "begin", "end",
                 "left", "right", "middle", "font", "x", "scale", "next", "line_end",
                 "edge", "shift", "centres", "own", "same_font", "exact", "line")


def _style(raw, i: int) -> tuple:
    """(origin, matrix (a, b, c, d), size, colour (r, g, b, a), font name, flags,
    weight) of character *i*."""
    x, y = ctypes.c_double(), ctypes.c_double()
    pdfium_raw.FPDFText_GetCharOrigin(raw, i, x, y)
    m = pdfium_raw.FS_MATRIX()
    if not pdfium_raw.FPDFText_GetMatrix(raw, i, m):
        m.a, m.b, m.c, m.d = 1.0, 0.0, 0.0, 1.0
    rgba = [ctypes.c_uint() for _ in range(4)]
    if not pdfium_raw.FPDFText_GetFillColor(raw, i, *rgba):
        for v, value in zip(rgba, (0, 0, 0, 255), strict=True):
            v.value = value
    flags, name = ctypes.c_int(), ctypes.create_string_buffer(256)
    pdfium_raw.FPDFText_GetFontInfo(raw, i, name, 256, flags)
    font = _SUBSET.sub("", name.value.decode("latin-1", "replace"))
    return ((x.value, y.value), (m.a, m.b, m.c, m.d), pdfium_raw.FPDFText_GetFontSize(raw, i),
            tuple(v.value for v in rgba), font, flags.value,
            pdfium_raw.FPDFText_GetFontWeight(raw, i))


def _turned(matrix: tuple, x: float, y: float) -> tuple[float, float]:
    """A page vector in the text space of *matrix* (a, b, c, d)."""
    a, b, c, d = matrix
    det = a * d - b * c
    return (d * x - c * y) / det, (-b * x + a * y) / det


def _project(change: _Change, x: float, y: float) -> tuple[float, float]:
    """A page point in the change's text space."""
    return _turned(change.matrix, x - change.origin[0], y - change.origin[1])


class _Lines:
    """A page's characters (not spaces), for finding what shares a line with a change:
    for each turn of text asked about, their extents in its text space (from the
    page's origin), worked out once and sorted by height."""

    def __init__(self, chars: list, budget: Budget) -> None:
        self.boxes = [(i, box) for i, (u, box, generated) in enumerate(chars)
                      if not generated and box[2] > box[0] and
                      not (u < 0x110000 and chr(u).isspace())]
        self.budget = budget
        self._turns: dict = {}

    def along(self, matrix: tuple) -> tuple[list, list]:
        """(heights, [(height, left, right, index)]) in *matrix*'s text space, by height."""
        key = tuple(round(v, 6) for v in matrix)
        if key not in self._turns:
            found = []
            for n, (i, box) in enumerate(self.boxes):
                if not n & 0xFFF:
                    self.budget.check()
                points = [_turned(matrix, px, py) for px in (box[0], box[2])
                          for py in (box[1], box[3])]
                xs, ys = [q[0] for q in points], [q[1] for q in points]
                found.append(((min(ys) + max(ys)) / 2, min(xs), max(xs), i))
            found.sort()
            self._turns[key] = ([f[0] for f in found], found)
        return self._turns[key]


def _span(change: _Change, box: tuple) -> tuple[float, float, float]:
    """(left, right, middle height) of a page box along the change's baseline."""
    points = [_project(change, px, py) for px in (box[0], box[2]) for py in (box[1], box[3])]
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    return min(xs), max(xs), (min(ys) + max(ys)) / 2


def _edge_room(change: _Change, visible: tuple) -> tuple[float, float]:
    """How far the baseline runs back and forward from the origin inside the page."""
    a, b = change.matrix[0], change.matrix[1]
    ox, oy = change.origin
    back = forward = math.inf
    for o, step, low, high in ((ox, a, visible[0], visible[2]),
                               (oy, b, visible[1], visible[3])):
        if abs(step) < 1e-9:
            continue
        ahead, behind = (high - o) / step, (low - o) / step
        if step < 0:
            ahead, behind = behind, ahead
        forward, back = min(forward, ahead), min(back, -behind)
    margin = EDGE / math.hypot(a, b)
    return back - margin, forward - margin


def _measure(change: _Change, chars: list, lines: _Lines, visible: tuple) -> None:
    """The old text's extent along its baseline (as its characters' boxes show it, until
    _settle knows exactly), and the room around it: up to the next text on its line
    (keeping the gap there was, up to GAP) and the page's edge."""
    size, own = change.size, range(change.start, change.start + change.count)
    begin = end = 0.0
    for i in own:
        _, box, generated = chars[i]
        if not generated and box[2] > box[0]:
            left, right, _ = _span(change, box)
            begin, end = min(begin, left), max(end, right)
    change.begin, change.end, change.exact = begin, end, False
    back, forward = _edge_room(change, visible)
    left, right = -back, forward
    change.next, change.line_end, change.edge = None, end, forward
    tolerance = LINE * size
    heights, found = lines.along(change.matrix)
    ox, oy = _turned(change.matrix, *change.origin)
    first = bisect.bisect_left(heights, change.middle + oy - NEIGHBOUR * size)
    last = bisect.bisect_right(heights, change.middle + oy + NEIGHBOUR * size)
    for n in range(first, last):   # only what is on the line
        if not n & 0xFFF:
            lines.budget.check()
        _, bl, br, i = found[n]
        if i in own:
            continue
        bl, br = bl - ox, br - ox
        if bl >= end - tolerance:
            right = min(right, bl - min(GAP * size, max(bl - end, 0.0)))
            change.next = bl if change.next is None else min(change.next, bl)
            change.line_end = max(change.line_end, br)
        elif br <= begin + tolerance:
            left = max(left, br + min(GAP * size, max(begin - br, 0.0)))
    change.left, change.right = left, right


def _locate(doc, targets: list[int], edits: list, match_case: bool, whole_word: bool,
            deadline: float) -> tuple[list[_Change], list[int], dict]:
    """Every place the edits' texts are found; the pages searched that have no text;
    the characters of each page with a change."""
    changes: list[_Change] = []
    without = []
    page_chars: dict[int, list] = {}
    for index in targets:
        if time.monotonic() > deadline:
            raise PdfUserError(f"Searching took longer than {EDIT_TIME_LIMIT_S} s; nothing "
                               "was written. Try fewer pages")
        with _PDFIUM_LOCK:
            page = doc[index]
            try:
                textpage = page.get_textpage()
                try:
                    chars = _chars(textpage, union=True)
                    if not any(not g for _, _, g in chars):
                        without.append(index + 1)
                        continue
                    found = []
                    for n, (find, _, _) in enumerate(edits):
                        searcher = textpage.search(find, match_case=match_case,
                                                   match_whole_word=whole_word)
                        try:
                            while (hit := searcher.get_next()) is not None:
                                found.append((hit[0], hit[1], n))
                                if len(changes) + len(found) > MAX_CHANGES:
                                    raise PdfUserError(
                                        f"The texts are found more than {MAX_CHANGES} "
                                        "times; edit fewer pages at a time, or make them "
                                        "more specific")
                        finally:
                            searcher.close()
                    found.sort()
                    for (s1, c1, n1), (s2, _, n2) in zip(found, found[1:], strict=False):
                        if s1 + c1 > s2:
                            raise PdfUserError(
                                f"Edits {min(n1, n2) + 1} and {max(n1, n2) + 1} change the "
                                f"same text on page {index + 1}; combine them into one. "
                                "Nothing was written")
                    for start, count, n in found:
                        changes.append(_change(textpage, chars, index, start, count, n))
                    if found:
                        page_chars[index] = chars
                finally:
                    textpage.close()
            finally:
                page.close()
    return changes, without, page_chars


def _change(textpage, chars: list, index: int, start: int, count: int, n: int) -> _Change:
    raw = textpage.raw
    change = _Change()
    change.page, change.edit, change.start, change.count = index, n, start, count
    change.old = re.sub(r"\s+", " ", textpage.get_text_range(start, count)).strip()
    real = [i for i in range(start, start + count)
            if not chars[i][2] and chars[i][1][2] > chars[i][1][0]]
    if not real:
        raise PdfUserError(f"\"{change.old}\" on page {index + 1} can't be located on the "
                           "page. Nothing was written")
    (change.origin, change.matrix, change.size, change.colour, change.font_name,
     change.flags, change.weight) = _style(raw, real[0])
    a, b, c, d = change.matrix
    if abs(a * d - b * c) < 1e-9 or not change.size > 0:
        raise PdfUserError(f"\"{change.old}\" on page {index + 1} is drawn flattened to "
                           "nothing; it can't be edited. Nothing was written")
    for i in real[1:]:
        (x, y), matrix = _style(raw, i)[:2]
        if abs(_project(change, x, y)[1]) > LINE * change.size or any(
                abs(p - q) > 1e-3 * max(1.0, abs(q))
                for p, q in zip(matrix, change.matrix, strict=True)):
            raise PdfUserError(f"\"{change.old}\" on page {index + 1} runs over more than "
                               "one line, or at more than one angle; edit_text changes one "
                               "line at a time. Nothing was written")
    change.middle = _span(change, chars[real[0]][1])[2]
    change.areas = _match_boxes(chars, start, count)
    # Where its characters are, and what they are, to find the font drawing them.
    change.centres = [(chars[i][0], (chars[i][1][0] + chars[i][1][2]) / 2,
                       (chars[i][1][1] + chars[i][1][3]) / 2) for i in real]
    return change


# --- fitting ----------------------------------------------------------------------------


def _settle(change: _Change, glyphs: _Glyphs) -> None:
    """Where the old text ends, exactly: where the content's own reading of its last
    character says its advance ends (PDFium's boxes overstate it for turned text, and
    older PDFium misplaces those). Unknown (text in a form, say): the line can't move."""
    _, x, y = change.centres[-1]
    last = glyphs.near(x, y, NEIGHBOUR * change.size)
    if last is not None:
        change.end, change.exact = _project(change, last[5], last[6])[0], True


def _own_font(change: _Change, glyphs: _Glyphs, new: str, fonts: dict, maps: dict,
              budget: Budget) -> tuple | None:
    """(font object, codes, width) to draw *new* in the font the old text was drawn
    in, if that font has every character: the page draws each of them in it (so the
    embedded copy has it) and its ToUnicode map (or encoding) says which code that is.
    None otherwise."""
    if not new or not glyphs.cells:
        return None
    drawn = []   # each old character with what draws it
    for u, x, y in change.centres:
        near = glyphs.near(x, y, NEIGHBOUR * change.size)
        if near is None or near[0] != (drawn[0][1][0] if drawn else near[0]):
            return None
        drawn.append((u, near))
    key, obj = drawn[0][1][0], drawn[0][1][1]
    if not isinstance(obj, pikepdf.Dictionary) or \
            obj.get("/Subtype") not in (pikepdf.Name.TrueType, pikepdf.Name.Type1,
                                        pikepdf.Name.Type0):
        return None
    encoding = obj.get("/Encoding")
    if isinstance(encoding, pikepdf.Name) and str(encoding).endswith("-V"):
        return None   # written downwards
    try:
        if key not in maps:
            maps[key] = _UnicodeMap(obj, budget)
        if key not in glyphs.meanings:
            glyphs.meanings[key] = maps[key].lookup(glyphs.codes[key], budget)
    except Unsure:   # its fonts are too large to follow
        return None
    meaning = glyphs.meanings[key]
    if any(chr(u) != meaning.get(near[2]) for u, near in drawn):
        return None   # the map doesn't say what the page shows: don't trust it
    codes = {}
    for code, text in sorted(meaning.items()):
        codes.setdefault(text, code)
    if any(ch not in codes for ch in new):
        return None
    data = b"".join(codes[ch] for ch in new)
    if key not in fonts:
        try:
            fonts[key] = Font(obj, budget)
        except Unsure:
            return None
    width = sum(w for _, w, _ in fonts[key].codes(data)) * change.size
    return obj, data, width


def _fit(change: _Change, new: str, align: str, own: tuple | None, reflow: bool) -> None:
    """Where the new text starts, whether the rest of its line moves (*reflow*) or it is
    squeezed; refuses text that can't fit."""
    change.own = own
    if own is not None:
        change.font, width = change.font_name, own[2]
    else:
        change.font = _font_for(change.font_name, change.flags, change.weight, new)
        width = pdfmetrics.stringWidth(new, change.font, change.size)
    # A standard font (Helvetica, say) not embedded is drawn as itself either way.
    change.same_font = own is not None or change.font == change.font_name
    middle = (change.begin + change.end) / 2
    room = {"left": change.right,
            "right": change.end - change.left,
            "center": 2 * min(middle - change.left, change.right - middle)}[align]
    scale, change.shift = 1.0, 0.0
    # Keep the gap there was before the rest of the line, moving it along either way.
    shift = width - change.end
    if reflow and align == "left" and change.exact and change.next is not None and \
            change.line_end + shift <= change.edge:
        change.shift = shift if abs(shift) > STILL * change.size else 0.0
    elif width > room:
        scale = max(room, 0.0) / width
    if scale < MIN_SQUEEZE:
        fits = max(0, int(len(new) * max(room, 0.0) / (width * MIN_SQUEEZE)))
        raise PdfUserError(
            f"\"{new}\" is too long to replace \"{change.old}\" on page {change.page + 1}: "
            f"there is room for about {fits} characters before the next text on the line "
            "or the page's edge. Shorten it, or try align \"right\" or \"center\". "
            "Nothing was written")
    drawn = width * scale
    change.scale = scale
    # Its line's height and its end, from the page's origin in its text space, before
    # any line moves (for _unchanged).
    ox, oy = _turned(change.matrix, *change.origin)
    change.line = (oy + change.middle, ox + change.end)
    change.x = {"left": 0.0, "right": change.end - drawn, "center": middle - drawn / 2}[align]


def _push(change: _Change) -> tuple[float, float]:
    """How far, as a page vector, the change moves the rest of its line."""
    return change.matrix[0] * change.shift, change.matrix[1] * change.shift


def _follow(changes: list[_Change]) -> None:
    """Move changes later on a line another moves along (by every move before them);
    refuse a line pushed past the page's edge."""
    by_page: dict[int, list[_Change]] = {}
    for change in changes:
        by_page.setdefault(change.page, []).append(change)
    for index, on_page in by_page.items():
        moves = _moves(on_page, index)
        for _, _, lines in moves:
            for _, _, _, _, on_line in lines:
                ahead = 0.0   # what the changes from here on push the line's end by
                for change in reversed(on_line):
                    ahead += change.shift
                    if change.line_end + ahead > change.edge:
                        raise PdfUserError(
                            f"The new text on page {index + 1} pushes the rest of the line "
                            f"with \"{change.old}\" past the page's edge; shorten it. "
                            "Nothing was written")
        for change in on_page:
            mx = my = 0.0
            for matrix, heights, lines in moves:
                px, py = _turned(matrix, *change.origin)
                k = bisect.bisect_left(heights, py + change.middle)
                for height, tolerance, ends, sums, _ in lines[max(k - 1, 0):k + 1]:
                    if abs(py + change.middle - height) <= tolerance:
                        # Every change ending where this one starts, or before.
                        dx, dy = sums[bisect.bisect_right(ends, px + LINE * change.size)]
                        mx, my = mx + dx, my + dy
                        break
            change.origin = (change.origin[0] + mx, change.origin[1] + my)


def _moves(changes: list[_Change], index: int) -> list:
    """The page's moving lines, to find how far a character moves in one bisection: per
    turn of text, (matrix, line heights, lines), each line (height, tolerance, the ends
    of the changes moving it in order, the sum of their moves up to each, the changes)."""
    groups: dict = {}
    for c in changes:
        if c.shift and c.page == index:
            key = tuple(round(v, 6) for v in c.matrix)
            groups.setdefault(key, (c.matrix, []))[1].append(c)
    found = []
    for matrix, moving in groups.values():
        lines: list = []
        for c in sorted(moving, key=lambda c: c.line[0]):
            if lines and abs(c.line[0] - lines[-1][0]) <= LINE * c.size:
                lines[-1][2].append(c)
            else:
                lines.append([c.line[0], NEIGHBOUR * c.size, [c]])
        prepared = []
        for height, tolerance, on_line in lines:
            on_line.sort(key=lambda c: c.line[1])
            sums, x, y = [(0.0, 0.0)], 0.0, 0.0
            for c in on_line:
                dx, dy = _push(c)
                x, y = x + dx, y + dy
                sums.append((x, y))
            prepared.append((height, tolerance, [c.line[1] for c in on_line], sums,
                             on_line))
        found.append((matrix, [line[0] for line in prepared], prepared))
    return found


def _unchanged(before: list, after: list, areas: list, changes: list[_Change],
               index: int, budget: Budget) -> bool:
    """Whether a page reads as it should once its old text is removed: every character
    outside the areas still there, in order, where it was or where its line moved to,
    and nothing else. Whether a character was removed is decided where it was; one
    within TOLERANCE of an area's edge may go either way."""
    t = TOLERANCE
    inner = Areas((a[0] + t, a[1] + t, a[2] - t, a[3] - t) for a in areas
                  if a[2] - a[0] > 2 * t and a[3] - a[1] > 2 * t)
    outer = Areas((a[0] - t, a[1] - t, a[2] + t, a[3] + t) for a in areas)
    moves = _moves(changes, index)
    j = 0
    for n, (u, x, y) in enumerate(before):
        if not n & 0xFFF:
            budget.check()
        if inner.hold(x, y):
            continue
        optional = outer.hold(x, y)
        mx = my = 0.0
        for matrix, heights, lines in moves:   # moved by every change before it on its line
            px, py = _turned(matrix, x, y)
            k = bisect.bisect_left(heights, py)
            for height, tolerance, ends, sums, _ in lines[max(k - 1, 0):k + 1]:
                if abs(py - height) <= tolerance:
                    dx, dy = sums[bisect.bisect_left(ends, px)]
                    mx, my = mx + dx, my + dy
                    break
        x, y = x + mx, y + my
        if j < len(after) and after[j][0] == u and abs(after[j][1] - x) <= t and \
                abs(after[j][2] - y) <= t:
            j += 1
        elif not optional:
            return False
    return j == len(after)


def _count(doc, index: int, text: str) -> int:
    with _PDFIUM_LOCK:
        page = doc[index]
        try:
            textpage = page.get_textpage()
            searcher = textpage.search(text, match_case=True)
            try:
                found = 0
                while searcher.get_next() is not None:
                    found += 1
                return found
            finally:
                searcher.close()
                textpage.close()
        finally:
            page.close()


# --- drawing ----------------------------------------------------------------------------


def _drawings(pdf: pikepdf.Pdf, changes: list[_Change], edits: list
              ) -> tuple[pikepdf.Pdf, dict[int, pikepdf.Stream], dict[int, bytes]]:
    """A form drawing each change's new text (by change index; none for text removed
    without replacement), and the content each form gets once the removal is checked
    (they are empty until then). Returns the Pdf the standard fonts' forms came from
    too: keep it open until saved."""
    drawn = [n for n, change in enumerate(changes) if edits[change.edit][1]]
    standard = [n for n in drawn if changes[n].own is None]
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    for n in standard:
        change = changes[n]
        r, g, b, alpha = change.colour
        c.setPageSize((100, 100))   # each form's box is set to its page's below
        c.saveState()
        c.transform(*change.matrix, *change.origin)
        c.setFillColorRGB(r / 255, g / 255, b / 255)
        c.setFillAlpha(alpha / 255)
        text = c.beginText(change.x, 0)
        text.setFont(change.font, change.size)
        text.setHorizScale(change.scale * 100)
        text.textOut(edits[change.edit][1])
        c.drawText(text)
        c.restoreState()
        c.showPage()
    c.save()
    source = pikepdf.open(io.BytesIO(buf.getvalue()))
    forms, content = {}, {}
    # Drawn inside the page's content, a form inherits its text state: reset it.
    reset = b"0 Tc 0 Tw 100 Tz 0 Ts 0 Tr\n"
    for page, n in enumerate(standard):
        forms[n] = pdf.copy_foreign(source.pages[page].as_form_xobject())
        content[n] = reset + forms[n].read_bytes()
    for n in drawn:
        change = changes[n]
        if change.own is None:
            continue
        obj, data, _ = change.own
        r, g, b, alpha = (v / 255 for v in change.colour)
        numbers = " ".join(f"{v:.6f}" for v in (*change.matrix, *change.origin))
        content[n] = reset + (
            f"q {numbers} cm {r:.4f} {g:.4f} {b:.4f} rg /Pa gs BT /Pf {change.size:.4f} Tf "
            f"{change.scale * 100:.3f} Tz {change.x:.4f} 0 Td <{data.hex()}> Tj ET Q\n"
        ).encode()
        forms[n] = pdf.make_stream(b"", Type=pikepdf.Name.XObject,
                                   Subtype=pikepdf.Name.Form, Resources=pikepdf.Dictionary(
                                       Font=pikepdf.Dictionary(Pf=obj),
                                       ExtGState=pikepdf.Dictionary(Pa=pikepdf.Dictionary(
                                           Type=pikepdf.Name.ExtGState, ca=alpha))))
    for n, form in forms.items():
        form.BBox = pikepdf.Array([float(v) for v in pdf.pages[changes[n].page].mediabox])
        form.write(b"")
    return source, forms, content


def _remove(pdf: pikepdf.Pdf, index: int, page_changes: list[int], changes: list[_Change],
            forms: dict, budget: Budget, fonts: dict) -> tuple[Redactor, list, bool]:
    """Take a page's old text out, placing each change's form right after the text it
    replaces (at the end of the page if there's no such place); returns the redactor,
    the page's areas, and whether any form went outside marked content (so isn't in a
    tagged PDF's tags)."""
    page = pdf.pages[index]
    _own_resources(page)
    names = {n: page.add_resource(forms[n], pikepdf.Name.XObject, prefix="Pb")
             for n in page_changes if n in forms}
    areas, owner = [], []
    for n in page_changes:
        areas += changes[n].areas
        owner += [n] * len(changes[n].areas)
    placed: set[int] = set()
    untagged = False

    def draw(held: frozenset, back: tuple, marked: bool) -> tuple[list, float]:
        nonlocal untagged
        here = sorted({owner[k] for k in held} - placed)
        placed.update(here)
        untagged = untagged or (not marked and any(n in names for n in here))
        shift = sum(math.copysign(math.hypot(*_push(changes[n])), changes[n].shift)
                    for n in here)
        shown = [pikepdf.ContentStreamInstruction([pikepdf.Name(names[n])],
                                                  pikepdf.Operator("Do"))
                 for n in here if n in names]
        if not shown:
            return [], shift
        return [pikepdf.ContentStreamInstruction([], pikepdf.Operator("q")),
                pikepdf.ContentStreamInstruction(list(back), pikepdf.Operator("cm")),
                *shown, pikepdf.ContentStreamInstruction([], pikepdf.Operator("Q"))], shift

    redactor = Redactor(pdf, areas, budget, fonts, text_only=True, draw=draw)
    redactor.page(page.obj, _page_resources(page.obj), None)
    left = [n for n in page_changes if n not in placed and n in names]
    if left:
        _isolate(pdf, page)
        page.contents_add(pikepdf.Stream(pdf, b"".join(
            f"q {names[n]} Do Q\n".encode() for n in left)))
    return redactor, areas, untagged or bool(left)


# --- the old text elsewhere ------------------------------------------------------------


def _normal(text: str, match_case: bool) -> str:
    text = re.sub(r"\s+", " ", unicodedata.normalize("NFKC", text))
    return text if match_case else text.casefold()


def _elsewhere(pdf: pikepdf.Pdf, finds: list[str], match_case: bool) -> list[str]:
    """Where else the old texts are still written, which edit_text leaves as they are:
    "document info", "XMP metadata", "bookmarks", "comments", "form fields". At most
    MAX_LOOKED of each are looked at."""
    wanted = [_normal(f, match_case) for f in finds]

    def holds(value) -> bool:
        if not isinstance(value, pikepdf.String):
            return False
        text = _normal(str(value), match_case)
        return any(w in text for w in wanted)

    places = []
    info = pdf.trailer.get("/Info")
    if isinstance(info, pikepdf.Dictionary) and \
            any(holds(info[k]) for k in list(info.keys())[:MAX_LOOKED]):
        places.append("document info")
    meta = pdf.Root.get("/Metadata")
    if isinstance(meta, pikepdf.Stream):
        data = decode(meta, MAX_XMP)
        if data is not None and holds(pikepdf.String(data.decode("utf-8", "replace"))):
            places.append("XMP metadata")
    outlines = pdf.Root.get("/Outlines")
    stack = [outlines.get("/First")] if isinstance(outlines, pikepdf.Dictionary) else []
    seen, looked = set(), 0
    while stack and looked < MAX_LOOKED:
        item = stack.pop()
        if not isinstance(item, pikepdf.Dictionary) or item.objgen in seen:
            continue
        seen.add(item.objgen)
        looked += 1
        if holds(item.get("/Title")):
            places.append("bookmarks")
            break
        stack += [item.get("/Next"), item.get("/First")]
    comments = fields = False
    looked = 0
    for page in pdf.pages:
        annots = page.obj.get("/Annots")
        for annot in islice(annots, MAX_LOOKED) if isinstance(annots, pikepdf.Array) else ():
            looked += 1
            if not isinstance(annot, pikepdf.Dictionary):
                continue
            comments = comments or holds(annot.get("/Contents"))
            fields = fields or holds(annot.get("/V")) or (
                isinstance(annot.get("/Parent"), pikepdf.Dictionary) and
                holds(annot.Parent.get("/V")))
        if looked >= MAX_LOOKED:
            break
    places += ["comments"] * comments + ["form fields"] * fields
    return places


# --- the whole call ---------------------------------------------------------------------


def _attempt(data: bytes, out: Path, settings: Settings, checked: list, pages: str | None,
             match_case: bool, whole_word: bool, password: str | None, reflow: bool,
             deadline: float) -> dict:
    with ExitStack() as stack:
        pdf = stack.enter_context(open_pdf(data, settings, password))
        require_modify_permission(pdf)
        require_copy_permission(pdf, "finding the text to edit")
        signed, usage_rights = signature_state(pdf)
        if signed:
            raise PdfUserError("This PDF is digitally signed; editing it would invalidate "
                               "the signature")
        total = len(pdf.pages)
        targets = parse_pages(pages, total, dedupe=True) if pages else list(range(total))
        sizes: dict = {}
        too_big = [i + 1 for i in targets
                   if (_content_size(pdf.pages[i], MAX_PAGE_CONTENT, sizes) or
                       MAX_PAGE_CONTENT + 1) > MAX_PAGE_CONTENT]
        if too_big:
            listed = ", ".join(map(str, too_big[:20])) + (" …" if len(too_big) > 20 else "")
            raise PdfUserError(f"Pages {listed} have too much content to edit safely; leave "
                               "them out with pages")
        visible = {i: _visible_box(pdf.pages[i]) for i in targets}
        doc = _open(_plain(pdf))
        stack.callback(_close, doc)
        changes, without, page_chars = _locate(doc, targets, checked, match_case,
                                               whole_word, deadline)
        missing = sorted(set(range(len(checked))) - {c.edit for c in changes})
        if missing:
            names = ", ".join(f"{n + 1} (\"{checked[n][0]}\")" for n in missing[:10])
            hint = (f" Pages {', '.join(map(str, without[:20]))} have no text (scans?): "
                    "run ocr_pdf on them first, though their words stay pictures that "
                    "edit_text can't change." if without else "")
            raise PdfUserError(f"Edits {names} weren't found.{hint} Nothing was written")

        budget = Budget(deadline, action="Editing")
        # Reading pages for their fonts has a budget of its own: running out of it only
        # means drawing in standard fonts, never refusing the edit.
        survey = Budget(deadline, action="Editing")
        fonts: dict = {}
        maps: dict = {}
        by_page: dict[int, list[int]] = {}
        for n, change in enumerate(changes):
            by_page.setdefault(change.page, []).append(n)
        for index, ns in by_page.items():
            lines = _Lines(page_chars[index], budget)
            glyphs = _survey(pdf, index, survey, {})
            for n in ns:
                _, new, align = checked[changes[n].edit]
                _measure(changes[n], page_chars[index], lines, visible[index])
                _settle(changes[n], glyphs)
                _fit(changes[n], new, align,
                     _own_font(changes[n], glyphs, new, fonts, maps, survey), reflow)
        _follow(changes)
        source, forms, content = _drawings(pdf, changes, checked)
        stack.callback(source.close)

        gone: set = set()
        touched: dict = {}
        areas: dict[int, list] = {}
        at_end = False
        for index in sorted(by_page):
            try:
                redactor, areas[index], end = _remove(pdf, index, by_page[index], changes,
                                                      forms, budget, fonts)
            except Unsure as exc:
                raise PdfUserError(f"The text on page {index + 1} can't be edited "
                                   f"exactly: {exc}. Nothing was written") from None
            at_end = at_end or end
            gone |= redactor.gone
            if 3 in redactor.modes:
                raise PdfUserError(
                    f"The text to edit on page {index + 1} is invisible: it is the "
                    "searchable layer over a scanned picture, whose visible words are "
                    "pixels edit_text can't change. Nothing was written")
            if redactor.modes - {0}:   # outlined (or filled and outlined): drawn filled,
                #                        the new text could be invisible or look different
                raise PdfUserError(
                    f"The text to edit on page {index + 1} is drawn in outline; edit_text "
                    "can only draw filled text. Nothing was written")
            touched[pdf.pages[index].obj.objgen] = redactor.mcids
        check = _open(_plain(pdf))
        try:
            for index in sorted(areas):
                budget.check()
                if _unchanged(_glyphs(doc, index), _glyphs(check, index), areas[index],
                              changes, index, budget):
                    continue
                if any(c.shift and c.page == index for c in changes):
                    raise _NoRoom()
                raise PdfUserError(f"The text on page {index + 1} can't be edited exactly: "
                                   "text outside the edits would have changed. Nothing was "
                                   "written")
            expected: dict = {}
            for change in changes:
                new = checked[change.edit][1]
                if new.strip():
                    key = (change.page, _query(new))
                    expected[key] = expected.get(key, 0) + 1
            already = {key: _count(check, *key) for key in expected}
        finally:
            _close(check)

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
        try:
            for key, n in expected.items():
                if _count(final, *key) < already[key] + n:
                    raise PdfUserError(f"The new text on page {key[0] + 1} couldn't be "
                                       "confirmed; nothing was written")
        finally:
            _close(final)
        protection = inherit_protection(pdf, password)
        pdf.save(str(out), encryption=protection.encryption,
                 deterministic_id=not protection.encryption,
                 fix_metadata_version=False)
        tagged = isinstance(pdf.Root.get("/StructTreeRoot"), pikepdf.Dictionary)
        elsewhere = _elsewhere(pdf, [find for find, _, _ in checked], match_case)

    listed = [{"page": c.page + 1, "find": checked[c.edit][0], "old": c.old,
               "new": checked[c.edit][1], "font": c.font, "same_font": c.same_font,
               "original_font": c.font_name, "size": round(c.size, 2),
               "squeezed": None if c.scale == 1.0 else round(c.scale, 3),
               "line_moved": round(math.copysign(math.hypot(*_push(c)), c.shift), 2)
               if c.shift else None}
              for c in changes]
    return protection.report({
        "output": str(out),
        "change_count": len(changes),
        "changes": listed,
        "pages_edited": [i + 1 for i in sorted(areas)],
        "new_text_untagged": tagged and at_end,
        "old_text_elsewhere": elsewhere,
        "originals_kept": originals_kept,
    })


def edit_text(
    path: Path,
    out: Path,
    settings: Settings,
    edits: list[dict],
    pages: str | None = None,
    match_case: bool = True,
    whole_word: bool = False,
    password: str | None = None,
) -> EditTextResult:
    """Write a copy with each edit's find text replaced by its replace text, wherever
    it is found (on *pages*, default all). The old characters are removed from the
    page's content and the new ones drawn where they were: align "left" (default)
    keeps where the text starts, "right" where it ends, "center" its middle."""
    checked = _check_edits(edits)
    deadline = time.monotonic() + EDIT_TIME_LIMIT_S
    data = path.read_bytes()
    try:
        return _attempt(data, out, settings, checked, pages, match_case, whole_word,
                        password, True, deadline)
    except _NoRoom:   # a line couldn't be moved: edit without moving lines (so this
        #               attempt can't raise _NoRoom)
        return _attempt(data, out, settings, checked, pages, match_case, whole_word,
                        password, False, deadline)
