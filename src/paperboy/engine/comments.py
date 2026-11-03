"""list_comments / add_comments: review comments on a PDF's pages.

Comments are annotations: sticky notes, highlights and other text markup, text boxes,
shapes, stamps. list_comments reads them; add_comments adds highlights, underlines,
strikeouts and squiggles (over text it finds, as search_text finds it, or over given
rectangles), sticky notes, text boxes and replies.

Every comment added carries its own appearance, so it looks the same in every viewer
(and prints), with the comment text, author and dates where viewers show them. Each
comment has an id, "p<page>-<n>" (its place in the page's list of annotations), that
a reply refers to; adding comments appends them, so ids already given stay valid.
"""
from __future__ import annotations

import io
import math
import re
import secrets
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pikepdf
import pypdfium2 as pdfium
from reportlab.lib.utils import simpleSplit
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen import canvas

from ..config import Settings
from ..errors import PdfUserError
from ..pagespec import parse_pages
from ..results import AddCommentsResult, CommentsResult
from .base import (
    inherit_protection,
    open_pdf,
    refuse_signed,
    require_annotation_permission,
    require_copy_permission,
)
from .render import _PDFIUM_LOCK, _close, _open
from .search import MAX_PAGE_CONTENT, _content_size, _page_matches, _query
from .stamp import _color, _latin, _rotation, _visible_box

MARKUP = {"highlight": "/Highlight", "underline": "/Underline", "strikeout": "/StrikeOut",
          "squiggly": "/Squiggly"}
KINDS = (*MARKUP, "note", "text_box", "reply")
# What list_comments reports, by subtype; links, form fields and popups aren't comments.
LISTED = {"/Text": "note", "/FreeText": "text_box", "/Highlight": "highlight",
          "/Underline": "underline", "/StrikeOut": "strikeout", "/Squiggly": "squiggly",
          "/Square": "square", "/Circle": "circle", "/Line": "line", "/Polygon": "polygon",
          "/PolyLine": "polyline", "/Ink": "ink", "/Stamp": "stamp", "/Caret": "caret",
          "/FileAttachment": "file", "/Sound": "sound", "/Redact": "redaction"}
DEFAULT_COLORS = {"highlight": "#ffd400", "underline": "#2f6fde", "strikeout": "#cc0000",
                  "squiggly": "#18794e", "note": "#ffd400", "text_box": "#000000",
                  "reply": "#ffd400"}
MAX_COMMENTS = 100              # comments one add_comments call may add
MAX_MATCHES = 100               # places one comment's text may mark
MAX_RECTS = 50                  # rectangles given for one comment
MAX_TEXT = 10_000               # characters of a comment's text
MAX_BOX_TEXT = 2_000            # characters drawn in a text box
MAX_AUTHOR = 200
MAX_LISTED = 1_000              # comments list_comments returns
MAX_SCANNED = 100_000           # annotations list_comments looks at
MAX_FIELD = 5_000               # characters of each text it returns
MAX_QUADS = 50                  # rectangles read for one comment's marked text
MAX_MARKED_RECTS = 2_000        # rectangles read for marked text, per call
MARKED_TIME_LIMIT_S = 20        # reading marked text, per call
MAX_COORD = 14_400              # points: the largest page PDF allows (200 inches)
FIND_TIME_LIMIT_S = 60          # finding texts, per call
NOTE_SIZE = 20                  # points: a sticky note's icon
MAX_WAVES = 400                 # strokes in one squiggle
FLAG_PRINT, FLAG_NO_ZOOM, FLAG_NO_ROTATE = 4, 8, 16
_ID = re.compile(r"p(\d{1,6})-(\d{1,6})")
_SURROGATE = re.compile("[\ud800-\udfff]")  # half a character: not text


# --- dates, text and colour ------------------------------------------------------------


