"""Watermarks, stamps and page numbers, drawn as each page is displayed.

Each mark is drawn by reportlab on a page the size of the target page *as
displayed*, turned into a Form XObject, and placed with a transform that undoes
the page's /Rotate and CropBox offset, so it is upright and inside the visible
area whatever the page's geometry.
"""
from __future__ import annotations

import io
import math
import re
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path

import pikepdf
from reportlab.lib.colors import Color, HexColor
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen import canvas

from ..config import Settings
from ..errors import PdfUserError
from ..pagespec import parse_pages
from ..results import PageNumbersResult, WatermarkResult
from .base import inherit_protection, open_pdf, require_modify_permission
from .create import _open_image

POSITIONS = ("center", "top-left", "top-center", "top-right",
             "bottom-left", "bottom-center", "bottom-right")
COLORS = {"black": "#000000", "gray": "#808080", "grey": "#808080", "red": "#cc0000",
          "blue": "#1f4fd1", "green": "#1a7f37"}
FONT, BOLD = "Helvetica", "Helvetica-Bold"   # built-in fonts: Latin (WinAnsi) only
TEXT_LIMIT = 200
FORMAT_LIMIT = 100
MAX_PADDING = 12
IMAGE_MAX_SIDE = 2500  # a mark is at most a page wide; more pixels only cost time
MIN_AUTO_SIZE = 4
_TOKEN = re.compile(r"\{(n|total)(?::0(\d{1,2}))?\}")  # {n}, {total}, {n:06}
_MAX_TREE_DEPTH = 32


def _color(value: str) -> Color:
    hex_value = COLORS.get(value.lower(), value)
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", hex_value):
        raise PdfUserError(f"color must be #rrggbb or one of: {', '.join(COLORS)}")
    return HexColor(hex_value)


def _latin(text: str, what: str) -> str:
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in text):
        raise PdfUserError(f"{what} can't contain control characters (tabs, line breaks)")
    try:
        text.encode("cp1252")
    except UnicodeEncodeError:
        raise PdfUserError(
            f"{what} can only use Latin characters (the built-in font's character set)"
        ) from None
    return text


def _range(name: str, value: float, low: float, high: float, *, low_open=False) -> None:
    if (value <= low if low_open else value < low) or value > high:
        bound = f"above {low:g}" if low_open else f"at least {low:g}"
        raise PdfUserError(f"{name} must be {bound} and at most {high:g}")


def _rotation(page: pikepdf.Page) -> int:
    """The page's /Rotate, inherited from the page tree if need be, as 0/90/180/270."""
    node, depth = page.obj, 0
    while isinstance(node, pikepdf.Dictionary) and depth < _MAX_TREE_DEPTH:
        rotate = node.get("/Rotate")
        if isinstance(rotate, (int, Decimal)) and not isinstance(rotate, bool):
            degrees = int(float(rotate))  # viewers accept 90.0 too
            return (degrees - degrees % 90) % 360
        node, depth = node.get("/Parent"), depth + 1
    return 0


def _box(values) -> tuple[float, float, float, float]:
    x0, y0, x1, y1 = (float(v) for v in values)
    return min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)


def _visible_box(page: pikepdf.Page) -> tuple[float, float, float, float]:
    """What viewers show: the CropBox clipped to the MediaBox (or the MediaBox)."""
    mx0, my0, mx1, my1 = _box(page.mediabox)
    cx0, cy0, cx1, cy1 = _box(page.cropbox)
    x0, y0, x1, y1 = max(mx0, cx0), max(my0, cy0), min(mx1, cx1), min(my1, cy1)
    if x1 <= x0 or y1 <= y0:  # no overlap: viewers fall back to the MediaBox
        return mx0, my0, mx1, my1
    return x0, y0, x1, y1


def _displayed_size(page: pikepdf.Page) -> tuple[float, float]:
    x0, y0, x1, y1 = _visible_box(page)
    w, h = x1 - x0, y1 - y0
    return (h, w) if _rotation(page) in (90, 270) else (w, h)


