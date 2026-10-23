"""Page layout: crop, resize to a paper size, N-up, insert blank pages, interleave.

Sizes and margins are given as the page is seen (after its rotation), in points,
inches or millimetres.
"""
from __future__ import annotations

import math
from pathlib import Path

import pikepdf

from ..config import Settings
from ..errors import PdfUserError
from ..pagespec import parse_pages
from ..results import (
    CropResult,
    InsertBlankResult,
    InterleaveResult,
    NUpResult,
    ResizeResult,
)
from .base import (
    combine_protections,
    copy_docinfo,
    inherit_protection,
    open_pdf,
    require_modify_permission,
)
from .search import _content_size
from .stamp import _displayed_size, _isolate, _rotation, _visible_box

UNITS = {"pt": 1.0, "in": 72.0, "mm": 72 / 25.4}
PAPER = {  # portrait width, height in points
    "a3": (841.89, 1190.55), "a4": (595.28, 841.89), "a5": (419.53, 595.28),
    "letter": (612.0, 792.0), "legal": (612.0, 1008.0), "tabloid": (792.0, 1224.0),
}
MIN_SIDE = 18.0           # points (a quarter inch) a page may be cropped or resized down to
MAX_SIDE = 14_400.0       # points: the largest page PDF viewers support (200 inches)
N_UP_MARGIN, N_UP_GAP = 18.0, 12.0
N_UP_MAX_CONTENT = 16 * 2**20  # content a placed page may process (forms as often as drawn)
MAX_BLANKS = 1_000
# Annotation keys holding x, y coordinates (in pairs) on the page.
_POINT_KEYS = ("/QuadPoints", "/Vertices", "/L", "/CL")


def _unit(unit: str) -> float:
    if unit not in UNITS:
        raise PdfUserError(f"unit must be one of: {', '.join(UNITS)}")
    return UNITS[unit]