def pdf_date(when: datetime) -> str:
    offset = when.utcoffset() or timedelta(0)
    minutes = int(offset.total_seconds() // 60)
    sign = "+" if minutes >= 0 else "-"
    return when.strftime("D:%Y%m%d%H%M%S") + \
        f"{sign}{abs(minutes) // 60:02d}'{abs(minutes) % 60:02d}'"


_DATE = re.compile(r"D?:?(\d{4})(\d{2})?(\d{2})?(\d{2})?(\d{2})?(\d{2})?"
                   r"(?:([Zz+-])(\d{2})?'?(\d{2})?'?)?")


def iso_date(value) -> str | None:
    """A PDF date as ISO 8601; None if it isn't one."""
    text = _string(value, 64)
    found = _DATE.fullmatch(text.strip()) if text else None
    if not found:
        return None
    year, month, day, hour, minute, second, sign, off_h, off_m = found.groups()
    try:
        when = datetime(int(year), int(month or 1), int(day or 1), int(hour or 0),
                        int(minute or 0), int(second or 0))
        if sign in ("+", "-"):
            offset = timedelta(hours=int(off_h or 0), minutes=int(off_m or 0))
            when = when.replace(tzinfo=timezone(offset if sign == "+" else -offset))
        elif sign:
            when = when.replace(tzinfo=timezone.utc)
    except ValueError:  # month 13, an offset of 30 hours, ...
        return None
    return when.isoformat()


def _string(value, limit: int = MAX_FIELD) -> str | None:
    if not isinstance(value, pikepdf.String):
        return None
    raw = bytes(value)
    if len(raw) > 4 * limit + 2:  # decode no more than can be kept (UTF-16: whole units)
        value = pikepdf.String(raw[:4 * limit + 2])
    text = str(value)
    return text if len(text) <= limit else text[:limit - 1] + "\u2026"


class _Strings:
    """_string for an annotation's entries, decoding a string object that many
    annotations share only once."""

    def __init__(self):
        self.seen: dict[tuple[tuple[int, int], int], str | None] = {}

    def __call__(self, annot: pikepdf.Dictionary, key: str, limit: int = MAX_FIELD):
        value = annot.get(key)
        if not isinstance(value, pikepdf.Object) or not value.is_indirect:
            return _string(value, limit)
        cached = (value.objgen, limit)
        if cached not in self.seen:
            self.seen[cached] = _string(value, limit)
        return self.seen[cached]


def _hex(value) -> str | None:
    """An annotation's /C as #rrggbb (gray and RGB; CMYK roughly)."""
    if not isinstance(value, pikepdf.Array) or len(value) not in (1, 3, 4):
        return None
    try:
        parts = [min(max(float(v), 0.0), 1.0) for v in value]
    except (TypeError, ValueError):
        return None
    if len(parts) == 1:
        parts *= 3
    elif len(parts) == 4:
        c, m, y, k = parts
        parts = [(1 - c) * (1 - k), (1 - m) * (1 - k), (1 - y) * (1 - k)]
    return "#" + "".join(f"{round(p * 255):02x}" for p in parts)


def _rgb(value: str | None, kind: str) -> tuple[float, float, float]:
    color = _color(value if value is not None else DEFAULT_COLORS[kind])
    return color.red, color.green, color.blue


# --- list_comments ---------------------------------------------------------------------


def _rect(value) -> list[float] | None:
    try:
        x0, y0, x1, y1 = (float(v) for v in value)
    except (TypeError, ValueError):
        return None
    return [round(min(x0, x1), 2), round(min(y0, y1), 2), round(max(x0, x1), 2),
            round(max(y0, y1), 2)]


def _quads(annot: pikepdf.Dictionary) -> list[list[float]]:
    """The rectangles a text markup annotation covers ([left, bottom, right, top])."""
    points = annot.get("/QuadPoints")
    if not isinstance(points, pikepdf.Array) or len(points) % 8 or len(points) > 8 * 500:
        rect = _rect(annot.get("/Rect"))
        return [rect] if rect else []
    try:
        values = [float(v) for v in points]
    except (TypeError, ValueError):
        return []
    rects = []
    for n in range(0, len(values), 8):
        xs, ys = values[n:n + 8:2], values[n + 1:n + 8:2]
        rects.append([min(xs), min(ys), max(xs), max(ys)])
    return rects


def _annotations(pdf: pikepdf.Pdf, targets) -> list[tuple[int, int, pikepdf.Dictionary]]:
    """(page index, position in /Annots (1-based), annotation) for each annotation on
    *targets*, at most MAX_SCANNED."""
    found = []
    for index in targets:
        annots = pdf.pages[index].obj.get("/Annots")
        if not isinstance(annots, pikepdf.Array):
            continue
        for n, annot in enumerate(annots, 1):
            if len(found) >= MAX_SCANNED:
                raise PdfUserError(f"This PDF has over {MAX_SCANNED:,} annotations; list "
                                   "fewer pages")
            if isinstance(annot, pikepdf.Dictionary):
                found.append((index, n, annot))
    return found


def _ids(pdf: pikepdf.Pdf) -> dict[tuple[int, int], str]:
    """Each comment object's id, from every page (so a reply's parent is found wherever
    it is); one listed twice keeps its first."""
    ids: dict[tuple[int, int], str] = {}
    for index, n, annot in _annotations(pdf, range(len(pdf.pages))):
        if annot.is_indirect and str(annot.get("/Subtype")) in LISTED:
            ids.setdefault(annot.objgen, f"p{index + 1}-{n}")
    return ids


def _pdfium_bytes(pdf: pikepdf.Pdf, data: bytes) -> bytes:
    """What PDFium reads: the file itself, or a decrypted copy (it never sees a
    password)."""
    if not pdf.is_encrypted:
        return data
    plain = io.BytesIO()
    pdf.save(plain)  # without encryption=, the copy is decrypted
    return plain.getvalue()


def _marked_texts(pdf: pikepdf.Pdf, data: bytes,
                  marked: list[tuple[int, pikepdf.Dictionary, dict]]) -> bool:
    """Fill in marked_text for text markup comments; whether every one was read. Each
    page's text is read once; the rectangles read and the time taken are bounded."""
    doc = _open(_pdfium_bytes(pdf, data))
    deadline = time.monotonic() + MARKED_TIME_LIMIT_S
    budget = MAX_MARKED_RECTS
    complete = True
    by_page: dict[int, list[tuple[pikepdf.Dictionary, dict]]] = {}
    for index, annot, entry in marked:
        by_page.setdefault(index, []).append((annot, entry))
    try:
        for index, items in by_page.items():
            size = _content_size(pdf.pages[index], MAX_PAGE_CONTENT, {})
            if size is None or size > MAX_PAGE_CONTENT or time.monotonic() > deadline:
                complete = False
                continue
            with _PDFIUM_LOCK:
                try:
                    page = doc[index]
                except pdfium.PdfiumError:
                    complete = False
                    continue
                textpage = page.get_textpage()
            try:
                for annot, entry in items:
                    rects = _quads(annot)[:MAX_QUADS]
                    if len(rects) > budget or time.monotonic() > deadline:
                        complete = False
                        break
                    budget -= len(rects)
                    with _PDFIUM_LOCK:  # held for one comment at a time
                        parts = [textpage.get_text_bounded(*r).strip() for r in rects]
                    entry["marked_text"] = re.sub(
                        r"\s+", " ", " ".join(p for p in parts if p))[:MAX_FIELD]
            finally:
                with _PDFIUM_LOCK:
                    textpage.close()
                    page.close()
    finally:
        _close(doc)
    return complete


def list_comments(path: Path, settings: Settings, pages: str | None = None,
                  password: str | None = None) -> CommentsResult:
    """The comments on all pages, or the pages given, in page order."""
    data = path.read_bytes()
    with open_pdf(data, settings, password) as pdf:
        total = len(pdf.pages)
        targets = parse_pages(pages, total, dedupe=True) if pages else list(range(total))
        # Comment text, like form values, is the document's content: when copying is
        # forbidden, only the owner password gives it.
        can_copy = not pdf.is_encrypted or pdf.owner_password_matched or pdf.allow.extract
        ids = _ids(pdf)
        strings = _Strings()
        found: list[tuple[int, pikepdf.Dictionary, dict]] = []
        truncated = False
        for index, n, annot in _annotations(pdf, targets):
            kind = LISTED.get(str(annot.get("/Subtype")))
            if kind is None:
                continue
            if len(found) >= MAX_LISTED:
                truncated = True
                break
            own = f"p{index + 1}-{n}"
            parent = annot.get("/IRT")
            reply_to = ids.get(parent.objgen) if isinstance(parent, pikepdf.Dictionary) \
                and parent.is_indirect else None
            found.append((index, annot, {
                "id": own, "page": index + 1, "type": kind,
                "author": strings(annot, "/T", MAX_AUTHOR),
                "comment": strings(annot, "/Contents") if can_copy else None,
                "subject": strings(annot, "/Subj", MAX_AUTHOR) if can_copy else None,
                "created": iso_date(annot.get("/CreationDate")),
                "modified": iso_date(annot.get("/M")),
                "color": _hex(annot.get("/C")), "rect": _rect(annot.get("/Rect")),
                "reply_to": reply_to if reply_to != own else None, "marked_text": None}))
        marked = [item for item in found if item[2]["type"] in MARKUP]
        complete = _marked_texts(pdf, data, marked) if marked and can_copy else True
    comments = [entry for _, _, entry in found]
    return {"path": str(path), "comments": comments, "count": len(comments),
            "truncated": truncated, "text_withheld": bool(found) and not can_copy,
            "marked_text_complete": complete and (can_copy or not marked)}


# --- appearances -----------------------------------------------------------------------


def _number(value: float) -> str:
    return f"{value:.3f}".rstrip("0").rstrip(".")


def _appearance(pdf: pikepdf.Pdf, bbox, content: str, *, multiply: bool = False
                ) -> pikepdf.Stream:
    """A Form XObject drawing *content*; with multiply, it darkens what is beneath
    instead of covering it (as a highlighter does)."""
    resources = pikepdf.Dictionary()
    if multiply:
        resources.ExtGState = pikepdf.Dictionary(GS0=pikepdf.Dictionary(
            Type=pikepdf.Name.ExtGState, BM=pikepdf.Name.Multiply))
        content = "/GS0 gs " + content
    stream = pikepdf.Stream(pdf, content.encode("ascii"))
    stream.Type = pikepdf.Name.XObject
    stream.Subtype = pikepdf.Name.Form
    stream.BBox = pikepdf.Array([float(v) for v in bbox])
    stream.Resources = resources
    return pdf.make_indirect(stream)


Point = tuple[float, float]


def corners(rect: list[float], rotation: int) -> tuple[Point, Point, Point, Point]:
    """A rectangle of text as its upper-left, upper-right, lower-left and lower-right
    corners as the text reads. Text is taken to read upright as the page is displayed
    (on a page turned a quarter, it runs up or down the page as stored), unless the
    rectangle's shape says otherwise."""
    x0, y0, x1, y1 = rect
    width, height = x1 - x0, y1 - y0
    if rotation in (0, 180):
        reading = rotation if height <= 3 * width else 90
    else:
        reading = rotation if width <= 3 * height else 0
    return {0: ((x0, y1), (x1, y1), (x0, y0), (x1, y0)),
            90: ((x0, y0), (x0, y1), (x1, y0), (x1, y1)),
            180: ((x1, y0), (x0, y0), (x1, y1), (x0, y1)),
            270: ((x1, y1), (x1, y0), (x0, y1), (x0, y0))}[reading]


def _toward(a: Point, b: Point, share: float) -> Point:
    return a[0] + (b[0] - a[0]) * share, a[1] + (b[1] - a[1]) * share


def _point(p: Point) -> str:
    return f"{_number(p[0])} {_number(p[1])}"


def _markup_drawing(kind: str, quads: list[tuple[Point, Point, Point, Point]], rgb) -> str:
    r, g, b = (_number(v) for v in rgb)
    out = [f"{r} {g} {b} rg {r} {g} {b} RG"]
    for ul, ur, ll, lr in quads:
        height = math.dist(ul, ll)
        length = math.dist(ll, lr)
        width = max(height / 14, 0.5)
        if kind == "highlight":
            out.append(f"{_point(ul)} m {_point(ur)} l {_point(lr)} l {_point(ll)} l h f")
        elif kind in ("underline", "strikeout"):
            share = 0.08 if kind == "underline" else 0.45
            out.append(f"{_number(width)} w {_point(_toward(ll, ul, share))} m "
                       f"{_point(_toward(lr, ur, share))} l S")
        else:  # squiggly: a zigzag along the bottom, in at most MAX_WAVES strokes
            step = max(height / 6, 1.0, length / MAX_WAVES)
            amplitude = max(height / 12, 0.5) / max(height, 1e-9)
            points, done, up = [], 0.0, True
            while True:
                along = _toward(ll, lr, min(done / length, 1.0) if length else 0.0)
                lift = amplitude * (2 if up else 0)
                points.append(_point((along[0] + (ul[0] - ll[0]) * lift,
                                      along[1] + (ul[1] - ll[1]) * lift)))
                if done >= length:
                    break
                done, up = min(done + step, length), not up
            out.append(f"{_number(width)} w {points[0]} m "
                       + " ".join(f"{p} l" for p in points[1:]) + " S")
    return " ".join(out)


def _note_drawing(rgb) -> str:
    """A sticky note: a coloured square with a folded corner and three lines."""
    r, g, b = (_number(v) for v in rgb)
    s = NOTE_SIZE
    return (f"{r} {g} {b} rg 0.25 0.25 0.25 RG 0.8 w 0.5 0.5 m {s - 5.5} 0.5 l "
            f"{s - 0.5} 5.5 l {s - 0.5} {s - 0.5} l 0.5 {s - 0.5} l h B "
            f"{s - 5.5} 0.5 m {s - 5.5} 5.5 l {s - 0.5} 5.5 l S "
            f"4 {s - 6} m {s - 4} {s - 6} l 4 {s - 10} m {s - 4} {s - 10} l "
            f"4 {s - 14} m {s - 9} {s - 14} l S")


def _text_box_form(pdf: pikepdf.Pdf, text: str, width: float, height: float, size: float,
                   rgb, border: bool) -> pikepdf.Stream:
    """A text box's appearance, drawn upright at its displayed size by reportlab."""
    lines = simpleSplit(text, "Helvetica", size, width - 8)
    leading = size * 1.2
    if len(lines) * leading + 6 > height or any(
            stringWidth(line, "Helvetica", size) > width - 8 for line in lines):
        raise PdfUserError("The text doesn't fit in its text box: make the rect larger or "
                           "the font_size smaller")
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(width, height), pageCompression=0)
    c.setFillColorRGB(1, 1, 1)  # readable over whatever is beneath
    c.setStrokeColorRGB(*rgb)
    c.setLineWidth(1)
    c.rect(0.5, 0.5, width - 1, height - 1, stroke=int(border), fill=1)
    c.setFillColorRGB(*rgb)
    c.setFont("Helvetica", size)
    for n, line in enumerate(lines):
        c.drawString(4, height - 4 - size * 0.9 - n * leading, line)
    c.showPage()
    c.save()
    with pikepdf.open(io.BytesIO(buf.getvalue())) as made:
        return pdf.copy_foreign(made.pages[0].as_form_xobject())


