"""redact_pdf: remove text, pictures and drawings from areas of pages, for real.

What is redacted is given as text to find, regular expressions, or areas of pages.
Text is found by PDFium; each match becomes areas covering its characters. On each
page with areas, the content is rewritten without what lies under them (see
:mod:`.redact_content`), the areas are painted over, and annotations touching them
are removed, form fields with their values included. Text found anywhere else in the
document is replaced too: metadata, bookmarks, comments, form values, alt text.

Nothing is taken on trust. Each rewritten page is read again by PDFium: no character
may remain under an area, and every character outside the areas must still be there.
A page that can't be rewritten exactly, or fails that check, is replaced by a picture
of itself with the areas painted over (unless rasterize="never", when the call is
refused). Earlier versions of the file, thumbnails and application data of the pages
are dropped, so the original doesn't survive elsewhere in the file.
"""
from __future__ import annotations

import ctypes
import html
import io
import json
import math
import re
import subprocess
import sys
import time
import unicodedata
from pathlib import Path

import pikepdf
import pypdfium2.raw as pdfium_raw
from PIL import ImageDraw

from ..config import Settings
from ..errors import PdfUserError
from ..pagespec import parse_pages
from ..results import RedactResult
from .base import (
    inherit_protection,
    open_pdf,
    require_copy_permission,
    require_modify_permission,
    signature_state,
)
from .extract import _page_resources
from .redact_content import (
    Areas,
    Budget,
    Redactor,
    Unsure,
    _overlaps,
    _rect,
    content_streams,
    parse,
)
from .render import _PDFIUM_LOCK, MAX_PIXELS, MAX_TOTAL_PIXELS, _close, _open
from .sanitize import _dictionaries
from .search import MAX_PAGE_CONTENT, _content_size, _query
from .streams import decode
from .tables import WORKER_MEMORY

MAX_TEXTS = 50
MAX_PATTERNS = 20
MAX_PATTERN = 300          # characters in a pattern
MAX_AREAS = 2_000
MAX_MATCHES = 50_000       # matches one call may redact
MAX_LISTED = 1_000         # matches listed in the result
MAX_MATCH_TEXT = 200
MAX_PATTERN_TEXT = 20_000_000  # characters of text matched against patterns per call
MAX_XMP = 4 * 2**20
MAX_STRUCTURE = 500_000    # structure elements looked through for alt text
MAX_OBJECTS = 1_000_000    # objects looked through for what redaction left behind
CHECK_CONTENT = 256 * 2**20   # content read to see what still draws the originals
CHECK_TOKENS = 16_000_000
MAX_APPEARANCES = 10_000   # annotation appearances drawn for searching, per call
MIN_APPEARANCE = 256       # bytes each counts as, however small
DPI_RANGE = (72, 300)
FILLS = ("black", "white", "none")
RASTERIZE = ("if_needed", "never", "always")
REDACT_TIME_LIMIT_S = 180  # per call
PATTERN_TIME_LIMIT_S = 30  # per run of the pattern worker
TOLERANCE = 1.0            # points: characters this close to an area's edge may go either way
REPLACEMENT = "[redacted]"
_STAND_INS = ("/ActualText", "/Alt", "/E")
# The keys whose strings are text a person wrote or reads (any key of the document
# info counts too); other strings are identifiers or syntax (/DA, /Registry, ...).
# Named destinations (/Dest, /D) and the keys of name trees (/Names) are renamed the
# same way everywhere, so links still find them.
_TEXT_KEYS = {"/Title", "/Author", "/Subject", "/Keywords", "/Creator", "/Producer",
              "/Contents", "/T", "/TU", "/TM", "/V", "/DV", "/Opt", "/RC", "/RV", "/Alt",
              "/ActualText", "/E", "/Desc", "/UF", "/F", "/Subj", "/P", "/JS", "/URI",
              "/Name", "/NM", "/CA", "/AC", "/Reason", "/Location", "/ContactInfo",
              "/Dest", "/D", "/Names"}
# Streams of text: scripts, rich text, XFA forms.
_TEXT_STREAMS = {"/JS": "scripts", "/RC": "comments", "/XFA": "XFA form data"}
MAX_TEXT_STREAM = 4 * 2**20


# --- what to redact ---------------------------------------------------------------------


def _check_inputs(texts, patterns, fill, rasterize, dpi) -> tuple[list[str], list[str]]:
    if texts is not None and not isinstance(texts, list) or \
            patterns is not None and not isinstance(patterns, list):
        raise PdfUserError("texts and patterns must be lists")
    texts = list(dict.fromkeys(_query(t) for t in texts or []))
    if len(texts) > MAX_TEXTS:
        raise PdfUserError(f"Give at most {MAX_TEXTS} texts")
    patterns = list(dict.fromkeys(patterns or []))
    if len(patterns) > MAX_PATTERNS:
        raise PdfUserError(f"Give at most {MAX_PATTERNS} patterns")
    for p in patterns:
        if not isinstance(p, str) or not p or len(p) > MAX_PATTERN:
            raise PdfUserError(f"Each pattern must be 1 to {MAX_PATTERN} characters")
    if fill not in FILLS:
        raise PdfUserError(f"fill must be one of: {', '.join(FILLS)}")
    if rasterize not in RASTERIZE:
        raise PdfUserError(f"rasterize must be one of: {', '.join(RASTERIZE)}")
    if isinstance(dpi, bool) or not isinstance(dpi, int) or \
            not DPI_RANGE[0] <= dpi <= DPI_RANGE[1]:
        raise PdfUserError(f"dpi must be {DPI_RANGE[0]} to {DPI_RANGE[1]}")
    return texts, patterns


def _given_areas(areas, pdf: pikepdf.Pdf) -> dict[int, list[tuple]]:
    """The areas asked for, by page index: [left, bottom, right, top] in PDF points,
    within the page's media box."""
    total = len(pdf.pages)
    if areas is None:
        return {}
    if not isinstance(areas, list) or len(areas) > MAX_AREAS:
        raise PdfUserError(f"areas must be a list of at most {MAX_AREAS}")
    found: dict[int, list[tuple]] = {}
    for n, area in enumerate(areas, 1):
        page = area.get("page") if isinstance(area, dict) else None
        if isinstance(page, bool) or not isinstance(page, int) or not 1 <= page <= total:
            raise PdfUserError(f"Area {n}: page must be 1 to {total}")
        rect = area.get("rect")
        values = None
        if isinstance(rect, list) and len(rect) == 4 and all(
                isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
                for v in rect):
            values = _rect(pikepdf.Array(rect))
        if values is None or values[2] - values[0] <= 0 or values[3] - values[1] <= 0:
            raise PdfUserError(f"Area {n}: rect must be [left, bottom, right, top], with "
                               "some width and height")
        media = _rect(pdf.pages[page - 1].mediabox) or (0, 0, 0, 0)
        values = (max(values[0], media[0]), max(values[1], media[1]),
                  min(values[2], media[2]), min(values[3], media[3]))
        if values[2] <= values[0] or values[3] <= values[1]:
            raise PdfUserError(f"Area {n} is outside page {page}")
        found.setdefault(page - 1, []).append(values)
    return found