def _placement(page: pikepdf.Page) -> tuple[float, ...]:
    """Matrix from displayed coordinates (origin bottom-left as seen) to user space."""
    x0, y0, x1, y1 = _visible_box(page)
    return {0: (1, 0, 0, 1, x0, y0), 90: (0, 1, -1, 0, x1, y0),
            180: (-1, 0, 0, -1, x1, y1), 270: (0, -1, 1, 0, x0, y1)}[_rotation(page)]


def _anchor(position: str, width: float, height: float, margin: float) -> tuple[float, float]:
    vertical, _, horizontal = position.partition("-")
    if position == "center":
        return width / 2, height / 2
    x = {"left": margin, "center": width / 2, "right": width - margin}[horizontal]
    y = height - margin if vertical == "top" else margin
    return x, y


def _draw_text(c: canvas.Canvas, text: str, font: str, size: float, position: str) -> None:
    """Draw *text* aligned at the current origin as the position implies."""
    horizontal = "center" if position == "center" else position.partition("-")[2]
    vertical = "center" if position == "center" else position.partition("-")[0]
    y = {"top": -size * 0.75, "bottom": 0, "center": -size * 0.35}[vertical]
    c.setFont(font, size)
    {"left": c.drawString, "right": c.drawRightString,
     "center": c.drawCentredString}[horizontal](0, y, text)


def _state_balance(page: pikepdf.Page) -> tuple[int, int, bool] | None:
    """(most extra Q's at any point, q's left open at the end, text object left open).

    None if the content can't be parsed.
    """
    depth = lowest = 0
    in_text = False
    try:
        for _, operator in pikepdf.parse_content_stream(page):
            op = str(operator)
            if op == "q":
                depth += 1
            elif op == "Q":
                depth -= 1
                lowest = min(lowest, depth)
            elif op == "BT":
                in_text = True
            elif op == "ET":
                in_text = False
    except pikepdf.PdfError:
        return None
    return -lowest, depth - lowest, in_text


def _isolate(pdf: pikepdf.Pdf, page: pikepdf.Page) -> None:
    """Wrap the page's content so nothing it leaves behind affects what follows.

    An extra Q in the content would pop a single prepended q, so prepend one more q
    than it ever over-closes, close an open text object, and restore every q left open.
    """
    extra_closes, left_open, in_text = _state_balance(page) or (0, 0, False)
    opens = 1 + extra_closes
    page.contents_add(pikepdf.Stream(pdf, b"q\n" * opens), prepend=True)
    tail = (b"\nET" if in_text else b"") + b"\n" + b"Q\n" * (1 + left_open)
    page.contents_add(pikepdf.Stream(pdf, tail))


def _own_resources(page: pikepdf.Page) -> None:
    """Give the page its own /Resources and /XObject, so marks don't leak to pages that
    shared them (and to every page later extracted from the output)."""
    resources = page.obj.get("/Resources")
    if not isinstance(resources, pikepdf.Dictionary):
        page.obj.Resources = pikepdf.Dictionary()
        return
    copy = pikepdf.Dictionary({k: v for k, v in resources.items()})
    xobjects = copy.get("/XObject")
    if isinstance(xobjects, pikepdf.Dictionary):
        copy.XObject = pikepdf.Dictionary({k: v for k, v in xobjects.items()})
    page.obj.Resources = copy