# --- add_comments ----------------------------------------------------------------------


def _fail(n: int, message: str):
    raise PdfUserError(f"Comment {n}: {message}")


def _checked_rect(n: int, value, name: str = "rect") -> list[float]:
    if not isinstance(value, list) or len(value) != 4 or not all(
            isinstance(v, (int, float)) and not isinstance(v, bool) for v in value):
        _fail(n, f"{name} must be [left, bottom, right, top] in PDF points")
    x0, y0, x1, y1 = (float(v) for v in value)
    if not all(abs(v) <= MAX_COORD for v in value) or x1 <= x0 or y1 <= y0:
        _fail(n, f"{name} must have left < right and bottom < top, within the page")
    return [x0, y0, x1, y1]


ALLOWED = {
    **{kind: {"type", "page", "text", "rects", "comment", "color", "match_case",
              "whole_word"} for kind in MARKUP},
    "note": {"type", "page", "text", "at", "comment", "color", "match_case", "whole_word"},
    "text_box": {"type", "page", "rect", "text", "font_size", "color", "border",
                 "comment"},
    "reply": {"type", "to", "comment"},
}


def _check(n: int, item, total: int) -> dict:
    if not isinstance(item, dict):
        _fail(n, "must be an object")
    kind = item.get("type")
    if kind not in KINDS:
        _fail(n, f"type must be one of: {', '.join(KINDS)}")
    extra = sorted(set(item) - ALLOWED[kind])
    if extra:
        _fail(n, f"a {kind} doesn't take {', '.join(extra)}")
    page = item.get("page")
    if page is not None and (isinstance(page, bool) or not isinstance(page, int)
                             or not 1 <= page <= total):
        _fail(n, f"page must be 1 to {total}")
    comment = item.get("comment")
    if comment is not None and (not isinstance(comment, str) or len(comment) > MAX_TEXT
                                or _SURROGATE.search(comment)):
        _fail(n, f"comment must be text of at most {MAX_TEXT:,} characters")
    for flag in ("match_case", "whole_word", "border"):
        if flag in item and not isinstance(item[flag], bool):
            _fail(n, f"{flag} must be true or false")
    if "color" in item:
        if not isinstance(item["color"], str):
            _fail(n, "color must be #rrggbb or a colour name")
        try:
            _color(item["color"])
        except PdfUserError as exc:
            _fail(n, str(exc))
    if kind in MARKUP:
        if ("text" in item) == ("rects" in item):
            _fail(n, f"a {kind} needs text to find, or rects (not both)")
        if "rects" in item:
            if page is None:
                _fail(n, "rects need the page they are on")
            rects = item["rects"]
            if not isinstance(rects, list) or not 1 <= len(rects) <= MAX_RECTS:
                _fail(n, f"rects must be a list of 1 to {MAX_RECTS} rectangles")
            item = {**item, "rects": [_checked_rect(n, r, "each rect") for r in rects]}
    elif kind == "note":
        if not comment:
            _fail(n, "a note needs its comment")
        if ("text" in item) == ("at" in item):
            _fail(n, "a note needs text to find (it goes beside it), or at (not both)")
        if "at" in item:
            at = item["at"]
            if page is None:
                _fail(n, "at needs the page it is on")
            if not isinstance(at, list) or len(at) != 2 or not all(
                    isinstance(v, (int, float)) and not isinstance(v, bool)
                    and abs(v) <= MAX_COORD for v in at):
                _fail(n, "at must be [x, y] in PDF points: where the note's top-left goes")
    elif kind == "text_box":
        if page is None or "rect" not in item:
            _fail(n, "a text box needs its page and rect")
        text = item.get("text")
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_BOX_TEXT:
            _fail(n, f"a text box needs its text (at most {MAX_BOX_TEXT:,} characters)")
        try:
            _latin(text.replace("\n", " "), "A text box's text")
        except PdfUserError as exc:
            _fail(n, str(exc))
        size = item.get("font_size", 12)
        if isinstance(size, bool) or not isinstance(size, (int, float)) or not 4 <= size <= 72:
            _fail(n, "font_size must be 4 to 72")
        item = {**item, "rect": _checked_rect(n, item["rect"])}
    else:  # reply
        if not comment:
            _fail(n, "a reply needs its comment")
        if not isinstance(item.get("to"), str) or not _ID.fullmatch(item["to"]):
            _fail(n, 'to must be the id of a comment, as list_comments gives it (e.g. "p2-3")')
    if "text" in item and kind != "text_box":
        try:
            item = {**item, "text": _query(item["text"])}
        except PdfUserError as exc:
            _fail(n, str(exc))
    return item


