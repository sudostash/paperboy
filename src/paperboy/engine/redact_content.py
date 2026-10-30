"""Taking what lies under areas of a page out of its content, for real.

A page's content is read operator by operator, following the text state, the current
transformation and forms drawn inside it, to find where everything is drawn:
- a character whose centre is inside an area is removed from its string, and the
  space it took is kept so the rest of the line stays where it was;
- a picture partly under an area has those pixels blanked in a copy of it (a
  picture, form or drawing wholly inside an area is removed);
- a drawing (a filled or stroked path) wholly inside an area is removed;
- text stand-ins (/ActualText, /Alt, /E) around anything removed are dropped.
Shared pictures and forms are copied, never changed, so other pages keep theirs.

Anything that can't be followed exactly raises Unsure, naming why: a font whose
widths aren't known, a picture in a format that can't be edited, text that clips
other content, content too large or damaged to read. The caller then turns the page
into a picture instead (or refuses), so a page is never left half redacted.
"""
from __future__ import annotations

import bisect
import io
import itertools
import math
import re
import time
import zlib

import pikepdf
from PIL import Image
from reportlab.pdfbase import _fontdata

from ..errors import PdfUserError
from .extract import _problem, _size
from .images import _DELIMITERS, _WHITESPACE, _mul
from .streams import decode, encoded_image, filters_of

MAX_CONTENT = 16 * 2**20        # decoded bytes of one page's or form's content
MAX_TOTAL_CONTENT = 64 * 2**20  # decoded content bytes read per call
MAX_STREAM_TOKENS = 1_000_000   # tokens one page or form may have to be read
MAX_TOKENS = 4_000_000          # tokens parsed per call (each costs a Python object)
MAX_OPS = 4_000_000             # operators followed per call (forms re-entered count again)
MAX_GLYPHS = 2_000_000          # characters placed per call
MAX_FORM_DEPTH = 12
MAX_WIDTHS = 100_000            # entries in a composite font's widths (/W)
MAX_CMAP = 2**20                # bytes of an embedded CMap
MAX_CODE_SPACES = 16            # code space ranges of a CMap (real ones have a few)
MAX_FONT_BYTES = 64 * 2**20     # CMap and width data read per call
EDGE = 0.5                      # points: within this of an area counts as inside it
PARTLY = 0.4                    # a kept character this much under an area: Unsure
MAX_PIECES = 10_000             # pieces one drawing may be cut into
_IDENTITY = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
_PATH = {"m", "l", "c", "v", "y", "re", "h"}
_PAINT = {"S", "s", "f", "F", "f*", "B", "B*", "b", "b*"}
_STROKE = {"S", "s", "B", "B*", "b", "b*"}
_SHOW = {"Tj", "TJ", "'", '"'}
_STAND_INS = ("/ActualText", "/Alt", "/E")
_GENERAL = {"/FlateDecode", "/LZWDecode", "/ASCII85Decode", "/ASCIIHexDecode",
            "/RunLengthDecode"}
_COMPONENTS = {"/DeviceGray": 1, "/G": 1, "/CalGray": 1, "/Indexed": 1, "/I": 1,
               "/Separation": 1, "/DeviceRGB": 3, "/RGB": 3, "/CalRGB": 3, "/Lab": 3,
               "/DeviceCMYK": 4, "/CMYK": 4}
# Keys a copy of a picture or form doesn't keep: its old encoding, and data about it
# (which may describe what was removed).
_NOT_COPIED = {"/Length", "/Filter", "/DecodeParms", "/Metadata", "/PieceInfo",
               "/LastModified", "/Alternates", "/OPI", "/Thumb"}


class Unsure(Exception):
    """A page can't be redacted precisely; the message says why."""


# --- geometry ---------------------------------------------------------------------------


def _point(m, x: float, y: float) -> tuple[float, float]:
    return m[0] * x + m[2] * y + m[4], m[1] * x + m[3] * y + m[5]


def _bbox(m, x0: float, y0: float, x1: float, y1: float) -> tuple:
    corners = ((x0, y0), (x1, y0), (x0, y1), (x1, y1))
    xs, ys = zip(*(_point(m, x, y) for x, y in corners), strict=True)
    return min(xs), min(ys), max(xs), max(ys)


def _overlaps(box, areas) -> bool:
    return any(box[0] < a[2] and a[0] < box[2] and box[1] < a[3] and a[1] < box[3]
               for a in areas)


def _inside(x: float, y: float, areas) -> bool:
    return any(a[0] <= x <= a[2] and a[1] <= y <= a[3] for a in areas)


class Areas:
    """Rectangles, quickly asked which holds a point: they are filed by the bands of
    the page they cross, so a point is checked only against those in its band."""

    BAND = 24.0   # points
    MAX_BANDS = 200

    def __init__(self, rects) -> None:
        self.bands: dict[int, list] = {}
        self.tall: list = []
        for r in rects:
            low, high = math.floor(r[1] / self.BAND), math.floor(r[3] / self.BAND)
            if high - low > self.MAX_BANDS:
                self.tall.append(r)
                continue
            for band in range(low, high + 1):
                self.bands.setdefault(band, []).append(r)

    def near(self, box) -> list:
        """The rectangles in the bands *box* crosses (and the tall ones)."""
        if not all(math.isfinite(v) for v in box):
            return list(self.tall)
        low, high = math.floor(box[1] / self.BAND), math.floor(box[3] / self.BAND)
        if high - low > self.MAX_BANDS:
            return [r for rects in self.bands.values() for r in rects] + self.tall
        found = {id(r): r for band in range(low, high + 1)
                 for r in self.bands.get(band, ())}
        return [*found.values(), *self.tall]

    def hold(self, x: float, y: float) -> bool:
        if not (math.isfinite(x) and math.isfinite(y)):
            return False
        return _inside(x, y, self.bands.get(math.floor(y / self.BAND), ())) or \
            _inside(x, y, self.tall)


def _within(box, areas) -> bool:
    return any(a[0] - EDGE <= box[0] and box[2] <= a[2] + EDGE
               and a[1] - EDGE <= box[1] and box[3] <= a[3] + EDGE for a in areas)


def _num(value) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise Unsure("its content has an invalid number") from None
    if not math.isfinite(number):
        raise Unsure("its content has an invalid number")
    return number


def _matrix(values) -> tuple:
    if not isinstance(values, (pikepdf.Array, list, tuple)) or len(values) != 6:
        raise Unsure("its content has an invalid matrix")
    return tuple(_num(v) for v in values)


def _rect(value) -> tuple | None:
    if not isinstance(value, pikepdf.Array) or len(value) != 4:
        return None
    try:
        x0, y0, x1, y1 = (float(v) for v in value)
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(v) for v in (x0, y0, x1, y1)):
        return None
    return min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)


# --- fonts ------------------------------------------------------------------------------

_FAMILIES = (("Helvetica", "Helvetica"), ("Arial", "Helvetica"), ("Times", "Times"),
             ("Courier", "Courier"))
_STYLES = {"Helvetica": ("Helvetica", "Helvetica-Bold", "Helvetica-Oblique",
                         "Helvetica-BoldOblique"),
           "Times": ("Times-Roman", "Times-Bold", "Times-Italic", "Times-BoldItalic"),
           "Courier": ("Courier", "Courier-Bold", "Courier-Oblique", "Courier-BoldOblique")}