def _chars(textpage, union: bool = False) -> list[tuple[int, tuple, bool]]:
    """Each character of a text page: its code point, its box (the glyph's advance
    by the font's height, as PDFium measures it) and whether PDFium made it up (a
    space or line break between pieces of text). With *union*, the box also covers the
    glyph's own outline (older PDFium misplaces some loose boxes: a match must never
    cover less than its characters)."""
    raw, rect = textpage.raw, pdfium_raw.FS_RECTF()
    left, right, bottom, top = (ctypes.c_double() for _ in range(4))
    found = []
    for i in range(textpage.count_chars()):
        generated = pdfium_raw.FPDFText_IsGenerated(raw, i) == 1
        box = (math.nan,) * 4   # none: it covers nothing, and counts as outside areas
        if pdfium_raw.FPDFText_GetLooseCharBox(raw, i, rect):
            box = (rect.left, rect.bottom, rect.right, rect.top)
        loose = box[2] > box[0] and box[3] > box[1]
        if (union or not loose) and pdfium_raw.FPDFText_GetCharBox(
                raw, i, left, right, bottom, top):
            tight = (left.value, bottom.value, right.value, top.value)
            if not loose:  # older PDFium gives turned text a loose box without width
                box = tight
            elif tight[2] > tight[0] and tight[3] > tight[1]:
                box = (min(box[0], tight[0]), min(box[1], tight[1]),
                       max(box[2], tight[2]), max(box[3], tight[3]))
        found.append((pdfium_raw.FPDFText_GetUnicode(raw, i), box, generated))
    return found


def _lines(boxes: list[tuple]) -> list[tuple]:
    """Character boxes joined into one box per run along a line (or down a column);
    boxes that run neither way (text at an angle) stay as they are."""
    found: list[tuple] = []
    for b in boxes:
        if found:
            a = found[-1]
            h, w = max(a[3] - a[1], 1.0), max(a[2] - a[0], 1.0)
            across = abs(a[1] - b[1]) <= h / 4 and abs(a[3] - b[3]) <= h / 4 and \
                -h / 4 <= b[0] - a[2] <= h
            down = abs(a[0] - b[0]) <= w / 4 and abs(a[2] - b[2]) <= w / 4 and \
                -w / 4 <= a[1] - b[3] <= w
            if across or down:
                found[-1] = (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]),
                             max(a[3], b[3]))
                continue
        found.append(b)
    return found


def _clean(text: str) -> str:
    text = re.sub(r"\s+", " ", text.replace("\x00", "")).strip()
    return text if len(text) <= MAX_MATCH_TEXT else text[:MAX_MATCH_TEXT - 1] + "…"


def _match_boxes(chars, start: int, count: int) -> list[tuple]:
    boxes = [box for _, box, generated in chars[start:start + count]
             if not generated and box[2] > box[0] and box[3] > box[1]]
    return _lines(boxes)


def _located(chars, start: int, count: int, index: int) -> list[tuple]:
    """A match's areas; a match that can't be placed on the page is refused, never
    skipped."""
    boxes = _match_boxes(chars, start, count)
    if not boxes and any(not g and not (u < 0x110000 and chr(u).isspace())
                         for u, _, g in chars[start:start + count]):
        raise PdfUserError(f"A match on page {index + 1} can't be located on the page; "
                           "nothing was written. Redact that page with rasterize=\"always\"")
    return boxes


def _run_patterns(patterns: list[str], texts: list[str], ignore_case: bool,
                  deadline: float) -> list[list[int]]:
    """Every match of the patterns in the texts, as [text, pattern, start, end], found
    by pattern_worker in a process that is killed if it runs too long."""
    seconds = min(PATTERN_TIME_LIMIT_S, deadline - time.monotonic())
    request = {"patterns": patterns, "texts": texts, "ignore_case": ignore_case,
               "max_matches": MAX_MATCHES, "cpu_seconds": max(1, int(seconds) + 1),
               "memory_bytes": WORKER_MEMORY}
    try:
        proc = subprocess.run([sys.executable, "-I", "-m", "paperboy.engine.pattern_worker"],
                              input=json.dumps(request).encode(), capture_output=True,
                              timeout=max(seconds, 0.1))
    except subprocess.TimeoutExpired:
        proc = None
    if proc is None or proc.returncode != 0:
        raise PdfUserError("The patterns took too long to match this document (some "
                           "patterns can take very long on some text); make them simpler. "
                           "Nothing was written")
    answer = json.loads(proc.stdout)
    if "invalid" in answer:
        raise PdfUserError(f"Pattern {answer['invalid'] + 1} is not a valid regular "
                           f"expression: {answer['error']}")
    if answer["truncated"]:
        raise PdfUserError(f"The patterns match more than {MAX_MATCHES} times; make them "
                           "more specific")
    return answer["matches"]


def _find(doc, targets: list[int], texts: list[str], patterns: list[str],
          match_case: bool, whole_word: bool, deadline: float,
          locate: bool = True) -> tuple[list, list[int]]:
    """Matches as (page index, what matched, the text, areas), and the pages without
    text. Without *locate*, matches aren't placed (and have no areas)."""
    found, without, pattern_pages = [], [], []
    for index in targets:
        if time.monotonic() > deadline:
            raise PdfUserError(f"Searching took longer than {REDACT_TIME_LIMIT_S} s; "
                               "nothing was written. Try fewer pages")
        with _PDFIUM_LOCK:
            page = doc[index]
            try:
                textpage = page.get_textpage()
                try:
                    chars = _chars(textpage, union=True)
                    if not any(not g for _, _, g in chars):
                        without.append(index + 1)
                        continue
                    for query in texts:
                        searcher = textpage.search(query, match_case=match_case,
                                                   match_whole_word=whole_word)
                        try:
                            while (hit := searcher.get_next()) is not None:
                                start, count = hit
                                found.append((index, query, _clean(
                                    textpage.get_text_range(start, count)),
                                    _located(chars, start, count, index) if locate else []))
                                if len(found) > MAX_MATCHES:
                                    raise PdfUserError(
                                        f"The texts are found more than {MAX_MATCHES} "
                                        "times; redact fewer pages at a time")
                        finally:
                            searcher.close()
                    if patterns:
                        pattern_pages.append((index, chars))
                finally:
                    textpage.close()
            finally:
                page.close()
    if pattern_pages:
        strings = ["".join(chr(u) if 0 < u < 0x110000 else "�" for u, _, _ in chars)
                   for _, chars in pattern_pages]
        if sum(map(len, strings)) > MAX_PATTERN_TEXT:
            raise PdfUserError("These pages have too much text to match patterns against; "
                               "choose fewer pages")
        for t, p, start, end in _run_patterns(patterns, strings, not match_case, deadline):
            index, chars = pattern_pages[t]
            found.append((index, patterns[p], _clean(strings[t][start:end]),
                          _located(chars, start, end - start, index) if locate else []))
            if len(found) > MAX_MATCHES:
                raise PdfUserError(f"The texts and patterns match more than {MAX_MATCHES} "
                                   "times; redact fewer pages at a time")
    found.sort(key=lambda m: m[0])
    return found, without