def _find(doc, pdf: pikepdf.Pdf, n: int, item: dict, deadline: float
          ) -> list[tuple[int, list[list[float]]]]:
    """The places *item*'s text is found: (page index, rectangles), at most MAX_MATCHES."""
    total = len(pdf.pages)
    pages = [item["page"] - 1] if item.get("page") else range(total)
    found, sizes = [], {}
    for index in pages:
        if time.monotonic() > deadline:
            _fail(n, f"finding {item['text']!r} took too long; give its page")
        size = _content_size(pdf.pages[index], MAX_PAGE_CONTENT, sizes)
        if size is None or size > MAX_PAGE_CONTENT:
            continue  # too much content to search safely
        matches, _ = _page_matches(doc, index, item["text"], item.get("match_case", False),
                                   item.get("whole_word", False), MAX_MATCHES - len(found))
        found += [(index, m["rects"]) for m in matches]
        if len(found) >= MAX_MATCHES:
            break
    if not found:
        where = f"on page {item['page']}" if item.get("page") else "in the PDF"
        _fail(n, f"{item['text']!r} isn't found {where}")
    return found


def _common(pdf, page: pikepdf.Page, subtype: str, rect, item: dict, kind: str,
            author: str | None, now: str) -> pikepdf.Dictionary:
    annot = pikepdf.Dictionary(
        Type=pikepdf.Name.Annot, Subtype=pikepdf.Name(subtype),
        Rect=pikepdf.Array([round(float(v), 3) for v in rect]),
        P=page.obj, F=FLAG_PRINT, C=pikepdf.Array(list(_rgb(item.get("color"), kind))),
        NM=pikepdf.String(f"paperboy-{secrets.token_hex(8)}"),
        M=pikepdf.String(now), CreationDate=pikepdf.String(now))
    if author:
        annot.T = pikepdf.String(author)
    if item.get("comment"):
        annot.Contents = pikepdf.String(item["comment"])
    return pdf.make_indirect(annot)