def _stamp_pages(
    pdf: pikepdf.Pdf,
    targets: list[int],
    draw: Callable[[canvas.Canvas, int, float, float], None],
    *,
    under: bool = False,
    shared: bool = False,
) -> pikepdf.Pdf:
    """Draw a mark on each target page; return the overlay Pdf (keep it open until saved).

    *shared* reuses one drawing per distinct displayed size (a watermark), instead of
    one per page (page numbers).
    """
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    overlay_page: dict = {}  # target index (or size, if shared) -> overlay page index
    for i in targets:
        width, height = _displayed_size(pdf.pages[i])
        key = (round(width, 2), round(height, 2)) if shared else i
        if key in overlay_page:
            continue
        c.setPageSize((width, height))
        draw(c, i, width, height)
        c.showPage()
        overlay_page[key] = len(overlay_page)
    c.save()
    buf.seek(0)
    overlay = pikepdf.open(buf)
    forms: dict = {}
    for i in targets:
        page = pdf.pages[i]
        width, height = _displayed_size(page)
        key = (round(width, 2), round(height, 2)) if shared else i
        if key not in forms:
            forms[key] = pdf.copy_foreign(overlay.pages[overlay_page[key]].as_form_xobject())
        _own_resources(page)
        name = page.add_resource(forms[key], pikepdf.Name.XObject, prefix="Pb")
        matrix = " ".join(f"{v:.6f}" for v in _placement(page))
        mark = pikepdf.Stream(pdf, f"q {matrix} cm {name} Do Q\n".encode())
        if under:
            page.contents_add(mark, prepend=True)
        else:  # isolate the page's own graphics state so it can't move the mark
            _isolate(pdf, page)
            page.contents_add(mark)
    return overlay


def add_page_numbers(
    path: Path,
    out: Path,
    settings: Settings,
    format: str = "Page {n} of {total}",
    position: str = "bottom-center",
    start: int = 1,
    pages: str | None = None,
    font_size: float = 10,
    margin: float = 36,
    color: str = "black",
    password: str | None = None,
) -> PageNumbersResult:
    """Number pages. Page k shows n = k + start - 1; {total} is the last page's n."""
    if len(format) > FORMAT_LIMIT:
        raise PdfUserError(f"format can be at most {FORMAT_LIMIT} characters")
    remaining = _TOKEN.sub("", format)
    if "{" in remaining or "}" in remaining:
        raise PdfUserError("format can only use {n}, {total} and zero padding like {n:06}")
    if any(int(m.group(2) or 0) > MAX_PADDING for m in _TOKEN.finditer(format)):
        raise PdfUserError(f"padding can be at most {MAX_PADDING} digits, e.g. {{n:06}}")
    if not any(m.group(1) == "n" for m in _TOKEN.finditer(format)):
        raise PdfUserError("format must include {n}, e.g. \"Page {n} of {total}\"")
    _latin(format, "format")
    if position not in POSITIONS:
        raise PdfUserError(f"position must be one of: {', '.join(POSITIONS)}")
    _range("start", start, 0, 1_000_000_000)
    _range("font_size", font_size, 4, 72)
    _range("margin", margin, 0, 144)
    ink = _color(color)
    with open_pdf(path, settings, password) as pdf:
        require_modify_permission(pdf)
        total = len(pdf.pages)
        targets = parse_pages(pages, total, dedupe=True) if pages else list(range(total))
        last = total + start - 1

        def label(i: int) -> str:
            values = {"n": i + start, "total": last}
            return _TOKEN.sub(lambda m: str(values[m.group(1)]).zfill(int(m.group(2) or 0)),
                              format)

        def draw(c: canvas.Canvas, i: int, width: float, height: float) -> None:
            c.setFillColor(ink)
            c.translate(*_anchor(position, width, height, margin))
            _draw_text(c, label(i), FONT, font_size, position)

        overlay = _stamp_pages(pdf, targets, draw)
        with overlay:
            protection = inherit_protection(pdf, password)
            pdf.save(str(out), encryption=protection.encryption)
    return protection.report({
        "output": str(out), "pages": [i + 1 for i in targets],
        "first_label": label(targets[0]) if targets else None,
        "last_label": label(targets[-1]) if targets else None,
    })