def _standard(font: pikepdf.Dictionary) -> str | None:
    """The standard font a font without widths stands for: one of the 14 every reader
    has (or Arial, Times New Roman, Courier New, which readers draw with them)."""
    descriptor = font.get("/FontDescriptor")
    if isinstance(descriptor, pikepdf.Dictionary) and any(
            k in descriptor for k in ("/FontFile", "/FontFile2", "/FontFile3")):
        return None  # embedded: its widths are in a font program this can't read
    name = str(font.get("/BaseFont", ""))[1:]
    name = re.sub(r"^[A-Z]{6}\+", "", name)
    if name in _fontdata.widthsByFontGlyph:
        return name
    for prefix, family in _FAMILIES:
        if name.startswith(prefix):
            rest = name[len(prefix):].replace("NewRoman", "").replace("New", "")
            if rest and not re.fullmatch(r"(PS)?(MT)?([,-].*)?", rest):
                return None
            bold, italic = "Bold" in rest, "Italic" in rest or "Oblique" in rest
            return _STYLES[family][bold + 2 * italic]
    return None


def _glyph_names(font: pikepdf.Dictionary, standard: str) -> list:
    builtin = {"Symbol": "SymbolEncoding", "ZapfDingbats": "ZapfDingbatsEncoding"}
    base, differences = builtin.get(standard, "StandardEncoding"), None
    encoding = font.get("/Encoding")
    if isinstance(encoding, pikepdf.Dictionary):
        differences = encoding.get("/Differences")
        encoding = encoding.get("/BaseEncoding")
    if isinstance(encoding, pikepdf.Name):
        base = str(encoding)[1:]
        if base not in _fontdata.encodings:
            raise Unsure(f"a font uses an unknown encoding ({base[:40]})")
    names = list(_fontdata.encodings[base])
    if isinstance(differences, pikepdf.Array):
        code = 0
        for item in differences:
            if isinstance(item, int):
                code = int(item)
            elif isinstance(item, pikepdf.Name):
                if 0 <= code < 256:
                    names[code] = str(item)[1:]
                code += 1
    return names


_MARK = re.compile(rb"(begin|end)(codespacerange|cidrange|cidchar)")
_CMAP_TOKEN = re.compile(rb"<([0-9A-Fa-f\s]*)>|(\d+)")


def _sections(data: bytes) -> list[tuple[bytes, bytes]]:
    """The codespacerange, cidrange and cidchar sections of a CMap, found in one pass."""
    found, kind, start = [], None, 0
    for m in _MARK.finditer(data):
        if m.group(1) == b"begin":
            kind, start = m.group(2), m.end()
        elif m.group(2) == kind:
            found.append((kind, data[start:m.start()]))
            kind = None
    return found


def _cmap(stream: pikepdf.Stream, budget: Budget | None = None):
    """An embedded CMap's code space and code-to-CID mapping."""
    if stream.get("/UseCMap") is not None or stream.get("/WMode", 0) != 0:
        raise Unsure("a font uses vertical writing or a CMap built on another")
    data = decode(stream, MAX_CMAP + 1)
    if data is None or len(data) > MAX_CMAP:
        raise Unsure("a font's CMap can't be read")
    if budget is not None:
        budget.spend_font(len(data))
    if re.search(rb"usecmap|/WMode\s+1", data):
        raise Unsure("a font uses vertical writing or a CMap built on another")
    space, single, ranges, entries = [], {}, {}, 0
    for kind, body in _sections(data):
        tokens: list = []
        for m in _CMAP_TOKEN.finditer(body):
            if m.group(1) is None:
                tokens.append(int(m.group(2)))
                continue
            try:
                tokens.append(bytes.fromhex(re.sub(rb"\s", b"", m.group(1)).decode()))
            except ValueError:
                raise Unsure("a font's CMap is damaged") from None
        step = 2 if kind in (b"codespacerange", b"cidchar") else 3
        entries += len(tokens) // step
        if entries > MAX_WIDTHS:
            raise Unsure("a font's CMap is too large")
        for i in range(0, len(tokens) - step + 1, step):
            group = tokens[i:i + step]
            if kind == b"codespacerange":
                lo, hi = group
                if isinstance(lo, bytes) and isinstance(hi, bytes) and \
                        len(lo) == len(hi) and 1 <= len(lo) <= 4:
                    space.append((lo, hi))
            elif kind == b"cidchar":
                code, cid = group
                if isinstance(code, bytes) and isinstance(cid, int):
                    single[code] = cid
            else:
                lo, hi, cid = group
                if isinstance(lo, bytes) and isinstance(hi, bytes) and \
                        isinstance(cid, int) and len(lo) == len(hi):
                    ranges.setdefault(len(lo), []).append(
                        (int.from_bytes(lo, "big"), int.from_bytes(hi, "big"), cid))
    if not space:
        raise Unsure("a font's CMap has no code space")
    if len(space) > MAX_CODE_SPACES:
        raise Unsure("a font's CMap has too many code spaces")
    for found in ranges.values():
        found.sort()
    return space, single, ranges


def _cid_widths(widths, budget: Budget | None = None) -> tuple[dict, list]:
    single, ranges = {}, []
    if not isinstance(widths, pikepdf.Array):
        return single, ranges
    if len(widths) > MAX_WIDTHS:
        raise Unsure("a font has too many widths")
    items, count, i = list(widths), 0, 0
    while i + 1 < len(items):
        first, then = items[i], items[i + 1]
        if not isinstance(first, int):
            raise Unsure("a font's widths are damaged")
        if isinstance(then, pikepdf.Array):
            count += len(then)
            if count > MAX_WIDTHS:
                raise Unsure("a font has too many widths")
            for k, w in enumerate(then):
                single[int(first) + k] = _num(w)
            i += 2
        elif i + 2 < len(items) and isinstance(then, int):
            ranges.append((int(first), int(then), _num(items[i + 2])))
            i += 3
        else:
            raise Unsure("a font's widths are damaged")
        count += 1
        if count > MAX_WIDTHS:
            raise Unsure("a font has too many widths")
    if budget is not None:
        budget.spend_font(count * 16)
    ranges.sort()
    return single, ranges