def _bounds(rects: list[list[float]], pad: float = 1.0) -> list[float]:
    return [min(r[0] for r in rects) - pad, min(r[1] for r in rects) - pad,
            max(r[2] for r in rects) + pad, max(r[3] for r in rects) + pad]


def _markup(pdf, page, item, rects, author, now) -> pikepdf.Dictionary:
    kind = item["type"]
    rect = _bounds(rects)
    annot = _common(pdf, page, MARKUP[kind], rect, item, kind, author, now)
    rotation = _rotation(page)
    quads = [corners(r, rotation) for r in rects]
    annot.QuadPoints = pikepdf.Array([round(v, 3) for quad in quads for p in quad for v in p])
    if kind == "highlight":
        annot.CA = 1.0
    annot.AP = pikepdf.Dictionary(N=_appearance(
        pdf, rect, _markup_drawing(kind, quads, _rgb(item.get("color"), kind)),
        multiply=kind == "highlight"))
    return annot


def _visible(page: pikepdf.Page, rects: list[list[float]]) -> list[list[float]]:
    """*rects* cut to the visible page; those wholly off it are left out."""
    vx0, vy0, vx1, vy1 = _visible_box(page)
    kept = []
    for x0, y0, x1, y1 in rects:
        x0, y0, x1, y1 = max(x0, vx0), max(y0, vy0), min(x1, vx1), min(y1, vy1)
        if x1 > x0 and y1 > y0:
            kept.append([x0, y0, x1, y1])
    return kept


