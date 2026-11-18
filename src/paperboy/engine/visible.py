"""Whether text a tool drew can be seen: the page is rendered without it, and with it drawn
all black and all white (opaque), and each new character must change the picture inside
its own outline in one of the two. That proves it isn't cut off by a clipping path nor
under something drawn later; text the colour of what is under it (as the old text was)
changes only one of them, and passes. Reading text back, as PDFium extracts it, can't
tell: it ignores clipping, and what is drawn over the text later.

The pictures are rendered in this process, as view_pages renders, under the PDFium lock
and between deadline checks.
"""
from __future__ import annotations

import ctypes
import math

import pikepdf
import pypdfium2.raw as pdfium_raw
from PIL import ImageChops

from ..errors import PdfUserError
from .redact import TOLERANCE, _chars
from .redact_content import Budget
from .render import _PDFIUM_LOCK, MAX_PIXELS

INK_PIXELS = 40.0      # the smallest new text's size, in pixels, when checked (a full stop: 4)
INK_MAX_SCALE = 4.0    # pixels per point at most
INK_CONTRAST = 16      # grey levels of change that count as drawn
INK_MIN_PIXELS = 3.0   # the smallest new character's height, in pixels, at the least


GREYS = (0.0, 1.0)     # the new text drawn all black, then all white, to check it shows
OPAQUE = b"/Pv gs\n"   # starts each of those drawings: whatever the page's opacity


def make_opaque(pdf, form) -> None:
    """Give *form* the /Pv graphics state OPAQUE uses (fully opaque)."""
    res = form.get("/Resources")
    if res is None:
        res = form.Resources = pikepdf.Dictionary()
    states = res.get("/ExtGState")
    if states is None:
        states = res.ExtGState = pikepdf.Dictionary()
    states.Pv = pdf.make_indirect(pikepdf.Dictionary(Type=pikepdf.Name.ExtGState,
                                                     ca=1.0, CA=1.0))


def glyphs(doc, index: int) -> list[tuple[int, float, float, tuple]]:
    """The characters PDFium reads on a page (not spaces): each with the centre of its
    box as redaction reads it (the advance by the font's height), and the box of its
    outline, which covers only its ink, with the font's height last (x0, y0, x1, y1,
    height; None if it has no ink)."""
    with _PDFIUM_LOCK:
        page = doc[index]
        try:
            textpage = page.get_textpage()
            try:
                raw = textpage.raw
                edges = [ctypes.c_double() for _ in range(4)]
                found = []
                for i, (u, b, generated) in enumerate(_chars(textpage)):
                    if generated or (u < 0x110000 and chr(u).isspace()):
                        continue
                    ink = None
                    if pdfium_raw.FPDFText_GetCharBox(raw, i, *edges):
                        left, right, bottom, top = (e.value for e in edges)
                        if right > left and top > bottom:
                            ink = (left, bottom, right, top, max(b[3] - b[1], top - bottom))
                    found.append((u, (b[0] + b[2]) / 2, (b[1] + b[3]) / 2, ink))
                return found
            finally:
                textpage.close()
        finally:
            page.close()


class Known:
    """Characters already on a page, to tell them from those drawn since: the same
    character within TOLERANCE of where one was."""

    def __init__(self, before: list) -> None:
        self.cells: dict[tuple, list] = {}
        for u, x, y, *_ in before:
            self.cells.setdefault((u, math.floor(x / TOLERANCE), math.floor(y / TOLERANCE)),
                                  []).append((x, y))

    def __contains__(self, glyph: tuple) -> bool:
        u, x, y = glyph[:3]
        cx, cy = math.floor(x / TOLERANCE), math.floor(y / TOLERANCE)
        return any(abs(x - px) <= TOLERANCE and abs(y - py) <= TOLERANCE
                   for dx in (-1, 0, 1) for dy in (-1, 0, 1)
                   for px, py in self.cells.get((u, cx + dx, cy + dy), ()))


def _picture(doc, index: int, boxes: list[tuple]) -> tuple:
    """Page *index* of *doc* rendered in grey, at a scale where the smallest text of the
    *boxes* (see glyphs) is INK_PIXELS high (within MAX_PIXELS), with each box in the
    picture's pixels."""
    smallest = min(b[4] for b in boxes)
    with _PDFIUM_LOCK:
        page = doc[index]
        try:
            width, height = page.get_size()
            scale = min(INK_MAX_SCALE, max(1.0, INK_PIXELS / smallest),
                        math.sqrt(MAX_PIXELS / max(width * height, 1.0)))
            if smallest * scale < INK_MIN_PIXELS:
                raise PdfUserError(f"Page {index + 1} is too large to check that the new "
                                   "text shows (the picture of it would be too coarse). "
                                   "Nothing was written")
            image = page.render(scale=scale, grayscale=True).to_pil().convert("L").copy()
            dx, dy = ctypes.c_int(), ctypes.c_int()
            placed = []
            for x0, y0, x1, y1, _ in boxes:
                corners = []
                for x, y in ((x0, y0), (x1, y1)):
                    pdfium_raw.FPDF_PageToDevice(page.raw, 0, 0, image.width,
                                                 image.height, 0, x, y, dx, dy)
                    corners.append((dx.value, dy.value))
                (ax, ay), (bx, by) = corners
                x0, y0 = min(ax, bx), min(ay, by)   # within the outline: none of the
                placed.append((x0, y0, max(max(ax, bx), x0 + 1),   # ink around it counts
                               max(max(ay, by), y0 + 1)))
        finally:
            page.close()
    return image, placed


def shown(before_doc, drawn_docs: list, index: int, boxes: list[tuple],
          budget: Budget) -> bool:
    """Whether every one of *boxes* (a new character's, in page space) changes the
    picture of page *index* from *before_doc* to one of *drawn_docs* (the new text drawn
    black, and white: see drawn_docs): not cut off by a clipping path, nor hidden under
    something drawn later."""
    boxes = [box for box in boxes if box is not None]   # a character with no ink
    if not boxes:
        return True
    budget.check()
    before, placed = _picture(before_doc, index, boxes)
    seen = [False] * len(placed)
    for doc in drawn_docs:
        budget.check()
        after, _ = _picture(doc, index, boxes)
        changed = ImageChops.difference(before, after).point(
            lambda v: 255 if v > INK_CONTRAST else 0)
        for n, box in enumerate(placed):
            if not n & 0x3FF:
                budget.check()
            seen[n] = seen[n] or changed.crop(box).getbbox() is not None
    return all(seen)


def drawn_docs(pdf, forms: dict, variants: list[dict], opener, closer, stack,
               budget: Budget) -> list:
    """PDFium documents of *pdf* with *forms* holding each of *variants* in turn (the
    content each form gets: the new text all black, then all white, opaque), closed with
    *stack*. The forms are left holding the last variant: write their real content
    after."""
    docs = []
    for variant in variants:
        budget.check()
        for n, form in forms.items():
            form.write(variant[n])
        doc = opener(pdf)
        stack.callback(closer, doc)
        docs.append(doc)
    return docs