class Font:
    """What redaction needs of a font: how its strings split into character codes, the
    width of each, and the height of its glyphs (all in text space, per unit of size).
    Type 3 glyphs are drawn by their own content, anywhere in their box: *box* is that
    box, in the same units, or None if the font doesn't say."""

    def __init__(self, font, budget: Budget | None = None) -> None:
        if not isinstance(font, pikepdf.Dictionary):
            raise Unsure("text uses a font that is missing")
        self.composite = font.get("/Subtype") == pikepdf.Name.Type0
        descriptor = font.get("/FontDescriptor")
        self.ascent, self.descent, self.box = 0.8, -0.2, None
        if self.composite:
            self._composite(font, budget)
            descendants = font.get("/DescendantFonts")
            if isinstance(descendants, pikepdf.Array) and len(descendants) and \
                    isinstance(descendants[0], pikepdf.Dictionary):
                descriptor = descendants[0].get("/FontDescriptor")
        else:
            self._simple(font)
        if not self.type3:
            self._heights(descriptor)

    def _simple(self, font) -> None:
        self.type3 = font.get("/Subtype") == pikepdf.Name.Type3
        self.scale = 0.001
        if self.type3:
            m = _matrix(font.get("/FontMatrix"))
            if m[1] or m[2] or not m[0] or not m[3]:
                raise Unsure("a Type 3 font is rotated or slanted")
            self.scale = m[0]
            box = _rect(font.get("/FontBBox"))
            if box is not None and box[3] > box[1] and box[2] > box[0]:
                xs = sorted((box[0] * m[0] + m[4], box[2] * m[0] + m[4]))
                ys = sorted((box[1] * m[3] + m[5], box[3] * m[3] + m[5]))
                self.descent, self.ascent = ys
                self.box = (xs[0], ys[0], xs[1], ys[1])
        widths = font.get("/Widths")
        if isinstance(widths, pikepdf.Array):
            first = font.get("/FirstChar", 0)
            first = int(first) if isinstance(first, int) else 0
            self.widths = {first + i: _num(w) for i, w in enumerate(list(widths)[:256])}
            descriptor = font.get("/FontDescriptor")
            missing = descriptor.get("/MissingWidth", 0) \
                if isinstance(descriptor, pikepdf.Dictionary) else 0
            self.missing = _num(missing)
        else:
            standard = None if self.type3 else _standard(font)
            if standard is None:
                raise Unsure("a font has no widths")
            metrics = _fontdata.widthsByFontGlyph[standard]
            self.widths = {code: metrics.get(name, 0)
                           for code, name in enumerate(_glyph_names(font, standard)) if name}
            self.missing = 0.0
            self.ascent, self.descent = (v / 1000 for v in _fontdata.ascent_descent[standard])

    def _composite(self, font, budget: Budget | None) -> None:
        self.type3, self.scale = False, 0.001
        encoding = font.get("/Encoding")
        if encoding == pikepdf.Name("/Identity-H"):
            self.space, self.single, self.ranges = [(b"\0\0", b"\xff\xff")], None, None
        elif isinstance(encoding, pikepdf.Stream):
            self.space, self.single, self.ranges = _cmap(encoding, budget)
        else:
            name = str(encoding)[1:41] if isinstance(encoding, pikepdf.Name) else "?"
            raise Unsure(f"a font uses an encoding this can't follow ({name})")
        self.lengths = sorted({len(lo) for lo, _ in self.space})
        descendants = font.get("/DescendantFonts")
        cid_font = descendants[0] if isinstance(descendants, pikepdf.Array) and \
            len(descendants) else None
        if not isinstance(cid_font, pikepdf.Dictionary):
            raise Unsure("a font is damaged")
        default = cid_font.get("/DW", 1000)
        self.default = _num(default)
        self.cid_single, self.cid_ranges = _cid_widths(cid_font.get("/W"), budget)
        self._starts = [r[0] for r in self.cid_ranges]

    def _heights(self, descriptor) -> None:
        if not isinstance(descriptor, pikepdf.Dictionary):
            return
        ascent, descent = descriptor.get("/Ascent"), descriptor.get("/Descent")
        if isinstance(ascent, (int, float, pikepdf.Object)) and \
                isinstance(descent, (int, float, pikepdf.Object)):
            try:
                a, d = float(ascent) / 1000, float(descent) / 1000
            except (TypeError, ValueError):
                return
            if math.isfinite(a) and math.isfinite(d) and 0.1 <= a - d <= 5 and a > 0:
                self.ascent, self.descent = a, min(d, 0.0)
                return
        box = _rect(descriptor.get("/FontBBox"))
        if box is not None and 0.1 <= (box[3] - box[1]) / 1000 <= 5:
            self.ascent, self.descent = box[3] / 1000, box[1] / 1000

    def _cid(self, code: bytes) -> int:
        if self.ranges is None:  # Identity-H
            return int.from_bytes(code, "big")
        if code in self.single:
            return self.single[code]
        value = int.from_bytes(code, "big")
        found = self.ranges.get(len(code), [])
        i = bisect.bisect_right(found, (value, math.inf, math.inf)) - 1
        if i >= 0 and found[i][0] <= value <= found[i][1]:
            return found[i][2] + value - found[i][0]
        return 0

    def _cid_width(self, cid: int) -> float:
        if cid in self.cid_single:
            return self.cid_single[cid]
        i = bisect.bisect_right(self._starts, cid) - 1
        if i >= 0 and self.cid_ranges[i][0] <= cid <= self.cid_ranges[i][1]:
            return self.cid_ranges[i][2]
        return self.default

    def codes(self, data: bytes) -> list[tuple[bytes, float, bool]]:
        """Each character code in *data*, its width, and whether word spacing applies
        to it (only to the single byte 32)."""
        if not self.composite:
            return [(data[i:i + 1], self.widths.get(b, self.missing) * self.scale, b == 32)
                    for i, b in enumerate(data)]
        found, i = [], 0
        while i < len(data):
            for n in self.lengths:
                code = data[i:i + n]
                if len(code) == n and any(len(lo) == n and all(
                        lo[k] <= code[k] <= hi[k] for k in range(n)) for lo, hi in self.space):
                    break
            else:
                raise Unsure("text has character codes outside its font's code space")
            found.append((code, self._cid_width(self._cid(code)) * self.scale,
                          code == b" "))
            i += n
        return found


# --- reading content --------------------------------------------------------------------


class Budget:
    """Work left for one call; running out makes the page in hand Unsure (and the
    deadline ends the call)."""

    def __init__(self, deadline: float, content: int = MAX_TOTAL_CONTENT,
                 tokens: int = MAX_TOKENS) -> None:
        self.deadline = deadline
        self.content_left = content
        self.tokens_left = tokens
        self.ops_left = MAX_OPS
        self.glyphs_left = MAX_GLYPHS
        self.font_left = MAX_FONT_BYTES

    def check(self) -> None:
        if time.monotonic() > self.deadline:
            raise PdfUserError("Redacting took too long; nothing was written. Try fewer "
                               "pages at a time")

    def spend_font(self, size: int) -> None:
        self.check()
        self.font_left -= size
        if self.font_left < 0:
            raise Unsure("its fonts are too large to follow")


def content_streams(obj) -> list:
    contents = obj.get("/Contents")
    streams = list(contents) if isinstance(contents, pikepdf.Array) else \
        ([] if contents is None else [contents])
    if not all(isinstance(s, pikepdf.Stream) for s in streams):
        raise Unsure("its content is damaged")
    return streams


def parse(source, streams: list, budget: Budget, operators: str = "") -> list:
    """The instructions of a page or form (only *operators*, if given), measured
    before being parsed."""
    tokens = size = 0
    for stream in streams:
        budget.check()
        data = decode(stream, MAX_CONTENT + 1)
        if data is None:
            raise Unsure("its content can't be read")
        size += len(data)
        if size > MAX_CONTENT or size > budget.content_left:
            raise Unsure("it has too much content")
        tokens += 1 + sum(data.count(c) for c in _WHITESPACE) \
            + 2 * sum(data.count(c) for c in _DELIMITERS)
    if tokens > MAX_STREAM_TOKENS or tokens > budget.tokens_left:
        raise Unsure("it has too much content")
    budget.tokens_left -= tokens
    budget.content_left -= size
    if not streams:
        return []
    try:
        return list(pikepdf.parse_content_stream(source, operators))
    except Exception:  # noqa: BLE001 - malformed content (pikepdf raises several kinds)
        raise Unsure("its content is damaged") from None