def _box_text(box) -> str:
    return "[" + ", ".join(_number(v) for v in box) + "]"


def _note_beside(page: pikepdf.Page, rect: list[float]) -> tuple[float, float]:
    """The top-left of a note just before the text in *rect*, as it reads, kept on the
    visible page."""
    ul, ur, ll, _ = corners(rect, _rotation(page))
    length = math.dist(ul, ur) or 1.0
    unit = ((ur[0] - ul[0]) / length, (ur[1] - ul[1]) / length)
    middle = _toward(ul, ll, 0.5)
    gap = NOTE_SIZE / 2 + 2
    cx, cy = middle[0] - unit[0] * gap, middle[1] - unit[1] * gap
    return _on_page(page, cx - NOTE_SIZE / 2, cy + NOTE_SIZE / 2)


def _on_page(page: pikepdf.Page, x: float, y: float) -> tuple[float, float]:
    """A note's top-left moved, if need be, so the whole note is on the visible page."""
    vx0, vy0, vx1, vy1 = _visible_box(page)
    return (min(max(x, vx0), max(vx1 - NOTE_SIZE, vx0)),
            max(min(y, vy1), min(vy0 + NOTE_SIZE, vy1)))


def _note(pdf, page, item, x, y, author, now) -> pikepdf.Dictionary:
    rect = [x, y - NOTE_SIZE, x + NOTE_SIZE, y]
    annot = _common(pdf, page, "/Text", rect, item, "note", author, now)
    annot.Name = pikepdf.Name.Comment
    annot.Open = False
    annot.F = FLAG_PRINT | FLAG_NO_ZOOM | FLAG_NO_ROTATE
    annot.AP = pikepdf.Dictionary(N=_appearance(
        pdf, [0, 0, NOTE_SIZE, NOTE_SIZE], _note_drawing(_rgb(item.get("color"), "note"))))
    return annot