# --- annotations and fields -------------------------------------------------------------


def _field_of(widget: pikepdf.Dictionary) -> pikepdf.Dictionary:
    """The form field a widget belongs to (a widget can be its own field)."""
    if "/T" in widget or "/FT" in widget or not isinstance(widget.get("/Parent"),
                                                           pikepdf.Dictionary):
        return widget
    return widget.Parent


def _widgets(field: pikepdf.Dictionary) -> list:
    kids = field.get("/Kids")
    found = [field] if field.get("/Subtype") == pikepdf.Name.Widget else []
    if isinstance(kids, pikepdf.Array):
        found += [k for k in kids if isinstance(k, pikepdf.Dictionary)
                  and k.get("/Subtype") == pikepdf.Name.Widget]
    return found


def _drop_annotations(pdf: pikepdf.Pdf, wanted: dict) -> tuple[dict[int, int], set]:
    """Remove annotations touching the areas, with their popups and replies; a form
    field with a widget there goes entirely (its value is the field's, wherever it
    shows). Returns how many were removed from each page, and what was removed."""
    doomed: set = set()
    fields = []
    for index, areas in wanted.items():
        annots = pdf.pages[index].obj.get("/Annots")
        for annot in annots if isinstance(annots, pikepdf.Array) else []:
            if not isinstance(annot, pikepdf.Dictionary) or \
                    annot.get("/Subtype") == pikepdf.Name.Popup:
                continue
            rect = _rect(annot.get("/Rect"))
            if rect is None or not _overlaps(
                    (rect[0], rect[1], max(rect[2], rect[0] + 0.01),
                     max(rect[3], rect[1] + 0.01)), areas):
                continue
            doomed.add(annot.objgen if annot.is_indirect else id(annot))
            if annot.get("/Subtype") == pikepdf.Name.Widget:
                field = _field_of(annot)
                fields.append(field)
                doomed.update(w.objgen for w in _widgets(field) if w.is_indirect)
                if field.is_indirect:
                    doomed.add(field.objgen)
    acro = pdf.Root.get("/AcroForm")
    for field in fields:
        parent = field.get("/Parent")
        owner, key = (parent, "/Kids") if isinstance(parent, pikepdf.Dictionary) \
            else (acro, "/Fields")
        holder = owner.get(key) if isinstance(owner, pikepdf.Dictionary) else None
        if isinstance(holder, pikepdf.Array) and field.is_indirect:
            keep = [k for k in holder if not (isinstance(k, pikepdf.Dictionary)
                                              and k.is_indirect and k.objgen == field.objgen)]
            if len(keep) != len(holder):
                owner[key] = pikepdf.Array(keep)
    removed: dict[int, int] = {}
    for index, page in enumerate(pdf.pages):
        annots = page.obj.get("/Annots")
        if not isinstance(annots, pikepdf.Array) or not doomed:
            continue

        def gone(a) -> bool:
            if not isinstance(a, pikepdf.Dictionary):
                return False
            if (a.objgen if a.is_indirect else id(a)) in doomed:
                return True
            for key in ("/Parent", "/IRT"):  # a popup or reply to a removed annotation
                other = a.get(key)
                if isinstance(other, pikepdf.Dictionary) and other.is_indirect and \
                        other.objgen in doomed and a.get("/Subtype") != pikepdf.Name.Widget:
                    return True
            return False

        items = list(annots)
        keep = [a for a in items if not gone(a)]
        if len(keep) != len(items):
            page.obj.Annots = pdf.make_indirect(pikepdf.Array(keep))
            removed[index] = sum(1 for a in items if gone(a)
                                 and a.get("/Subtype") != pikepdf.Name.Popup)
            doomed.update(a.objgen for a in items if gone(a) and a.is_indirect)
    return removed, {k for k in doomed if isinstance(k, tuple)}


# --- text elsewhere in the document -----------------------------------------------------


def _literal(query: str, match_case: bool, whole_word: bool) -> re.Pattern:
    """A text query as a pattern for strings: its words with any whitespace between."""
    body = r"\s+".join(re.escape(part) for part in query.split(" "))
    if whole_word:
        body = rf"(?<!\w){body}(?!\w)"
    return re.compile(body, 0 if match_case else re.IGNORECASE)


def _place(d, info) -> str:
    if info is not None and d.is_indirect and info.is_indirect and d.objgen == info.objgen:
        return "document info"
    if d.get("/Subtype") == pikepdf.Name.Widget or "/FT" in d:
        return "form fields"
    if "/Rect" in d and "/Subtype" in d:
        return "comments"
    if "/Title" in d and "/Parent" in d:
        return "bookmarks"
    if "/S" in d and "/P" in d:
        return "accessibility structure"
    return "other"


def _strings(container, every: bool = False, depth: int = 0):
    """(container, key, string) for the text strings in a dictionary (under the keys
    that hold text, or all of them if *every*) and in arrays under those keys."""
    if isinstance(container, pikepdf.Array):
        items = enumerate(container)
    else:
        items = ((k, v) for k, v in (container.stream_dict if isinstance(
            container, pikepdf.Stream) else container).items() if every or k in _TEXT_KEYS)
    for key, value in items:
        if isinstance(value, pikepdf.String):
            yield container, key, value
        elif isinstance(value, pikepdf.Array) and depth < 3:
            yield from _strings(value, True, depth + 1)


def _text_streams(d) -> list:
    """(container, key, stream, place) for the streams of text a dictionary holds."""
    found = []
    for key, place in _TEXT_STREAMS.items():
        value = d.get(key)
        if isinstance(value, pikepdf.Stream):
            found.append((d, key, value, place))
        elif isinstance(value, pikepdf.Array) and key == "/XFA":
            found += [(value, i, v, place) for i, v in enumerate(value)
                      if isinstance(v, pikepdf.Stream)]
    return found