def _instruction(operands: list, op: str) -> pikepdf.ContentStreamInstruction:
    return pikepdf.ContentStreamInstruction(operands, pikepdf.Operator(op))


_RESOURCE_IDS = itertools.count()


class Resources:
    """A resource dictionary that is copied before anything in it changes, so the pages
    and forms sharing it are unaffected."""

    def __init__(self, res) -> None:
        self.res = res if isinstance(res, pikepdf.Dictionary) else pikepdf.Dictionary()
        # What names in it mean doesn't change while it is read: fonts can be cached
        # by it and their name, even when written into it directly.
        self.key = ("o", self.res.objgen) if self.res.is_indirect else \
            ("d", next(_RESOURCE_IDS))
        self.private = False
        self._copied: set[str] = set()

    def get(self, group: str, name):
        entries = self.res.get(group)
        if isinstance(entries, pikepdf.Dictionary) and isinstance(name, pikepdf.Name):
            return entries.get(name)
        return None

    def _own(self, group: str) -> pikepdf.Dictionary:
        if not self.private:
            self.res = pikepdf.Dictionary(dict(self.res.items()))
            self.private = True
        if group not in self._copied:
            entries = self.res.get(group)
            self.res[group] = pikepdf.Dictionary(
                dict(entries.items()) if isinstance(entries, pikepdf.Dictionary) else {})
            self._copied.add(group)
        return self.res[group]

    def add(self, group: str, obj, prefix: str) -> pikepdf.Name:
        entries = self._own(group)
        n = 1
        while f"/{prefix}{n}" in entries:
            n += 1
        name = pikepdf.Name(f"/{prefix}{n}")
        entries[name] = obj
        return name


def _risky(pattern) -> bool:
    """Whether a pattern can draw anything but a plain gradient: a tiling pattern's
    cell is content (text, pictures), and mesh or function shadings can be pictures."""
    if not isinstance(pattern, (pikepdf.Dictionary, pikepdf.Stream)):
        return False  # tiling patterns are streams, which aren't Dictionary in pikepdf
    if pattern.get("/PatternType") == 2:
        return _risky_shading(pattern.get("/Shading"))
    return True


def _risky_shading(shading) -> bool:
    return not (isinstance(shading, pikepdf.Dictionary)
                and shading.get("/ShadingType") in (2, 3))


class _State:
    __slots__ = ("ctm", "font", "size", "tc", "tw", "th", "tl", "rise", "tr", "lw", "dashed",
                 "fill_pattern", "stroke_pattern")

    def __init__(self, ctm) -> None:
        self.ctm, self.font, self.size = ctm, None, 0.0
        self.tc = self.tw = self.tl = self.rise = 0.0
        self.th, self.tr, self.lw, self.dashed = 1.0, 0, 1.0, False
        self.fill_pattern = self.stroke_pattern = None

    def copy(self) -> _State:
        other = _State.__new__(_State)
        for key in _State.__slots__:
            setattr(other, key, getattr(self, key))
        return other


class _Path:
    """The path being built, in subpaths: each one's instructions, its points in page
    space (None if they can't be read) and its operators with their numbers (None for
    numbers that can't be read)."""

    def __init__(self, known: bool = True) -> None:
        self.subpaths: list[list] = []   # [instructions, points, [(op, numbers)]]
        self.clip: list = []             # W or W*
        self.known = known

    def add(self, ins, op: str, operands: list, ctm) -> None:
        if op in ("m", "re") or not self.subpaths:
            self.subpaths.append([[], [] if self.known else None, []])
        sub = self.subpaths[-1]
        sub[0].append(ins)
        try:
            numbers = [float(v) for v in operands]
        except (TypeError, ValueError):
            numbers = None
        sub[2].append((op, numbers))
        if sub[1] is not None:
            sub[1] = _path_points(op, operands, ctm, sub[1])

    def instructions(self) -> list:
        return [ins for sub in self.subpaths for ins in sub[0]] + self.clip


def _lone_rect(ops: list) -> list | None:
    """The rectangle a subpath is, if it's just one (re, and perhaps h)."""
    if ops and ops[0][0] == "re" and ops[0][1] is not None and len(ops[0][1]) == 4 and \
            all(op == "h" for op, _ in ops[1:]):
        return ops[0][1]
    return None


def _polyline(ops: list, closed: bool = False) -> list | None:
    """The straight segments of a subpath of m, l and h (closed too if *closed*, as s
    and b close it), or None if it's anything else."""
    rect = _lone_rect(ops)
    if rect is not None:
        x, y, w, h = rect
        corners = [(x, y), (x + w, y), (x + w, y + h), (x, y + h), (x, y)]
        return list(zip(corners, corners[1:], strict=False))
    if not ops or ops[0][0] != "m" or ops[0][1] is None or len(ops[0][1]) != 2:
        return None
    start = last = tuple(ops[0][1])
    segments = []
    for op, numbers in ops[1:]:
        if op == "l" and numbers is not None and len(numbers) == 2:
            segments.append((last, tuple(numbers)))
            last = tuple(numbers)
        elif op == "h":
            if last != start:
                segments.append((last, start))
            last = start
        else:
            return None
    if closed and last != start:
        segments.append((last, start))
    return segments


def _invert(m) -> tuple | None:
    a, b, c, d, e, f = m
    det = a * d - b * c
    if abs(det) < 1e-12:
        return None
    return (d / det, -b / det, -c / det, a / det, (c * f - d * e) / det, (b * e - a * f) / det)


def _outside(rect: tuple, area: tuple) -> list[tuple]:
    """The parts of a rectangle outside another, as up to four rectangles."""
    x0, y0, x1, y1 = rect
    a0, b0, a1, b1 = area
    if a0 >= x1 or a1 <= x0 or b0 >= y1 or b1 <= y0:
        return [rect]
    pieces = [(x0, y0, x1, max(y0, b0)), (x0, min(y1, b1), x1, y1),
              (x0, max(y0, b0), max(x0, a0), min(y1, b1)),
              (min(x1, a1), max(y0, b0), x1, min(y1, b1))]
    return [r for r in pieces if r[2] - r[0] > 1e-9 and r[3] - r[1] > 1e-9]


def _clip_segment(p, q, area) -> tuple[float, float] | None:
    """The part (as parameters 0..1) of segment p-q inside a rectangle, if any."""
    t0, t1 = 0.0, 1.0
    dx, dy = q[0] - p[0], q[1] - p[1]
    for pv, qv in ((-dx, p[0] - area[0]), (dx, area[2] - p[0]),
                   (-dy, p[1] - area[1]), (dy, area[3] - p[1])):
        if pv == 0:
            if qv < 0:
                return None
            continue
        t = qv / pv
        if pv < 0:
            t0 = max(t0, t)
        else:
            t1 = min(t1, t)
        if t0 > t1:
            return None
    return t0, t1