def _text_box(pdf, page, item, author, now) -> pikepdf.Dictionary:
    rect = item["rect"]
    rgb = _rgb(item.get("color"), "text_box")
    size = float(item.get("font_size", 12))
    rotation = _rotation(page)
    width, height = rect[2] - rect[0], rect[3] - rect[1]
    if rotation in (90, 270):  # drawn upright as the page is displayed
        width, height = height, width
    form = _text_box_form(pdf, item["text"], width, height, size, rgb,
                          item.get("border", True))
    form.Matrix = pikepdf.Array({0: [1, 0, 0, 1, 0, 0], 90: [0, 1, -1, 0, 0, 0],
                                 180: [-1, 0, 0, -1, 0, 0],
                                 270: [0, -1, 1, 0, 0, 0]}[rotation])
    annot = _common(pdf, page, "/FreeText", rect, item, "text_box", author, now)
    annot.Contents = pikepdf.String(item["text"])
    if item.get("comment"):
        annot.Subj = pikepdf.String(item["comment"][:MAX_AUTHOR])
    annot.DA = pikepdf.String(f"/Helv {_number(size)} Tf "
                              + " ".join(_number(v) for v in rgb) + " rg")
    if rotation:
        annot.Rotate = rotation
    annot.AP = pikepdf.Dictionary(N=form)
    return annot


def _target(pdf: pikepdf.Pdf, n: int, ident: str) -> tuple[int, pikepdf.Dictionary]:
    """The page index and the comment *ident* names (made an object, if it was written
    inside the page's list, so a reply can refer to it)."""
    page, position = (int(v) for v in _ID.fullmatch(ident).groups())
    annots = pdf.pages[page - 1].obj.get("/Annots") if 1 <= page <= len(pdf.pages) else None
    if not isinstance(annots, pikepdf.Array) or not 1 <= position <= len(annots) \
            or not isinstance(annots[position - 1], pikepdf.Dictionary) \
            or str(annots[position - 1].get("/Subtype")) not in LISTED:
        _fail(n, f"there is no comment {ident} (list_comments gives the ids)")
    parent = annots[position - 1]
    if not parent.is_indirect:
        parent = pdf.make_indirect(parent)
        annots[position - 1] = parent
    return page - 1, parent


def _reply(pdf, page, item, parent, author, now) -> pikepdf.Dictionary:
    rect = _rect(parent.get("/Rect")) or [0, 0, 0, 0]
    annot = _common(pdf, page, "/Text", rect, item, "reply", author, now)
    annot.IRT = parent
    annot.RT = pikepdf.Name.R
    annot.Name = pikepdf.Name.Comment
    annot.F = FLAG_PRINT | FLAG_NO_ZOOM | FLAG_NO_ROTATE
    # Shown with the comment it answers, not as a second note on the page.
    annot.AP = pikepdf.Dictionary(N=_appearance(pdf, [0, 0, 0, 0], ""))
    return annot