def _number(name: str, value, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or \
            not math.isfinite(value) or not low <= value <= high:
        raise PdfUserError(f"{name} must be a number from {low:g} to {high:g}")
    return float(value)


def _paper(size: str | None, width, height, unit: str) -> tuple[float, float]:
    """A target size in points, portrait (width <= height): a paper name, or width and
    height in *unit*."""
    if size is not None:
        if width is not None or height is not None:
            raise PdfUserError("Give a paper size, or width and height, not both")
        if not isinstance(size, str) or size.lower() not in PAPER:
            raise PdfUserError(f"size must be one of: {', '.join(PAPER)}")
        return PAPER[size.lower()]
    if width is None or height is None:
        raise PdfUserError(f"Give a paper size ({', '.join(PAPER)}) or width and height")
    scale = _unit(unit)
    w = _number("width", width, MIN_SIDE / scale, MAX_SIDE / scale) * scale
    h = _number("height", height, MIN_SIDE / scale, MAX_SIDE / scale) * scale
    return min(w, h), max(w, h)


def _oriented(paper: tuple[float, float], orientation: str, landscape_page: bool
              ) -> tuple[float, float]:
    if orientation not in ("auto", "portrait", "landscape"):
        raise PdfUserError("orientation must be auto, portrait or landscape")
    w, h = paper
    landscape = landscape_page if orientation == "auto" else orientation == "landscape"
    return (h, w) if landscape else (w, h)


def _user_unit(page: pikepdf.Page) -> float:
    """Points per unit of the page's coordinates (/UserUnit, 1 unless set)."""
    unit = page.obj.get("/UserUnit")
    try:
        value = float(unit) if unit is not None and not isinstance(unit, bool) else 1.0
    except (TypeError, ValueError):
        return 1.0
    return value if math.isfinite(value) and value > 0 else 1.0


def _shown(page: pikepdf.Page) -> tuple[float, float]:
    """The page's size as seen, in points."""
    w, h = _displayed_size(page)
    u = _user_unit(page)
    return w * u, h * u


def _sized(page: pikepdf.Page, number: int) -> tuple[float, float, float, float]:
    """The visible box, refusing pages without a size."""
    x0, y0, x1, y1 = _visible_box(page)
    if x1 - x0 <= 0 or y1 - y0 <= 0:
        raise PdfUserError(f"Page {number} has no size (its page box is empty)")
    return x0, y0, x1, y1


# --- crop ----------------------------------------------------------------------------------


def crop_pages(path: Path, out: Path, settings: Settings, margins: list | None = None,
               unit: str = "pt", pages: str | None = None, reset: bool = False,
               password: str | None = None) -> CropResult:
    """Crop pages by margins [top, right, bottom, left] measured on the page as seen,
    or (reset) show the whole page again. Cropping hides content; it stays in the file."""
    if reset == (margins is not None):
        raise PdfUserError("Give margins to crop by, or reset=true to undo cropping")
    if margins is not None:
        if not isinstance(margins, list) or len(margins) != 4:
            raise PdfUserError("margins must be 4 numbers: top, right, bottom, left")
        scale = _unit(unit)
        top, right, bottom, left = (
            _number(name, m, 0, MAX_SIDE / scale) * scale
            for name, m in zip(("top", "right", "bottom", "left"), margins, strict=True))
    with open_pdf(path, settings, password) as src:
        total = len(src.pages)
        targets = parse_pages(pages, total, dedupe=True) if pages else list(range(total))
        sizes = []
        for i in targets:
            page = src.pages[i]
            if reset:
                if "/CropBox" in page.obj:
                    del page.obj.CropBox
                sizes.append(_shown(page))
                continue
            x0, y0, x1, y1 = _sized(page, i + 1)
            u = _user_unit(page)  # margins are in points; the box is in page units
            # Each displayed side is a different edge of the unrotated box.
            inset = {0: (left, bottom, right, top), 90: (top, left, bottom, right),
                     180: (right, top, left, bottom), 270: (bottom, right, top, left)
                     }[_rotation(page)]
            box = (x0 + inset[0] / u, y0 + inset[1] / u, x1 - inset[2] / u, y1 - inset[3] / u)
            if (box[2] - box[0]) * u < MIN_SIDE or (box[3] - box[1]) * u < MIN_SIDE:
                w, h = _shown(page)
                raise PdfUserError(f"Those margins leave less than {MIN_SIDE:g} pt of page "
                                   f"{i + 1} ({w:.0f} x {h:.0f} pt)")
            page.obj.CropBox = pikepdf.Array([round(v, 3) for v in box])
            sizes.append(_shown(page))
        protection = inherit_protection(src, password)
        src.save(str(out), encryption=protection.encryption)
    return protection.report({"output": str(out), "pages": [i + 1 for i in targets],
                              "sizes": [[round(w, 2), round(h, 2)] for w, h in sizes]})


# --- resize --------------------------------------------------------------------------------


def _numbers(values) -> list[float] | None:
    """An array's numbers, or None if any entry isn't a number."""
    try:
        numbers = [float(v) for v in values]
    except (TypeError, ValueError):
        return None
    return numbers if all(math.isfinite(v) for v in numbers) else None


def _transform_annotations(page: pikepdf.Page, s: float, tx: float, ty: float,
                           visible: tuple[float, float, float, float], done: set) -> None:
    """Move and scale the page's annotations with its content (links, fields, comments).
    Each annotation object is moved once (*done*), however many pages list it. Ones
    outside what was visible are hidden, as the content there is clipped away."""
    def points(numbers):
        return pikepdf.Array([round(s * v + (tx if k % 2 == 0 else ty), 3)
                              for k, v in enumerate(numbers)])

    annots = page.obj.get("/Annots")
    for annot in annots if isinstance(annots, pikepdf.Array) else []:
        if not isinstance(annot, pikepdf.Dictionary):
            continue
        if annot.is_indirect:
            if annot.objgen in done:
                continue
            done.add(annot.objgen)
        rect = _numbers(annot.get("/Rect", []))
        if rect is not None and len(rect) == 4:
            x0, y0, x1, y1 = min(rect[0], rect[2]), min(rect[1], rect[3]), \
                max(rect[0], rect[2]), max(rect[1], rect[3])
            if x1 < visible[0] or x0 > visible[2] or y1 < visible[1] or y0 > visible[3]:
                flags = annot.get("/F", 0)
                annot.F = (int(flags) if isinstance(flags, int) else 0) | 2  # Hidden
            annot.Rect = points(rect)
        for key in _POINT_KEYS:
            values = annot.get(key)
            numbers = _numbers(values) if isinstance(values, pikepdf.Array) else None
            if numbers is not None and len(numbers) % 2 == 0:
                annot[key] = points(numbers)
        ink = annot.get("/InkList")
        if isinstance(ink, pikepdf.Array):
            paths = [_numbers(path) for path in ink if isinstance(path, pikepdf.Array)]
            annot.InkList = pikepdf.Array([points(p) for p in paths
                                           if p is not None and len(p) % 2 == 0])


def _own_annotations(pdf: pikepdf.Pdf, targets: list[int]) -> None:
    """Give resized pages their own /Annots, with their own copy of any annotation a
    page that isn't resized also lists, so moving it doesn't move theirs."""
    chosen = set(targets)
    elsewhere = set()
    for i, page in enumerate(pdf.pages):
        annots = page.obj.get("/Annots")
        if i not in chosen and isinstance(annots, pikepdf.Array):
            elsewhere |= {a.objgen for a in annots
                          if isinstance(a, pikepdf.Dictionary) and a.is_indirect}
    for i in targets:
        page = pdf.pages[i]
        annots = page.obj.get("/Annots")
        if not isinstance(annots, pikepdf.Array):
            continue
        page.obj.Annots = pikepdf.Array([
            pdf.make_indirect(pikepdf.Dictionary({k: v for k, v in a.items()}))
            if isinstance(a, pikepdf.Dictionary) and a.is_indirect and a.objgen in elsewhere
            else a for a in annots])


def resize_pages(path: Path, out: Path, settings: Settings, size: str | None = None,
                 width=None, height=None, unit: str = "pt", orientation: str = "auto",
                 pages: str | None = None, password: str | None = None) -> ResizeResult:
    """Change pages to a paper size, scaling their content to fit (keeping its
    proportions, centred). Links, form fields and comments move with it."""
    paper = _paper(size, width, height, unit)
    with open_pdf(path, settings, password) as src:
        require_modify_permission(src)
        total = len(src.pages)
        targets = parse_pages(pages, total, dedupe=True) if pages else list(range(total))
        scales, done = [], set()
        _own_annotations(src, targets)
        for i in targets:
            page = src.pages[i]
            x0, y0, x1, y1 = _sized(page, i + 1)
            shown_w, shown_h = _shown(page)
            target_w, target_h = _oriented(paper, orientation, shown_w > shown_h)
            turned = _rotation(page) in (90, 270)
            box_w, box_h = (target_h, target_w) if turned else (target_w, target_h)
            u = _user_unit(page)  # the new page is in points (no /UserUnit)
            s = min(box_w / ((x1 - x0) * u), box_h / ((y1 - y0) * u)) * u
            tx = (box_w - s * (x1 - x0)) / 2 - s * x0
            ty = (box_h - s * (y1 - y0)) / 2 - s * y0
            _isolate(src, page)
            # Scale into place, clipped to what was visible (no hidden margins appear).
            page.contents_add(pikepdf.Stream(src, (
                f"q {s:.6f} 0 0 {s:.6f} {tx:.4f} {ty:.4f} cm "
                f"{x0:.4f} {y0:.4f} {x1 - x0:.4f} {y1 - y0:.4f} re W n\n").encode()),
                prepend=True)
            page.contents_add(pikepdf.Stream(src, b"\nQ\n"))
            _transform_annotations(page, s, tx, ty, (x0, y0, x1, y1), done)
            page.obj.MediaBox = pikepdf.Array([0, 0, round(box_w, 3), round(box_h, 3)])
            for key in ("/CropBox", "/BleedBox", "/TrimBox", "/ArtBox", "/UserUnit"):
                if key in page.obj:
                    del page.obj[key]
            scales.append(round(s / u, 4))
        protection = inherit_protection(src, password)
        src.save(str(out), encryption=protection.encryption)
    return protection.report({"output": str(out), "pages": [i + 1 for i in targets],
                              "width": round(paper[0], 2), "height": round(paper[1], 2),
                              "scales": scales})


# --- N-up ----------------------------------------------------------------------------------


def n_up(path: Path, out: Path, settings: Settings, per_sheet: int = 2,
         size: str | None = None, pages: str | None = None,
         password: str | None = None) -> NUpResult:
    """Put 2 or 4 pages on each sheet, in reading order, each scaled to fit its cell.
    Sheets are the size given, or by default the size of the first page."""
    if per_sheet not in (2, 4) or isinstance(per_sheet, bool):
        raise PdfUserError("per_sheet must be 2 or 4")
    if size is not None and (not isinstance(size, str) or size.lower() not in PAPER):
        raise PdfUserError(f"size must be one of: {', '.join(PAPER)}")
    with open_pdf(path, settings, password) as src:
        require_modify_permission(src)
        total = len(src.pages)
        if not total:
            raise PdfUserError("This PDF has no pages")
        order = parse_pages(pages, total) if pages else list(range(total))
        if len(order) > settings.max_pages:  # "-,-,-" repeats every page
            raise PdfUserError(f"That places {len(order)} pages; at most {settings.max_pages}")
        first_w, first_h = _shown(src.pages[order[0]])
        paper = PAPER[size.lower()] if size else (min(first_w, first_h), max(first_w, first_h))
        # 2 pages side by side on a landscape sheet; 4 in a 2 x 2 grid on a portrait one.
        sheet_w, sheet_h = (paper[1], paper[0]) if per_sheet == 2 else paper
        cols, rows = 2, per_sheet // 2
        cell_w = (sheet_w - 2 * N_UP_MARGIN - (cols - 1) * N_UP_GAP) / cols
        cell_h = (sheet_h - 2 * N_UP_MARGIN - (rows - 1) * N_UP_GAP) / rows
        if min(cell_w, cell_h) < MIN_SIDE or max(sheet_w, sheet_h) > MAX_SIDE:
            raise PdfUserError(f"The first page ({first_w:.0f} x {first_h:.0f} pt) is too "
                               "small or large to use as the sheet; give a paper size")
        sizes: dict = {}
        for i in sorted(set(order)):
            # A page that already draws its content many times over (e.g. an N-up of an
            # N-up of ...) would be drawn that many times more: refuse it.
            work = _content_size(src.pages[i], N_UP_MAX_CONTENT, sizes)
            if work is None or work > N_UP_MAX_CONTENT:
                raise PdfUserError(f"Page {i + 1} has too much content (or too many nested "
                                   "drawings) to place on a sheet")
            # Place each page as viewers show it. qpdf's placement reads /Rotate (only
            # plain 90/180/270 before qpdf 12) and frames the page by its TrimBox; this
            # copy is never saved.
            page = src.pages[i]
            page.obj.Rotate = _rotation(page)
            page.obj.TrimBox = pikepdf.Array(list(_sized(page, i + 1)))
        dst = pikepdf.new()
        forms: dict[int, pikepdf.Object] = {}
        try:
            for start in range(0, len(order), per_sheet):
                dst.add_blank_page(page_size=(sheet_w, sheet_h))
                sheet = dst.pages[-1]
                for slot, i in enumerate(order[start:start + per_sheet]):
                    col, row = slot % cols, slot // cols
                    x = N_UP_MARGIN + col * (cell_w + N_UP_GAP)
                    y = sheet_h - N_UP_MARGIN - (row + 1) * cell_h - row * N_UP_GAP
                    if i not in forms:  # each page copied once, however often placed
                        forms[i] = dst.copy_foreign(src.pages[i].as_form_xobject())
                    sheet.add_overlay(forms[i], pikepdf.Rectangle(x, y, x + cell_w,
                                                                  y + cell_h))
            copy_docinfo(src, dst)
            protection = inherit_protection(src, password)
            dst.save(str(out), encryption=protection.encryption)
        finally:
            dst.close()
    return protection.report({"output": str(out), "sheets": math.ceil(len(order) / per_sheet),
                              "pages_placed": len(order),
                              "sheet_width": round(sheet_w, 2),
                              "sheet_height": round(sheet_h, 2)})


# --- blank pages ---------------------------------------------------------------------------


def insert_blank_pages(path: Path, out: Path, settings: Settings, after: list,
                       count: int = 1, password: str | None = None) -> InsertBlankResult:
    """Insert *count* blank pages after each page number in *after* (0 = before the
    first page), each the size of the page before it as seen."""
    if not isinstance(after, list) or not after:
        raise PdfUserError("after must list page numbers (0 inserts before the first page)")
    if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 100:
        raise PdfUserError("count must be 1 to 100")
    with open_pdf(path, settings, password) as src:
        total = len(src.pages)
        if not total:
            raise PdfUserError("This PDF has no pages")
        for n in after:
            if isinstance(n, bool) or not isinstance(n, int) or not 0 <= n <= total:
                raise PdfUserError(f"after must be page numbers from 0 to {total}")
        added = len(after) * count
        if added > MAX_BLANKS or total + added > settings.max_pages:
            raise PdfUserError(f"That would add {added} pages; at most {MAX_BLANKS}, and "
                               f"{settings.max_pages} pages in all")
        for n in sorted(after, reverse=True):  # from the end, so positions stay valid
            w, h = (min(max(v, 3.0), MAX_SIDE) for v in _shown(src.pages[max(n - 1, 0)]))
            for _ in range(count):
                blank = pikepdf.Dictionary(
                    Type=pikepdf.Name.Page, MediaBox=pikepdf.Array([0, 0, w, h]),
                    Resources=pikepdf.Dictionary(), Contents=src.make_stream(b""))
                src.pages.insert(n, pikepdf.Page(src.make_indirect(blank)))
        # 1-based numbers of the new blank pages in the output.
        inserted, shift = [], 0
        for n in sorted(after):
            inserted += [n + shift + k + 1 for k in range(count)]
            shift += count
        protection = inherit_protection(src, password)
        src.save(str(out), encryption=protection.encryption)
        pages = len(src.pages)
    return protection.report({"output": str(out), "pages": pages, "inserted": inserted})


# --- interleave ----------------------------------------------------------------------------


def interleave_pdfs(fronts: Path, backs: Path, out: Path, settings: Settings,
                    reverse_backs: bool = True, front_password: str | None = None,
                    back_password: str | None = None) -> InterleaveResult:
    """Combine two scans of a double-sided document into one: front 1, back 1, front 2,
    ... Backs scanned by turning the stack over come out last page first, so they are
    reversed unless reverse_backs is false."""
    dst = pikepdf.new()
    try:
        with open_pdf(fronts, settings, front_password) as front, \
                open_pdf(backs, settings, back_password) as back:
            nf, nb = len(front.pages), len(back.pages)
            if nb not in (nf, nf - 1):
                raise PdfUserError(f"The fronts have {nf} pages and the backs {nb}; the "
                                   "backs must have as many pages, or one fewer")
            if nf + nb > settings.max_pages:
                raise PdfUserError(f"The result would exceed the {settings.max_pages} page "
                                   "limit")
            order = list(range(nb))[::-1] if reverse_backs else list(range(nb))
            # With one fewer back, the last front has no back; reversed, the backs still
            # pair from the first sheet.
            for k in range(nf):
                dst.pages.append(front.pages[k])
                if k < nb:
                    dst.pages.append(back.pages[order[k]])
            copy_docinfo(front, dst)
            protection = combine_protections([inherit_protection(front, front_password),
                                              inherit_protection(back, back_password)])
            dst.save(str(out), encryption=protection.encryption)
    finally:
        dst.close()
    return protection.report({"output": str(out), "pages": nf + nb, "fronts": nf,
                              "backs": nb})