def _cut(ops: list, op: str, ctm, areas: list, pad: tuple) -> list:
    """A subpath crossing areas, without what's under them: a filled rectangle as the
    rectangles around the areas (drawn the same way round, so winding is kept), stroked
    straight lines as the pieces outside them, ending half the line's width before each
    area (*pad*, the stroke's reach around a point). Anything else crossing an area
    could be a signature or lettering: Unsure."""
    fills, strokes = op not in ("S", "s"), op in _STROKE
    if fills and strokes:
        raise Unsure("a drawing is partly under an area")
    if fills:
        rect = _lone_rect(ops)
        inverse = _invert(ctm)
        if rect is None or inverse is None or not (
                (ctm[1] == 0 and ctm[2] == 0) or (ctm[0] == 0 and ctm[3] == 0)):
            raise Unsure("a drawing is partly under an area")
        x, y, w, h = rect
        pieces = [(min(x, x + w), min(y, y + h), max(x, x + w), max(y, y + h))]
        for area in areas:  # in the drawing's own space, still a rectangle
            local = _bbox(inverse, *area)
            pieces = [piece for r in pieces for piece in _outside(r, local)]
            if len(pieces) > MAX_PIECES:
                raise Unsure("a drawing would be cut into too many pieces")
        return [_instruction([round(r[2] if w < 0 else r[0], 4),
                              round(r[3] if h < 0 else r[1], 4),
                              round((r[2] - r[0]) * (-1 if w < 0 else 1), 4),
                              round((r[3] - r[1]) * (-1 if h < 0 else 1), 4)], "re")
                for r in pieces]
    segments = _polyline(ops, closed=op in ("s", "b", "b*"))
    if segments is None:
        raise Unsure("a drawing is partly under an area")
    reach = pad[2]  # half the line width, in page space
    out = []
    if len(segments) * (len(areas) + 1) > MAX_PIECES:
        raise Unsure("a drawing would be cut into too many pieces")
    for p, q in segments:
        a, b = _point(ctm, *p), _point(ctm, *q)
        # Each cut reaches past the area by half the line's width along the line, so a
        # line's end (a round or square cap) doesn't reach back under the area.
        extra = reach / max(math.hypot(b[0] - a[0], b[1] - a[1]), 1e-9)
        cuts = sorted((span[0] - extra, span[1] + extra) for area in areas
                      if (span := _clip_segment(a, b, area)) is not None)
        t = 0.0
        for start, stop in [*cuts, (1.0, 1.0)]:
            if start > t + 1e-9:  # keep t..start
                for point, op_ in (((p[0] + (q[0] - p[0]) * t, p[1] + (q[1] - p[1]) * t), "m"),
                                   ((p[0] + (q[0] - p[0]) * start,
                                     p[1] + (q[1] - p[1]) * start), "l")):
                    out.append(_instruction([round(point[0], 4), round(point[1], 4)], op_))
            t = max(t, stop)
    return out