def add_watermark(
    path: Path,
    out: Path,
    settings: Settings,
    text: str | None = None,
    image: Path | None = None,
    position: str = "center",
    pages: str | None = None,
    opacity: float = 0.2,
    rotation: float | None = None,
    font_size: float | None = None,
    scale: float = 0.5,
    color: str = "gray",
    layer: str = "over",
    password: str | None = None,
) -> WatermarkResult:
    """Add a text or image watermark (or a corner stamp) to every page or the selection."""
    if (text is None) == (image is None):
        raise PdfUserError("Give a watermark text or an image, not both"
                           if text is not None else "Give a watermark text or an image")
    if text is not None:
        if not text.strip() or len(text) > TEXT_LIMIT:
            raise PdfUserError(f"Watermark text must be 1 to at most {TEXT_LIMIT} characters")
        _latin(text, "Watermark text")
    if position not in POSITIONS:
        raise PdfUserError(f"position must be one of: {', '.join(POSITIONS)}")
    if layer not in ("over", "under"):
        raise PdfUserError("layer must be over (on top of the content) or under (behind it)")
    _range("opacity", opacity, 0, 1, low_open=True)
    if rotation is None:  # tilted across the middle; level in a corner (a stamp)
        rotation = 45 if position == "center" else 0
    _range("rotation", rotation, -360, 360)
    _range("scale", scale, 0, 1, low_open=True)
    if font_size is not None:
        _range("font_size", font_size, 4, 300)
    ink = _color(color)
    picture = None
    if image is not None:
        opened = _open_image(image)
        opened.thumbnail((IMAGE_MAX_SIDE, IMAGE_MAX_SIDE))
        # Transparency can be an alpha band or a tRNS entry (palette, L, RGB images).
        has_alpha = "A" in opened.getbands() or "transparency" in opened.info
        picture = ImageReader(opened.convert("RGBA" if has_alpha else "RGB"))
        opened.close()
    margin = 36

    def _fit(text: str, width: float, height: float) -> float:
        """Largest size whose rotated text box fits the space the position allows."""
        angle = math.radians(rotation)
        cos, sin = abs(math.cos(angle)), abs(math.sin(angle))
        per_point_w = stringWidth(text, BOLD, 1) * cos + 1.0 * sin  # 1.0 ~ text height
        per_point_h = stringWidth(text, BOLD, 1) * sin + 1.0 * cos
        if position == "center":
            room_w, room_h = width * 0.9, height * 0.9
        else:  # anchored at a margin, extending into half the page at most
            room_w, room_h = (width - 2 * margin) * 0.5, (height - 2 * margin) * 0.5
        size = min(200.0, room_w / per_point_w, room_h / per_point_h)
        if size < MIN_AUTO_SIZE:
            raise PdfUserError(f"Watermark text is too long to fit on a {width:.0f} x "
                               f"{height:.0f} pt page; shorten it or set font_size")
        return size

    def draw(c: canvas.Canvas, i: int, width: float, height: float) -> None:
        c.setFillColor(ink)
        c.setFillAlpha(opacity)
        c.translate(*_anchor(position, width, height, margin))
        c.rotate(rotation)
        if picture is not None:
            pw, ph = picture.getSize()
            w = width * scale
            h = w * ph / pw
            dx = {"left": 0, "right": -w}.get(position.partition("-")[2], -w / 2)
            dy = {"top": -h, "bottom": 0}.get(position.partition("-")[0], -h / 2)
            c.drawImage(picture, dx, dy, w, h, mask="auto")
            return
        size = font_size if font_size is not None else _fit(text, width, height)
        _draw_text(c, text, BOLD, size, position)


    with open_pdf(path, settings, password) as pdf:
        require_modify_permission(pdf)
        total = len(pdf.pages)
        targets = parse_pages(pages, total, dedupe=True) if pages else list(range(total))
        overlay = _stamp_pages(pdf, targets, draw, under=layer == "under", shared=True)
        with overlay:
            protection = inherit_protection(pdf, password)
            pdf.save(str(out), encryption=protection.encryption)
    return protection.report({"output": str(out), "pages": [i + 1 for i in targets]})