def add_comments(path: Path, out: Path, settings: Settings, comments: list,
                 author: str | None = None, password: str | None = None
                 ) -> AddCommentsResult:
    """Add *comments* (see server.add_comments) and save the result to *out*."""
    if not isinstance(comments, list) or not 1 <= len(comments) <= MAX_COMMENTS:
        raise PdfUserError(f"comments must be a list of 1 to {MAX_COMMENTS}")
    if author is not None and (not isinstance(author, str) or len(author) > MAX_AUTHOR
                               or _SURROGATE.search(author)):
        raise PdfUserError(f"author must be text of at most {MAX_AUTHOR} characters")
    data = path.read_bytes()
    with open_pdf(data, settings, password) as pdf:
        require_annotation_permission(pdf, "adding comments")
        usage_rights = refuse_signed(pdf, "adding comments")
        protection = inherit_protection(src=pdf, password=password)
        total = len(pdf.pages)
        items = [_check(n, item, total) for n, item in enumerate(comments, 1)]
        now = pdf_date(datetime.now().astimezone())
        doc = None
        if any("text" in item and item["type"] != "text_box" for item in items):
            # Finding text tells where it is, letter by letter if need be: it is reading
            # the document, so copying must be allowed.
            require_copy_permission(pdf, "finding text to comment on")
            doc = _open(_pdfium_bytes(pdf, data))
        deadline = time.monotonic() + FIND_TIME_LIMIT_S
        added: dict[int, list] = {}
        made = []
        try:
            for n, item in enumerate(items, 1):
                kind = item["type"]
                if kind == "reply":
                    index, parent = _target(pdf, n, item["to"])
                    places = [(index, _reply(pdf, pdf.pages[index], item, parent, author,
                                             now))]
                elif kind == "text_box":
                    index = item["page"] - 1
                    page = pdf.pages[index]
                    if _visible(page, [item["rect"]]) != [item["rect"]]:
                        _fail(n, "rect must be on the visible page "
                                 f"{_box_text(_visible_box(page))}")
                    places = [(index, _text_box(pdf, page, item, author, now))]
                elif "rects" in item:
                    index = item["page"] - 1
                    page = pdf.pages[index]
                    rects = _visible(page, item["rects"])
                    if not rects:
                        _fail(n, "rects are all off the visible page "
                                 f"{_box_text(_visible_box(page))}")
                    places = [(index, _markup(pdf, page, item, rects, author, now))]
                elif "at" in item:
                    index = item["page"] - 1
                    page = pdf.pages[index]
                    x, y = item["at"]
                    if not _visible(page, [[x - 0.5, y - 0.5, x + 0.5, y + 0.5]]):
                        _fail(n, "at must be on the visible page "
                                 f"{_box_text(_visible_box(page))}")
                    places = [(index, _note(pdf, page, item, *_on_page(page, x, y),
                                            author, now))]
                else:
                    places = []
                    for index, rects in _find(doc, pdf, n, item, deadline):
                        page = pdf.pages[index]
                        rects = _visible(page, rects)
                        if not rects:  # found where the page is cut off
                            continue
                        if kind in MARKUP:
                            places.append((index, _markup(pdf, page, item, rects, author,
                                                          now)))
                        else:  # a note just before the text, where it is first found
                            places.append((index, _note(pdf, page, item,
                                                        *_note_beside(page, rects[0]),
                                                        author, now)))
                            break
                    if not places:
                        _fail(n, f"{item['text']!r} is only found off the visible page")
                for index, annot in places:
                    added.setdefault(index, []).append(annot)
                made.append((n, kind, places))
        finally:
            if doc is not None:
                _close(doc)
        positions = {}
        for index, annots in added.items():
            page = pdf.pages[index]
            existing = page.obj.get("/Annots")  # (qpdf drops one that isn't a list)
            # A new list for this page alone: others may share the old one.
            listed = pikepdf.Array(list(existing) if existing is not None else [])
            for annot in annots:
                listed.append(annot)
                positions[annot.objgen] = len(listed)
            page.obj.Annots = listed
        result = [{"index": n, "type": kind,
                   "added": [{"id": f"p{index + 1}-{positions[annot.objgen]}",
                              "page": index + 1} for index, annot in places]}
                  for n, kind, places in made]
        if usage_rights and "/Perms" in pdf.Root:  # they break once the file changes
            del pdf.Root.Perms
        pdf.save(str(out), encryption=protection.encryption)
    return protection.report({"output": str(out), "comments": result,
                              "added": sum(len(r["added"]) for r in result)})