class Redactor:
    """Rewrites the content of pages (and the forms they draw) without what lies under
    a page's areas. Counts what it removed in *stats*; *gone* collects the originals it
    replaced or dropped (pictures, forms, property lists, content), which must not stay
    reachable elsewhere in the file. *fonts* may be shared between pages."""

    def __init__(self, pdf: pikepdf.Pdf, areas: list, budget: Budget,
                 fonts: dict | None = None) -> None:
        self.pdf, self.areas, self.budget = pdf, areas, budget
        self._index = Areas(areas)
        self.stats = {"text_removed": 0, "images_edited": 0, "images_removed": 0,
                      "drawings_removed": 0}
        self.mcids: set[int] = set()   # marked content on the page holding removed things
        self.gone: set[tuple[int, int]] = set()
        self._fonts = {} if fonts is None else fonts
        self._parsed: dict = {}

    def _forget(self, obj) -> None:
        if isinstance(obj, pikepdf.Object) and obj.is_indirect:
            self.gone.add(obj.objgen)

    def font(self, key, obj) -> Font:
        if key not in self._fonts:
            self._fonts[key] = Font(obj, self.budget)
        return self._fonts[key]

    def page(self, obj: pikepdf.Dictionary, resources, fill: str | None) -> None:
        """Rewrite one page; its own resources if they had to change."""
        res = Resources(resources)
        streams = content_streams(obj)
        ops = parse(obj, streams, self.budget)
        out, depth, in_text = self.content(ops, res, _State(_IDENTITY), 0, frozenset(),
                                           top=True)
        body = pikepdf.unparse_content_stream(out if out is not None else ops)
        tail = (b"ET\n" if in_text else b"") + b"Q\n" * depth
        for stream in streams:
            self._forget(stream)
        obj.Contents = self.pdf.make_stream(b"q\n" + body + b"\n" + tail + b"Q\n"
                                            + boxes(self.areas, fill))
        if res.private or "/Resources" not in obj:
            obj.Resources = res.res

    def content(self, ops: list, res: Resources, gs: _State, depth: int, path: frozenset,
                top: bool) -> tuple:
        """(the content without what's under the areas, or None if nothing is; q left
        open at the end; whether a text object is left open)."""
        areas, budget, stats = self.areas, self.budget, self.stats
        out, stack, changed, in_text = [], [], False, False
        tm = tlm = _IDENTITY
        shape = _Path()
        # Open marked content: [index, operator, something removed, something kept].
        marks: list = []

        def touch(removed: bool = True, kept: bool = False) -> None:
            for mark in marks:
                mark[2] = mark[2] or removed
                mark[3] = mark[3] or kept

        for n, ins in enumerate(ops):
            budget.ops_left -= 1
            if budget.ops_left < 0:
                raise Unsure("it has too much content")
            if not n & 0xFFF:
                budget.check()
            if isinstance(ins, pikepdf.ContentStreamInlineImage):
                box = _bbox(gs.ctm, 0, 0, 1, 1)
                if _overlaps(box, areas):
                    if not _within(box, areas):
                        raise Unsure("a picture stored in its content is partly under an area")
                    stats["images_removed"] += 1
                    changed = True
                    touch()
                    continue
                out.append(ins)
                continue
            op, operands = str(ins.operator), list(ins.operands)
            if op in _PATH:
                shape.add(ins, op, operands, gs.ctm)
                continue
            if op in ("W", "W*"):
                shape.clip.append(ins)
                continue
            if op in _PAINT or op == "n":
                painted = self._paint(shape, ins, op, gs)
                if painted is not None:
                    changed = True
                    touch()
                    out += painted
                else:
                    out += [*shape.instructions(), ins]
                shape = _Path()
                continue
            if shape.subpaths or shape.clip:  # an operator inside a path: keep it as it is
                out += shape.instructions()
                shape = _Path(known=False)
            if op == "q":
                stack.append(gs.copy())
            elif op == "Q":
                if not stack:
                    if top:   # an unmatched Q does nothing; dropped so the end can close
                        continue
                else:
                    gs = stack.pop()
            elif op == "cm":
                gs.ctm = _mul(_matrix(operands), gs.ctm)
            elif op == "d" and operands:
                gs.dashed = isinstance(operands[0], pikepdf.Array) and len(operands[0]) > 0
            elif op == "w" and operands:
                gs.lw = abs(_num(operands[0]))
            elif op in ("cs", "CS", "g", "G", "rg", "RG", "k", "K"):
                if op in ("cs", "g", "rg", "k"):
                    gs.fill_pattern = None
                else:
                    gs.stroke_pattern = None
            elif op in ("scn", "SCN") and operands and isinstance(operands[-1], pikepdf.Name):
                pattern = res.get("/Pattern", operands[-1])
                if op == "scn":
                    gs.fill_pattern = pattern
                else:
                    gs.stroke_pattern = pattern
            elif op == "sh" and operands:
                shading = res.get("/Shading", operands[0])
                if _risky_shading(shading):
                    box = _rect(shading.get("/BBox")) if isinstance(
                        shading, pikepdf.Dictionary) else None
                    if box is None or _overlaps(_bbox(gs.ctm, *box), areas):
                        raise Unsure("a shading that can hold a picture is under an area")
            elif op == "gs" and operands:
                state = res.get("/ExtGState", operands[0])
                if isinstance(state, pikepdf.Dictionary):
                    if "/LW" in state:
                        gs.lw = abs(_num(state.LW))
                    font = state.get("/Font")
                    if isinstance(font, pikepdf.Array) and len(font) == 2:
                        key = ("o", font[0].objgen) if isinstance(font[0], pikepdf.Object) \
                            and font[0].is_indirect else (*res.key, "gs", str(operands[0]))
                        gs.font, gs.size = (key, font[0]), _num(font[1])
            elif op == "BT":
                tm = tlm = _IDENTITY
                in_text = True
            elif op == "ET":
                in_text = False
            elif op == "Tf" and len(operands) == 2:
                font = res.get("/Font", operands[0])
                key = ("o", font.objgen) if isinstance(font, pikepdf.Object) and \
                    font.is_indirect else (*res.key, "Tf", str(operands[0]))
                gs.font, gs.size = (key, font), _num(operands[1])
            elif op in ("Tc", "Tw", "Tz", "TL", "Ts", "Tr") and operands:
                value = _num(operands[0])
                if op == "Tc":
                    gs.tc = value
                elif op == "Tw":
                    gs.tw = value
                elif op == "Tz":
                    gs.th = value / 100
                elif op == "TL":
                    gs.tl = value
                elif op == "Ts":
                    gs.rise = value
                else:
                    gs.tr = int(value)
            elif op in ("Td", "TD") and len(operands) == 2:
                tx, ty = _num(operands[0]), _num(operands[1])
                if op == "TD":
                    gs.tl = -ty
                tlm = _mul((1.0, 0.0, 0.0, 1.0, tx, ty), tlm)
                tm = tlm
            elif op == "Tm":
                tm = tlm = _matrix(operands)
            elif op == "T*":
                tlm = _mul((1.0, 0.0, 0.0, 1.0, 0.0, -gs.tl), tlm)
                tm = tlm
            elif op in _SHOW and operands:
                before = []
                if op in ("'", '"'):
                    if op == '"':
                        if len(operands) != 3:
                            raise Unsure("its content has an invalid text operator")
                        gs.tw, gs.tc = _num(operands[0]), _num(operands[1])
                        before = [_instruction([operands[0]], "Tw"),
                                  _instruction([operands[1]], "Tc")]
                    tlm = _mul((1.0, 0.0, 0.0, 1.0, 0.0, -gs.tl), tlm)
                    tm = tlm
                    before.append(_instruction([], "T*"))
                items = operands[-1]
                items = list(items) if isinstance(items, pikepdf.Array) else [items]
                shown, tm, kept = self._show(items, gs, tm)
                touch(shown is not None, kept)
                if shown is not None:
                    changed = True
                    out += [*before, _instruction([pikepdf.Array(shown)], "TJ")]
                    continue
            elif op == "Do" and operands:
                drawn = res.get("/XObject", operands[0])
                replaced = self._draw(operands[0], res, gs, depth, path)
                if replaced is not None and isinstance(drawn, pikepdf.Stream) and \
                        drawn.get("/Subtype") == pikepdf.Name.Form:
                    touch(False, True)  # a form may keep text of its own
                if replaced is not False:
                    changed = True
                    touch()
                    if replaced is not None:
                        out.append(_instruction([replaced], "Do"))
                    continue
            elif op in ("BDC", "BMC"):
                marks.append([len(out), ins, False, False])
            elif op == "EMC" and marks:
                self._close(marks.pop(), out, res, top)
            out.append(ins)
        out += shape.instructions()
        while marks:
            self._close(marks.pop(), out, res, top)
        return (out if changed else None), len(stack), in_text

    def _paint(self, shape: _Path, ins, op: str, gs: _State) -> list | None:
        """A painted path without what's under the areas (None if nothing is): subpaths
        wholly under one are dropped, those crossing one are cut (see _cut). A clipping
        path crossing an area must be a plain rectangle: lettering or a signature used
        as a clip would keep its shape in the file."""
        areas = self.areas
        for sub in shape.subpaths:
            if sub[1] is None:
                raise Unsure("a drawing it can't read is on a page with areas")
        if not shape.subpaths and not shape.known and op != "n":
            raise Unsure("a drawing it can't read is on a page with areas")
        if shape.clip:
            for _, points, ops in shape.subpaths:
                if points and _overlaps(_spread(points, gs, "n"), areas) and \
                        _lone_rect(ops) is None:
                    raise Unsure("a clipping shape is under an area")
        if op == "n":
            return None
        fills, strokes = op not in ("S", "s"), op in _STROKE
        patterns = [p for p in ((gs.fill_pattern if fills else None),
                                (gs.stroke_pattern if strokes else None)) if p is not None]
        out, changed = [], 0
        for instructions, points, ops in shape.subpaths:
            box = _spread(points, gs, op) if points else None
            if box is None or not _overlaps(box, areas):
                out += instructions
            elif _within(box, areas):
                changed += 1
            elif any(_risky(p) for p in patterns):
                raise Unsure("a drawing filled with a pattern is partly under an area")
            else:
                self.budget.check()
                if gs.dashed and op in _STROKE:
                    raise Unsure("a dashed line is partly under an area")
                out += _cut(ops, op, gs.ctm, self._index.near(box), _spread([(0, 0)], gs, op))
                changed += 1
        if not changed:
            return None
        for pattern in patterns:  # what the removed drawing was filled with
            self._forget(pattern)
        self.stats["drawings_removed"] += changed
        if out:
            out.append(ins)
        if shape.clip:  # the whole path still clips what follows (after painting)
            out += [*shape.instructions(), _instruction([], "n")]
        return out

    def _show(self, items: list, gs: _State, tm) -> tuple:
        """A text-showing operator's strings with the characters under the areas taken
        out (None if none are), the text matrix after it, and whether any are kept."""
        if gs.font is None:
            raise Unsure("text uses a font that is missing")
        font = self.font(*gs.font)
        size, th = gs.size, gs.th
        base = _mul(tm, gs.ctm)
        middle = (font.ascent + font.descent) / 2 * size + gs.rise
        low, high = font.descent * size + gs.rise, font.ascent * size + gs.rise
        shown, run, x, removed, kept = [], bytearray(), 0.0, False, False
        for item in items:
            if isinstance(item, pikepdf.String):
                for code, width, spaced in font.codes(bytes(item)):
                    self.budget.glyphs_left -= 1
                    if self.budget.glyphs_left < 0:
                        raise Unsure("it has too much text")
                    if not self.budget.glyphs_left & 0xFFF:
                        self.budget.check()
                    advance = (width * size + gs.tc + (gs.tw if spaced else 0.0)) * th
                    cx, cy = _point(base, x + width * size * th / 2, middle)
                    if self._index.hold(cx, cy):
                        if gs.tr >= 4:
                            raise Unsure("text that clips other content is under an area")
                        if not size * th:
                            raise Unsure("text of size zero is under an area")
                        if run:
                            shown.append(pikepdf.String(bytes(run)))
                            run = bytearray()
                        shown.append(-advance * 1000 / (size * th))
                        removed = True
                        self.stats["text_removed"] += 1
                    else:
                        self._partly(font, base, x, width * size * th, low, high, size, th,
                                     gs.rise)
                        run += code
                        kept = True
                    x += advance
            else:
                try:
                    adjust = _num(item)
                except Unsure:
                    continue  # readers skip what isn't a string or number
                if run:
                    shown.append(pikepdf.String(bytes(run)))
                    run = bytearray()
                shown.append(adjust)
                x -= adjust / 1000 * size * th
        if run:
            shown.append(pikepdf.String(bytes(run)))
        tm = _mul((1.0, 0.0, 0.0, 1.0, x, 0.0), tm)
        if not removed:
            return None, tm, kept
        merged: list = []
        for item in shown:  # neighbouring numbers become one
            if merged and not isinstance(item, pikepdf.String) and \
                    not isinstance(merged[-1], pikepdf.String):
                merged[-1] += item
            else:
                merged.append(item)
        return [round(v, 3) if not isinstance(v, pikepdf.String) else v
                for v in merged], tm, kept

    def _partly(self, font: Font, base, x: float, width: float, low: float, high: float,
                size: float, th: float, rise: float) -> None:
        """Refuse a kept character largely under an area (its centre just outside), or a
        Type 3 character whose drawing reaches under one."""
        if font.type3:
            box = font.box or (-1.0, -1.0, 2.0, 2.0)  # no box given: assume a large one
            glyph = _bbox(base, x + box[0] * size * th, box[1] * size + rise,
                          x + box[2] * size * th, box[3] * size + rise)
            if _overlaps(glyph, self.areas):
                raise Unsure("a Type 3 character is partly under an area")
            return
        glyph = _bbox(base, x, low, x + width, high)
        area = (glyph[2] - glyph[0]) * (glyph[3] - glyph[1])
        if area <= 0:
            return
        for a in self._index.near(glyph):
            w = min(glyph[2], a[2]) - max(glyph[0], a[0])
            h = min(glyph[3], a[3]) - max(glyph[1], a[1])
            if w > 0 and h > 0 and w * h >= PARTLY * area:
                raise Unsure("a character is partly under an area")

    def _draw(self, name, res: Resources, gs: _State, depth: int, path: frozenset):
        """For a Do: False if it stays as it is, None to drop it, or the name of a
        redacted copy to draw instead."""
        xobject = res.get("/XObject", name)
        if not isinstance(xobject, pikepdf.Stream):
            return False
        subtype = xobject.get("/Subtype")
        if subtype == pikepdf.Name.Image:
            box = _bbox(gs.ctm, 0, 0, 1, 1)
            if not _overlaps(box, self.areas):
                return False
            if _within(box, self.areas):
                self.stats["images_removed"] += 1
                self._forget(xobject)
                return None
            copy = edit_image(self.pdf, xobject, gs.ctm, self.areas)
            if copy is None:
                return False
            self.stats["images_edited"] += 1
            self._forget(xobject)
            return res.add("/XObject", copy, "RdIm")
        if subtype != pikepdf.Name.Form:
            return False
        matrix = _matrix(xobject.get("/Matrix", pikepdf.Array(_IDENTITY)))
        ctm = _mul(matrix, gs.ctm)
        bbox = _rect(xobject.get("/BBox"))
        box = _bbox(ctm, *bbox) if bbox is not None else (-math.inf, -math.inf,
                                                           math.inf, math.inf)
        if not _overlaps(box, self.areas):
            return False
        if _within(box, self.areas):
            self.stats["drawings_removed"] += 1
            self._forget(xobject)
            return None
        key = xobject.objgen
        if key in path or depth + 1 > MAX_FORM_DEPTH:
            raise Unsure("forms inside forms go too deep")
        if key not in self._parsed:
            self._parsed[key] = parse(xobject, [xobject], self.budget)
        own = xobject.get("/Resources")
        inner = Resources(own if isinstance(own, pikepdf.Dictionary) else res.res)
        state = gs.copy()
        state.ctm = ctm
        out, _, _ = self.content(self._parsed[key], inner, state, depth + 1,
                                 path | {key}, top=False)
        if out is None:
            return False
        copy = self.pdf.make_stream(pikepdf.unparse_content_stream(out))
        for k, v in xobject.stream_dict.items():
            if k not in _NOT_COPIED and k != "/Resources":
                copy[k] = v
        if isinstance(own, pikepdf.Dictionary) or inner.private:
            copy.Resources = inner.res
        self._forget(xobject)
        return res.add("/XObject", copy, "RdFm")

    def _close(self, mark: list, out: list, res: Resources, top: bool) -> None:
        """Drop the text stand-ins of marked content that held something removed. If it
        kept some of its text too, the stand-in described all of it: what's left would
        read as something else, so the page is turned into a picture instead."""
        index, ins, touched, kept_some = mark
        if not touched or str(ins.operator) != "BDC" or len(ins.operands) != 2:
            return
        tag, props = ins.operands
        named = isinstance(props, pikepdf.Name)
        found = res.get("/Properties", props) if named else props
        if not isinstance(found, pikepdf.Dictionary):
            return
        mcid = found.get("/MCID")
        if top and isinstance(mcid, int):
            self.mcids.add(int(mcid))
        if not any(k in found for k in _STAND_INS):
            return
        if kept_some and "/ActualText" in found:
            raise Unsure("text with replacement text (ActualText) is only partly under an "
                         "area")
        kept = pikepdf.Dictionary({k: v for k, v in found.items() if k not in _STAND_INS})
        if named:
            self._forget(found)
            kept = res.add("/Properties", self.pdf.make_indirect(kept), "RdP")
        out[index] = _instruction([tag, kept], "BDC")