def _read_text(stream) -> tuple[str | None, str]:
    """A stream's text and the encoding to write it back in (None if unreadable)."""
    data = decode(stream, MAX_TEXT_STREAM + 1)
    if data is None or len(data) > MAX_TEXT_STREAM:
        return None, ""
    if data.startswith((b"\xfe\xff", b"\xff\xfe")):
        return data.decode("utf-16", "replace"), "utf-16"
    try:
        return data.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        return data.decode("latin-1"), "latin-1"


def _scrub(pdf: pikepdf.Pdf, texts: list[str], patterns: list[str], match_case: bool,
           whole_word: bool, deadline: float) -> tuple[list[str], int]:
    """Replace the text found in strings and XMP metadata anywhere in the document.
    Returns where it was found, and how many attachments (which can't be searched)
    the document has."""
    literal = [_literal(t, match_case, whole_word) for t in texts]
    info = pdf.trailer.get("/Info")
    entries, xmp, streams, attachments = [], [], [], 0
    for d in _dictionaries(pdf):
        if "/EF" in d:
            attachments += 1
        if d.get("/Filter") == pikepdf.Name.Standard and "/O" in d:
            continue  # the encryption dictionary
        place = _place(d, info)
        for container, key, value in _strings(d, every=place == "document info"):
            entries.append((d, container, key, place,
                            unicodedata.normalize("NFKC", str(value))))
        for container, key, stream, where in _text_streams(d):
            streams.append((container, key, where, *_read_text(stream)))
        meta = d.get("/Metadata")
        if isinstance(meta, pikepdf.Stream):
            data = decode(meta, MAX_XMP + 1)
            text = "" if data is None else data.decode("utf-8", "replace")
            xmp.append((d, None if data is None or len(data) > MAX_XMP
                        else text + "\n" + html.unescape(text)))
    spans: dict[int, list[tuple[int, int]]] = {}
    for i, (*_, text) in enumerate(entries):
        for regex in literal:
            for m in regex.finditer(text):
                spans.setdefault(i, []).append(m.span())
    xmp_hits = {i for i, (_, text) in enumerate(xmp)
                if text is None or any(r.search(text) for r in literal)}
    stream_spans: dict[int, list[tuple[int, int]]] = {
        i: [m.span() for r in literal for m in r.finditer(text)]
        for i, (*_, text, _) in enumerate(streams) if text is not None}
    hidden = {i for i, (*_, text, _) in enumerate(streams) if text is not None and not
              stream_spans.get(i) and any(r.search(html.unescape(text)) for r in literal)}
    if patterns:
        strings = [e[-1] for e in entries] + [t or "" for _, t in xmp] + \
            [t or "" for *_, t, _ in streams]
        if sum(map(len, strings)) > MAX_PATTERN_TEXT:
            raise PdfUserError("This document has too much text outside its pages to match "
                               "patterns against")
        for t, _, start, end in _run_patterns(patterns, strings, not match_case, deadline):
            if t < len(entries):
                spans.setdefault(t, []).append((start, end))
            elif t < len(entries) + len(xmp):
                xmp_hits.add(t - len(entries))
            else:
                stream_spans.setdefault(t - len(entries) - len(xmp), []).append((start, end))
    places = set()
    redrawn = False
    renamed = []
    for i, found in spans.items():
        owner, container, key, place, text = entries[i]
        for start, end in _merge(found)[::-1]:
            text = text[:start] + REPLACEMENT + text[end:]
        container[key] = pikepdf.String(text)
        places.add(place)
        if isinstance(container, pikepdf.Array) and isinstance(owner.get("/Names"),
                                                               pikepdf.Array):
            renamed.append((owner, bytes(container[key])))
        # An appearance drawn from the old text would still show it.
        for widget in [owner, *_widgets(owner)] if place in ("form fields", "comments") \
                else []:
            if "/AP" in widget:
                del widget["/AP"]
                redrawn = redrawn or place == "form fields"
    for i in xmp_hits:
        del xmp[i][0]["/Metadata"]
        places.add("XMP metadata")
    for i in reversed(range(len(streams))):  # later array items first: indices hold
        container, key, place, text, codec = streams[i]
        found = stream_spans.get(i)
        if text is None or i in hidden:   # can't be read, or matches only once decoded
            if isinstance(container, pikepdf.Array):  # XFA: names and parts pair up
                container[key] = pikepdf.Stream(pdf, b"")
            else:
                del container[key]
        elif found:
            for start, end in _merge(found)[::-1]:
                text = text[:start] + REPLACEMENT + text[end:]
            container[key] = pikepdf.Stream(pdf, text.encode(codec, "replace"))
        else:
            continue
        places.add(place)
    if renamed:
        _rebuild_name_trees(pdf, renamed)
    acro = pdf.Root.get("/AcroForm")
    if redrawn and isinstance(acro, pikepdf.Dictionary):
        acro.NeedAppearances = True
    return sorted(places), attachments


def _rebuild_name_trees(pdf: pikepdf.Pdf, renamed: list) -> None:
    """Make each name tree with renamed keys one sorted list again (a reader finds
    names by their order, and by the ranges of each branch). Keys not renamed keep
    their bytes; a renamed key that now equals another gets a number added."""
    leaves = {owner.objgen for owner, _ in renamed if owner.is_indirect}
    direct = any(not owner.is_indirect for owner, _ in renamed)
    new_keys = {key for _, key in renamed}
    names = pdf.Root.get("/Names")
    for tree_key, tree in names.items() if isinstance(names, pikepdf.Dictionary) else []:
        pairs, stack, seen, touched = [], [tree], set(), False
        while stack:
            node = stack.pop()
            if not isinstance(node, pikepdf.Dictionary):
                continue
            if node.is_indirect:
                if node.objgen in seen:
                    continue
                seen.add(node.objgen)
                touched = touched or node.objgen in leaves
            else:
                touched = touched or direct
            if len(seen) > MAX_STRUCTURE:
                raise PdfUserError("This document's name tree is too large to rebuild; "
                                   "nothing was written")
            items = node.get("/Names")
            if isinstance(items, pikepdf.Array):
                items = list(items)
                pairs += [(k, v) for k, v in zip(items[::2], items[1::2], strict=False)
                          if isinstance(k, pikepdf.String)]
            kids = node.get("/Kids")
            if isinstance(kids, pikepdf.Array):
                stack.extend(kids)
        if not touched:
            continue
        used, keyed = {bytes(k) for k, _ in pairs if bytes(k) not in new_keys}, []
        for key, value in pairs:
            raw = bytes(key)
            if raw in new_keys:
                n, text = 1, str(key)
                while raw in used:
                    n += 1
                    key = pikepdf.String(f"{text} ({n})")
                    raw = bytes(key)
                used.add(raw)
            keyed.append((raw, key, value))
        keyed.sort(key=lambda item: item[0])
        flat = [x for _, key, value in keyed for x in (key, value)]
        names[tree_key] = pdf.make_indirect(pikepdf.Dictionary(Names=pikepdf.Array(flat)))