def _path_points(op: str, operands: list, ctm, points: list):
    """The subpath's points so far (in page space) with this operator's; None if they
    can't be read."""
    try:
        values = [float(v) for v in operands]
    except (TypeError, ValueError):
        return None
    if op == "re" and len(values) == 4:
        x, y, w, h = values
        corners = [(x, y), (x + w, y), (x, y + h), (x + w, y + h)]
    elif op != "re" and len(values) % 2 == 0 and len(values) <= 6:
        corners = list(zip(values[::2], values[1::2], strict=True))
    else:
        return None
    if not all(math.isfinite(v) for v in values):
        return None
    return points + [_point(ctm, x, y) for x, y in corners]


def _spread(points: list, gs: _State, op: str) -> tuple:
    """The box a painted path covers, with half the line width around a stroke."""
    xs, ys = zip(*points, strict=True)
    pad = 0.0
    if op in _STROKE:
        a, b, c, d = gs.ctm[:4]
        pad = gs.lw / 2 * math.sqrt(abs(a * d - b * c)) if gs.lw else 0.5
    return min(xs) - pad, min(ys) - pad, max(xs) + pad, max(ys) + pad


def boxes(areas: list, fill: str | None) -> bytes:
    """Content painting the areas in the fill colour (nothing if fill is None)."""
    if fill is None or not areas:
        return b""
    shade = {"black": "0", "white": "1"}[fill]
    rects = " ".join(f"{a[0]:.3f} {a[1]:.3f} {a[2] - a[0]:.3f} {a[3] - a[1]:.3f} re"
                     for a in areas)
    return f"q {shade} g {rects} f Q\n".encode()


# --- pictures ---------------------------------------------------------------------------


def _clip(poly: list, y: float, keep_above: bool) -> list:
    """The part of a convex polygon above (or below) the line at height y."""
    out = []
    for i, (x0, y0) in enumerate(poly):
        x1, y1 = poly[(i + 1) % len(poly)]
        in0 = y0 >= y if keep_above else y0 <= y
        in1 = y1 >= y if keep_above else y1 <= y
        if in0:
            out.append((x0, y0))
        if in0 != in1 and y1 != y0:
            out.append((x0 + (x1 - x0) * (y - y0) / (y1 - y0), y))
    return out


def pixel_boxes(ctm, areas: list, width: int, height: int) -> list[tuple]:
    """The pixels of a picture drawn with *ctm* that lie under the areas, as boxes
    (x0, y0, x1, y1), rows from the top. Every pixel an area touches is included."""
    a, b, c, d, e, f = ctm
    det = a * d - b * c
    if abs(det) < 1e-12:
        return []
    inv = (d / det, -b / det, -c / det, a / det, (c * f - d * e) / det, (b * e - a * f) / det)
    found = []
    for area in areas:
        poly = []
        for x, y in ((area[0], area[1]), (area[2], area[1]), (area[2], area[3]),
                     (area[0], area[3])):
            u, v = _point(inv, x, y)
            poly.append((u * width, (1 - v) * height))
        top = max(0, math.floor(min(p[1] for p in poly)))
        bottom = min(height, math.ceil(max(p[1] for p in poly)))
        rows = []
        for row in range(top, bottom):
            band = _clip(_clip(poly, row, True), row + 1, False)
            if not band:
                continue
            x0 = max(0, math.floor(min(p[0] for p in band)))
            x1 = min(width, math.ceil(max(p[0] for p in band)))
            if x1 > x0:
                if rows and rows[-1][0] == x0 and rows[-1][2] == x1 and rows[-1][3] == row:
                    rows[-1] = (x0, rows[-1][1], x1, row + 1)
                else:
                    rows.append((x0, row, x1, row + 1))
        found += rows
    return found


def _components(space) -> int | None:
    if isinstance(space, pikepdf.Name):
        return _COMPONENTS.get(str(space))
    if isinstance(space, pikepdf.Array) and len(space):
        family = str(space[0])
        if family == "/ICCBased" and len(space) > 1 and isinstance(space[1], pikepdf.Stream):
            n = space[1].get("/N")
            return int(n) if isinstance(n, int) and n in (1, 3, 4) else None
        if family == "/DeviceN" and len(space) > 1 and isinstance(space[1], pikepdf.Array):
            return len(space[1]) or None
        return _COMPONENTS.get(family)
    return None


def _clear_bits(data: bytearray, start: int, first: int, end: int) -> None:
    """Set bits first..end-1 (counted from *start*, the row's first byte) to 0."""
    if end <= first:
        return
    lo, hi = start + first // 8, start + (end - 1) // 8
    head, tail = 0xFF >> (first % 8), (0xFF << (7 - (end - 1) % 8)) & 0xFF
    if lo == hi:
        data[lo] &= ~(head & tail) & 0xFF
        return
    data[lo] &= ~head & 0xFF
    data[hi] &= ~tail & 0xFF
    data[lo + 1:hi] = bytes(hi - lo - 1)


def _copy(pdf: pikepdf.Pdf, image: pikepdf.Stream, data: bytes, codec: str):
    copy = pikepdf.Stream(pdf, data)
    for k, v in image.stream_dict.items():
        if k not in _NOT_COPIED:
            copy[k] = v
    copy.Filter = pikepdf.Name(codec)
    return copy


def _blank(pdf: pikepdf.Pdf, image: pikepdf.Stream, found: list, mask: bool):
    """A copy of the picture with the pixels in *found* set to 0 in every channel."""
    filters = filters_of(image)
    codec = filters[-1] if filters else None
    if codec == "/DCTDecode":
        if "/Decode" in image:
            raise Unsure("a JPEG picture under an area has unusual colour settings")
        size = _size(image)
        data = encoded_image(image, size[0] * size[1] * 4 + 2**20)
        try:
            with Image.open(io.BytesIO(data or b"")) as picture:
                picture.load()
                if picture.mode not in ("L", "RGB"):
                    raise Unsure("a CMYK JPEG picture is under an area")
                edited = picture.copy()
        except Unsure:
            raise
        except Exception:  # noqa: BLE001 - any failure to decode it
            raise Unsure("a JPEG picture under an area can't be read") from None
        for x0, y0, x1, y1 in found:
            edited.paste(0, (x0, y0, x1, y1))
        out = io.BytesIO()
        edited.save(out, "JPEG", quality=92)
        return _copy(pdf, image, out.getvalue(), "/DCTDecode")
    if any(f not in _GENERAL for f in filters):
        what = "a mask" if mask else "a picture"
        raise Unsure(f"{what} under an area uses {codec[1:]} compression, which can't be "
                     "edited here")
    width, height = _size(image)
    if image.get("/ImageMask") is True:
        bits, channels = 1, 1
    else:
        bits, channels = image.get("/BitsPerComponent", 8), _components(image.get("/ColorSpace"))
    if bits not in (1, 2, 4, 8, 16) or channels is None:
        raise Unsure("a picture under an area has a layout this can't edit")
    try:
        data = bytearray(image.read_bytes())
    except (pikepdf.PdfError, RuntimeError, ValueError):
        raise Unsure("a picture under an area is damaged") from None
    row = (width * channels * bits + 7) // 8
    if len(data) < row * height:
        data.extend(bytes(row * height - len(data)))  # short data reads as zeros
    del data[row * height:]
    for x0, y0, x1, y1 in found:
        for y in range(y0, y1):
            _clear_bits(data, y * row, x0 * channels * bits, x1 * channels * bits)
    return _copy(pdf, image, zlib.compress(bytes(data)), "/FlateDecode")


def edit_image(pdf: pikepdf.Pdf, image: pikepdf.Stream, ctm, areas: list):
    """A copy of a picture (and its masks) with what's under the areas blanked; None if
    no pixel is."""
    problem = _problem(image)
    if problem:
        raise Unsure(f"a picture under an area can't be edited: {problem}")
    width, height = _size(image)
    found = pixel_boxes(ctm, areas, width, height)
    if not found:
        return None
    copy = _blank(pdf, image, found, mask=False)
    for key in ("/SMask", "/Mask"):
        mask = image.get(key)
        if isinstance(mask, pikepdf.Stream):
            mw, mh = _size(mask)
            under = pixel_boxes(ctm, areas, mw, mh)
            copy[key] = _blank(pdf, mask, under, mask=True) if under else mask
    return copy