def _merge(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return merged


def _strip_alt_text(pdf: pikepdf.Pdf, touched: dict) -> int:
    """Drop the alt text and replacement text of structure elements whose content was
    redacted: *touched* maps a page's objgen to its marked-content ids, or to None for
    every element on it. Returns how many elements lost some."""
    root = pdf.Root.get("/StructTreeRoot")
    if not isinstance(root, pikepdf.Dictionary) or not touched:
        return 0
    stack, seen, count = [(root.get("/K"), None)], set(), 0
    while stack:
        node, page = stack.pop()
        if isinstance(node, pikepdf.Array):
            stack.extend((k, page) for k in node)
            continue
        if not isinstance(node, pikepdf.Dictionary) or "/S" not in node:
            continue
        if node.is_indirect:
            if node.objgen in seen:
                continue
            seen.add(node.objgen)
        if len(seen) > MAX_STRUCTURE:
            raise PdfUserError("This document's structure is too large to remove the alt "
                               "text of what was redacted; nothing was written")
        page = node.get("/Pg", page)
        kids = node.get("/K")
        kids = list(kids) if isinstance(kids, pikepdf.Array) else [kids]
        hit = False
        for kid in kids:
            kid_page, mcid = page, kid
            if isinstance(kid, pikepdf.Dictionary) and kid.get("/Type") == pikepdf.Name.MCR:
                kid_page, mcid = kid.get("/Pg", page), kid.get("/MCID")
            if isinstance(mcid, int) and isinstance(kid_page, pikepdf.Dictionary) and \
                    kid_page.is_indirect and kid_page.objgen in touched:
                ids = touched[kid_page.objgen]
                hit = hit or ids is None or int(mcid) in ids
        if hit and any(k in node for k in _STAND_INS):
            for k in _STAND_INS:
                if k in node:
                    del node[k]
            count += 1
        stack.extend((k, page) for k in kids if isinstance(k, pikepdf.Dictionary))
    return count


# --- checking and the fallback ----------------------------------------------------------


def _plain(pdf: pikepdf.Pdf) -> bytes:
    out = io.BytesIO()
    # Without encryption=, the copy is decrypted; XMP is left as it is (pikepdf would
    # otherwise rewrite it in the open document).
    pdf.save(out, fix_metadata_version=False)
    return out.getvalue()


def _glyphs(doc, index: int) -> list[tuple[int, float, float]]:
    """The characters PDFium reads on a page (not spaces), with their centres."""
    with _PDFIUM_LOCK:
        page = doc[index]
        try:
            textpage = page.get_textpage()
            try:
                return [(u, (b[0] + b[2]) / 2, (b[1] + b[3]) / 2)
                        for u, b, generated in _chars(textpage)
                        if not generated and not (u < 0x110000 and chr(u).isspace())]
            finally:
                textpage.close()
        finally:
            page.close()


def _verify(before: list, after: list, areas: list) -> str | None:
    """Why the redacted page's text isn't what it should be, or None: nothing may be
    left under an area, and everything outside the areas must remain, in order and in
    place."""
    t = TOLERANCE
    inner = Areas((a[0] + t, a[1] + t, a[2] - t, a[3] - t) for a in areas
                  if a[2] - a[0] > 2 * t and a[3] - a[1] > 2 * t)
    outer = Areas((a[0] - t, a[1] - t, a[2] + t, a[3] + t) for a in areas)
    if any(inner.hold(x, y) for _, x, y in after):
        return "text was still found under an area"
    j = 0
    for u, x, y in before:
        if inner.hold(x, y):
            continue
        if j < len(after) and after[j][0] == u and \
                abs(after[j][1] - x) <= TOLERANCE and abs(after[j][2] - y) <= TOLERANCE:
            j += 1
        elif not outer.hold(x, y):
            return "text outside the areas would have changed"
    if j != len(after):
        return "text outside the areas would have changed"
    return None


def _crop(page) -> tuple:
    left, bottom, right, top = page.get_bbox()  # the crop box within the media box
    return float(left), float(bottom), float(right), float(top)


def _rasterize(pdf: pikepdf.Pdf, doc, index: int, areas: list, dpi: int, fill: str,
               pixels_left: int) -> tuple[int, set]:
    """Replace a page by a picture of it (rendered from the original) with the areas
    painted over; keeps its size, rotation and remaining annotations. Returns the
    pixels used, and the content and resources the page no longer draws."""
    with _PDFIUM_LOCK:
        page = doc[index]
        try:
            left, bottom, right, top = _crop(page)
            width, height = right - left, top - bottom
            if width <= 0 or height <= 0:
                raise PdfUserError(f"Page {index + 1} has no visible area to keep")
            scale = min(dpi / 72, math.sqrt(MAX_PIXELS / (width * height)))
            pixels = round(width * scale) * round(height * scale)
            if pixels > pixels_left:
                raise PdfUserError(f"Turning these pages into pictures at {dpi} dpi is over "
                                   f"the {MAX_TOTAL_PIXELS // 1_000_000} million pixel limit "
                                   "per call; use a lower dpi or fewer pages")
            image = page.render(scale=scale, rotation=(360 - page.get_rotation()) % 360,
                                may_draw_forms=False, draw_annots=False).to_pil()
            image = image.convert("RGB")
        finally:
            page.close()
    sx, sy = image.width / width, image.height / height
    draw = ImageDraw.Draw(image)
    for a in areas:
        box = (math.floor((a[0] - left) * sx), math.floor((top - a[3]) * sy),
               math.ceil((a[2] - left) * sx) - 1, math.ceil((top - a[1]) * sy) - 1)
        if box[2] >= box[0] and box[3] >= box[1]:
            draw.rectangle(box, fill=(255, 255, 255) if fill == "white" or fill == "none"
                           else (0, 0, 0))
    out = io.BytesIO()
    image.save(out, "JPEG", quality=90)
    picture = pikepdf.Stream(pdf, out.getvalue())
    picture.Type, picture.Subtype = pikepdf.Name.XObject, pikepdf.Name.Image
    picture.Width, picture.Height = image.width, image.height
    picture.ColorSpace, picture.BitsPerComponent = pikepdf.Name.DeviceRGB, 8
    picture.Filter = pikepdf.Name.DCTDecode
    obj = pdf.pages[index].obj
    gone = set()
    contents = obj.get("/Contents")
    for stream in (contents if isinstance(contents, pikepdf.Array) else [contents]):
        if isinstance(stream, pikepdf.Stream) and stream.is_indirect:
            gone.add(stream.objgen)
    res = _page_resources(obj)
    for group in _GROUPS:
        entries = res.get(group) if isinstance(res, pikepdf.Dictionary) else None
        for _, value in entries.items() if isinstance(entries, pikepdf.Dictionary) else []:
            if isinstance(value, pikepdf.Object) and value.is_indirect:
                gone.add(value.objgen)
    obj.Contents = pdf.make_stream(
        f"q {width:.4f} 0 0 {height:.4f} {left:.4f} {bottom:.4f} cm /RdPage Do Q\n".encode())
    obj.Resources = pikepdf.Dictionary(XObject=pikepdf.Dictionary(RdPage=picture))
    obj.MediaBox = obj.CropBox = pikepdf.Array([left, bottom, right, top])
    for key in ("/Group", "/SeparationInfo"):
        if key in obj:
            del obj[key]
    return pixels, gone


# --- what was replaced must be gone ----------------------------------------------------


def _holders(pdf: pikepdf.Pdf) -> list:
    """Everything that draws with resources: (a key for its resource dictionary, the
    dictionary or None if it uses its caller's, (source, streams) of its content)."""
    found = []
    for page in pdf.pages:
        node = page.obj
        for _ in range(64):  # the page tree node the page takes its resources from
            if "/Resources" in node or not isinstance(node.get("/Parent"), pikepdf.Dictionary):
                break
            node = node.Parent
        res = node.get("/Resources")
        key = ("o", res.objgen) if isinstance(res, pikepdf.Object) and res.is_indirect \
            else ("in", node.objgen)
        found.append((key, res, [(page.obj, content_streams(page.obj))]))
    for d in _dictionaries(pdf):
        if d.get("/Type") in (pikepdf.Name.Page, pikepdf.Name.Pages):
            continue
        if isinstance(d, pikepdf.Stream) and (d.get("/Subtype") == pikepdf.Name.Form
                                              or d.get("/PatternType") == 1):
            content = [(d, [d])]
        elif d.get("/Subtype") == pikepdf.Name.Type3:
            procs = d.get("/CharProcs")
            content = [(s, [s]) for _, s in (procs.items() if isinstance(
                procs, pikepdf.Dictionary) else []) if isinstance(s, pikepdf.Stream)]
        else:
            continue
        res = d.get("/Resources")
        if not isinstance(res, pikepdf.Dictionary):
            found.append((None, None, content))
            continue
        key = ("o", res.objgen) if res.is_indirect else ("in", d.objgen, id(d))
        found.append((key, res, content))
    return found


_FILLS = {"f", "F", "f*", "B", "B*", "b", "b*"}
_STROKES = {"S", "s", "B", "B*", "b", "b*"}
_BOTH = {"Tj", "TJ", "'", '"', "Do", "sh"}  # text may fill or stroke; forms inherit both
_FILL_COLOURS = {"g", "rg", "k", "cs", "sc"}
_STROKE_COLOURS = {"G", "RG", "K", "CS", "SC"}


def _names(content: list, budget: Budget) -> set[str] | None:
    """The resource names some content draws (Do, sh), marks with (BDC, DP) or paints
    with (a pattern chosen by scn or SCN, if anything is painted after it); None if the
    content can't be read (then it may use any)."""
    names: set[str] = set()
    for source, streams in content:
        try:
            ops = parse(source, streams, budget)
        except Unsure:
            return None
        colour = [None, None]   # the pattern filling and stroking, if one is chosen
        saved = []
        for ins in ops:
            if isinstance(ins, pikepdf.ContentStreamInlineImage):
                names.update(c for c in colour if c)  # a stencil paints in the fill colour
                continue
            op, operands = str(ins.operator), list(ins.operands)
            if op in ("Do", "sh") and operands and isinstance(operands[0], pikepdf.Name):
                names.add(str(operands[0]))
            elif op in ("BDC", "DP") and len(operands) == 2 and \
                    isinstance(operands[1], pikepdf.Name):
                names.add(str(operands[1]))
            if op == "q":
                saved.append(list(colour))
            elif op == "Q" and saved:
                colour = saved.pop()
            elif op in ("scn", "SCN"):
                chosen = str(operands[-1]) if operands and \
                    isinstance(operands[-1], pikepdf.Name) else None
                colour[op == "SCN"] = chosen
            elif op in _FILL_COLOURS:
                colour[0] = None
            elif op in _STROKE_COLOURS:
                colour[1] = None
            if (op in _FILLS or op in _BOTH) and colour[0]:
                names.add(colour[0])
            if (op in _STROKES or op in _BOTH) and colour[1]:
                names.add(colour[1])
    return names


_GROUPS = ("/XObject", "/Properties", "/Pattern", "/Shading")


def _cut_loose(pdf: pikepdf.Pdf, gone: set, deadline: float, unused: set = frozenset()
               ) -> int:
    """Make sure what redaction replaced or removed (*gone*: pictures, forms, property
    lists, patterns, content, annotations, fields) isn't left in the file. Resource
    entries for it, and for what redacted pages listed but never drew (*unused*: a form
    a page lists but never draws can hold the very text redacted), are removed where no
    content draws them; then references to *gone* are cut from annotation, field and
    structure lists. Anything else still using something in *gone* keeps it: returns
    how many stay (another page or form draws them unredacted)."""
    candidates = gone | unused
    budget = Budget(deadline, content=CHECK_CONTENT, tokens=CHECK_TOKENS)
    groups: dict = {}
    inheritors = []
    for key, res, content in _holders(pdf):
        if res is None:
            inheritors.append(content)
            continue
        entries = []
        for group in _GROUPS:
            values = res.get(group)
            for name, value in values.items() if isinstance(values, pikepdf.Dictionary) \
                    else []:
                if isinstance(value, pikepdf.Object) and value.is_indirect:
                    if value.objgen in candidates:
                        entries.append((group, name, value.objgen))
                elif group == "/Properties" and isinstance(value, pikepdf.Dictionary) and \
                        any(k in value for k in _STAND_INS):
                    entries.append((group, name, None))  # text written into the page
        if entries:
            groups.setdefault(key, [res, [], entries])[1].append(content)
    kept: set = set()
    if groups:
        # Content without resources of its own draws with its caller's, whichever that is.
        inherited, unreadable = set(), False
        for content in inheritors:
            found = _names(content, budget)
            if found is None:
                unreadable = True
            else:
                inherited |= found
        for res, contents, entries in groups.values():
            names, unknown = set(inherited), unreadable
            for content in contents:
                found = _names(content, budget)
                if found is None:
                    unknown = True
                else:
                    names |= found
            for group, name, objgen in entries:
                if objgen is not None and objgen in gone:
                    # Replaced or removed by redaction: kept only where content that
                    # can be read draws it, never on a guess.
                    if name in names:
                        kept.add(objgen)
                        continue
                elif name in names or unknown:
                    continue  # listed, never touched: kept unless surely unused
                del res[group][name]
    for page in pdf.pages:  # content another page shares stays, as that page's
        for stream in content_streams(page.obj):
            if stream.is_indirect:
                kept.add(stream.objgen)
    for d in _dictionaries(pdf):  # an appearance is drawn by its annotation
        for stream in _appearances(d):
            if stream.is_indirect:
                kept.add(stream.objgen)
    remove = gone - kept
    stack, seen = [pdf.trailer], set()
    while stack:  # every other reference to what's removed (thumbnails, alternates,
        obj = stack.pop()  # editor data, structure, actions...) is cut
        if obj.is_indirect:
            if obj.objgen in seen:
                continue
            seen.add(obj.objgen)
        if len(seen) > MAX_OBJECTS:
            raise PdfUserError("This PDF has too many objects to check that what was "
                               "redacted is gone; nothing was written")
        if isinstance(obj, pikepdf.Array):
            for i in reversed(range(len(obj))):
                child = obj[i]
                if _gone(child, remove):
                    del obj[i]
                elif isinstance(child, (pikepdf.Dictionary, pikepdf.Stream, pikepdf.Array)):
                    stack.append(child)
            continue
        holder = obj.stream_dict if isinstance(obj, pikepdf.Stream) else obj
        for key in list(holder.keys()):
            child = holder[key]
            if _gone(child, remove):
                if key in _MASKS:  # another picture's mask (or a soft mask) draws it
                    kept.add(child.objgen)
                    stack.append(child)
                    continue
                del holder[key]
            elif isinstance(child, (pikepdf.Dictionary, pikepdf.Stream, pikepdf.Array)):
                stack.append(child)
    return len(gone & kept)


_MASKS = ("/SMask", "/Mask", "/G")


def _listed(res) -> set:
    """What a resource dictionary lists that content draws: pictures, forms, property
    lists, patterns, shadings."""
    found = set()
    for group in _GROUPS:
        entries = res.get(group) if isinstance(res, pikepdf.Dictionary) else None
        for _, value in entries.items() if isinstance(entries, pikepdf.Dictionary) else []:
            if isinstance(value, pikepdf.Object) and value.is_indirect:
                found.add(value.objgen)
    return found


def _gone(obj, remove: set) -> bool:
    return isinstance(obj, (pikepdf.Dictionary, pikepdf.Stream)) and obj.is_indirect and \
        obj.objgen in remove


def _appearances(annot) -> list:
    """Every appearance an annotation has: normal, rollover and down, in every state."""
    ap = annot.get("/AP")
    found = []
    for key in ("/N", "/R", "/D"):
        value = ap.get(key) if isinstance(ap, pikepdf.Dictionary) else None
        if isinstance(value, pikepdf.Stream):
            found.append(value)
        elif isinstance(value, pikepdf.Dictionary):
            found += [v for _, v in value.items() if isinstance(v, pikepdf.Stream)]
    return found


class _Drawn:
    """An appearance seen as a page, so _content_size measures it with the forms it
    draws."""

    def __init__(self, stream) -> None:
        res = stream.get("/Resources")
        self.obj = pikepdf.Dictionary(Contents=stream, Resources=res if isinstance(
            res, pikepdf.Dictionary) else pikepdf.Dictionary())


_TEXT_SHOWN = re.compile(rb"(?:Tj|TJ|Do)(?![A-Za-z0-9])|['\"]")


def _may_show_text(stream) -> bool:
    """Whether an appearance can show text: it shows some itself or draws a form (an
    appearance it can't read is assumed to)."""
    data = decode(stream, MAX_PAGE_CONTENT + 1)
    return data is None or len(data) > MAX_PAGE_CONTENT or bool(_TEXT_SHOWN.search(data))


def _flattened(data: bytes, deadline: float) -> bytes:
    """The document with every appearance of its annotations (hidden ones too, and each
    state: on, off, rollover, down) drawn into its pages, so the text they show can be
    found."""
    with pikepdf.open(io.BytesIO(data)) as copy:
        acro = copy.Root.get("/AcroForm")
        if isinstance(acro, pikepdf.Dictionary) and "/NeedAppearances" in acro:
            del acro["/NeedAppearances"]  # else fields aren't drawn at all
        annotated, clones, sizes = False, 0, {}
        for number, page in enumerate(copy.pages, 1):
            annots = page.obj.get("/Annots")
            if not isinstance(annots, pikepdf.Array):
                continue
            drawn, size = [], 0
            for annot in annots:
                if not isinstance(annot, pikepdf.Dictionary):
                    continue
                streams = {s.objgen if s.is_indirect else id(s): s
                           for s in _appearances(annot)}
                for stream in streams.values():
                    if not _may_show_text(stream):
                        continue
                    clones += 1
                    if clones > MAX_APPEARANCES or time.monotonic() > deadline:
                        raise PdfUserError("This PDF has too many annotation appearances to "
                                           "search safely; nothing was written")
                    measured = _content_size(_Drawn(stream), MAX_PAGE_CONTENT, sizes)
                    size += MAX_PAGE_CONTENT + 1 if measured is None else \
                        max(measured, MIN_APPEARANCE)
                    if size > MAX_PAGE_CONTENT:
                        raise PdfUserError(f"The annotations on page {number} have too much "
                                           "content to search safely; nothing was written")
                    each = pikepdf.Dictionary(dict(annot.items()))
                    each.AP = pikepdf.Dictionary(N=stream)
                    each.F = 4  # printed, not hidden
                    for key in ("/AS", "/Popup", "/Parent"):
                        if key in each:
                            del each[key]
                    drawn.append(copy.make_indirect(each))
            if drawn:
                page.obj.Annots = pikepdf.Array(drawn)
                annotated = True
            else:
                del page.obj["/Annots"]
        if not annotated:
            return data
        try:
            copy.flatten_annotations("all")
        except Exception:  # noqa: BLE001 - qpdf raises several kinds
            raise PdfUserError("The annotations of this PDF can't be read, so the text they "
                               "show can't be searched; nothing was written") from None
        out = io.BytesIO()
        copy.save(out, fix_metadata_version=False)
        return out.getvalue()


# --- the tool ---------------------------------------------------------------------------


def redact_pdf(
    path: Path,
    out: Path,
    settings: Settings,
    texts: list[str] | None = None,
    patterns: list[str] | None = None,
    areas: list[dict] | None = None,
    pages: str | None = None,
    match_case: bool = False,
    whole_word: bool = False,
    fill: str = "black",
    rasterize: str = "if_needed",
    dpi: int = 150,
    password: str | None = None,
) -> RedactResult:
    """Write a copy with the texts, pattern matches and areas redacted."""
    texts, patterns = _check_inputs(texts, patterns, fill, rasterize, dpi)
    paint = None if fill == "none" else fill
    deadline = time.monotonic() + REDACT_TIME_LIMIT_S
    with open_pdf(path.read_bytes(), settings, password) as pdf:
        require_modify_permission(pdf)
        if texts or patterns:
            require_copy_permission(pdf, "finding the text to redact")
        signed, usage_rights = signature_state(pdf)
        if signed:
            raise PdfUserError("This PDF is digitally signed; redacting it would invalidate "
                               "the signature")
        total = len(pdf.pages)
        wanted = _given_areas(areas, pdf)
        if not (texts or patterns or wanted):
            raise PdfUserError("Give texts, patterns or areas to redact")
        targets = parse_pages(pages, total, dedupe=True) if pages else list(range(total))
        sizes: dict = {}

        def heavy(i: int) -> bool:
            size = _content_size(pdf.pages[i], MAX_PAGE_CONTENT, sizes)
            return size is None or size > MAX_PAGE_CONTENT

        before = _plain(pdf)
        doc = _open(before)
        search = None
        try:
            matches, without = [], []
            if texts or patterns:
                too_big = [i + 1 for i in targets if heavy(i)]
                if too_big:
                    listed = ", ".join(map(str, too_big[:20])) + (" …" if len(too_big) > 20
                                                                  else "")
                    raise PdfUserError(f"Pages {listed} have too much content to search "
                                       "safely; redact them with areas, or leave them out "
                                       "with pages")
                search = _open(_flattened(before, deadline))  # text in annotations shows there too
                matches, without = _find(search, targets, texts, patterns, match_case,
                                         whole_word, deadline)
                for index, _, _, rects in matches:
                    wanted.setdefault(index, []).extend(rects)
            if not wanted:
                hint = (f" Pages {', '.join(map(str, without[:20]))} have no text (scans?): "
                        "redact them with areas." if without else "")
                raise PdfUserError(f"Nothing to redact: the texts and patterns weren't found."
                                   f"{hint} Nothing was written")

            budget = Budget(deadline)
            report: dict[int, dict] = {}
            fallback: dict[int, str] = {}
            touched: dict = {}
            gone: set = set()
            unused: set = set()
            fonts: dict = {}
            for index in sorted(wanted):
                rects = wanted[index]
                if rasterize == "always":
                    fallback[index] = "rasterize was \"always\""
                    continue
                if heavy(index):
                    fallback[index] = "it has too much content to check"
                    continue
                obj = pdf.pages[index].obj
                unused |= _listed(_page_resources(obj))
                redactor = Redactor(pdf, rects, budget, fonts)
                try:
                    redactor.page(obj, _page_resources(obj), paint)
                except Unsure as exc:
                    fallback[index] = str(exc)
                    continue
                finally:
                    gone |= redactor.gone
                report[index] = redactor.stats
                touched[obj.objgen] = redactor.mcids

            annotations, dropped = _drop_annotations(pdf, wanted)
            gone |= dropped
            places, attachments = (_scrub(pdf, texts, patterns, match_case, whole_word,
                                          deadline) if texts or patterns else ([], 0))
            if report:  # read every rewritten page again, as a reader would
                after = _plain(pdf)
                check = _open(after)
                try:
                    for index in list(report):
                        budget.check()
                        problem = _verify(_glyphs(doc, index), _glyphs(check, index),
                                          wanted[index])
                        if problem:
                            fallback[index] = problem
                            del report[index]
                finally:
                    _close(check)
                if (texts or patterns) and report:  # and the texts can't be found again
                    again = _open(_flattened(after, deadline))
                    try:
                        left = {m[0] for m in _find(again, sorted(report), texts, patterns,
                                                    match_case, whole_word, deadline,
                                                    locate=False)[0]}
                    finally:
                        _close(again)
                    for index in left:
                        fallback[index] = "the text could still be found on it"
                        del report[index]
            if fallback and rasterize == "never":
                index = min(fallback)
                raise PdfUserError(
                    f"Page {index + 1} can't be redacted while keeping its text: "
                    f"{fallback[index]}. Pass rasterize=\"if_needed\" to turn such pages into "
                    "pictures instead. Nothing was written")
            pixels_left = MAX_TOTAL_PIXELS
            for index in sorted(fallback):
                budget.check()
                pixels, drawn = _rasterize(pdf, doc, index, wanted[index], dpi, fill,
                                           pixels_left)
                pixels_left -= pixels
                gone |= drawn
                touched[pdf.pages[index].obj.objgen] = None
        finally:
            _close(doc)
            if search is not None:
                _close(search)
        alt_text = _strip_alt_text(pdf, touched)
        for index in wanted:
            obj = pdf.pages[index].obj
            # A thumbnail, an editor's own copy, data describing the page.
            for key in ("/Thumb", "/PieceInfo", "/Metadata"):
                if key in obj:
                    del obj[key]
        meta = pdf.Root.get("/Metadata")
        if isinstance(meta, pikepdf.Stream):  # XMP may hold thumbnails of the pages
            data = decode(meta, MAX_XMP + 1)
            if data is None or len(data) > MAX_XMP or b"Thumbnails" in data:
                del pdf.Root["/Metadata"]
                places = sorted({*places, "XMP metadata"})
        if usage_rights:  # Reader's usage rights are a signature this would break
            del pdf.Root.Perms
        originals_kept = _cut_loose(pdf, gone, deadline, unused)
        if fallback:  # a picture page must carry no text at all
            check = _open(_plain(pdf))
            try:
                if any(_glyphs(check, i) for i in fallback):
                    raise PdfUserError("Redaction could not be confirmed; nothing was "
                                       "written")
            finally:
                _close(check)
        protection = inherit_protection(pdf, password)
        pdf.save(str(out), encryption=protection.encryption,
                 deterministic_id=not protection.encryption,
                 fix_metadata_version=False)  # leave XMP alone (it may be malformed)

    listed = [{"page": i + 1, "source": source, "text": text}
              for i, source, text, _ in matches[:MAX_LISTED]]
    zero = {"text_removed": 0, "images_edited": 0, "images_removed": 0,
            "drawings_removed": 0}
    redacted = [{"page": i + 1, "areas": len(wanted[i]),
                 "method": "rasterized" if i in fallback else "edited",
                 "reason": fallback.get(i), **report.get(i, zero),
                 "annotations_removed": annotations.get(i, 0)} for i in sorted(wanted)]
    return protection.report({
        "output": str(out),
        "pages_searched": len(targets) if texts or patterns else 0,
        "match_count": len(matches), "matches": listed,
        "matches_truncated": len(matches) > MAX_LISTED,
        "pages": redacted,
        "pages_without_text": without[:MAX_LISTED],
        "also_redacted_in": places,
        "alt_text_removed": alt_text,
        "attachments_not_searched": attachments,
        "originals_kept": originals_kept,
    })
