"""clean_scans: photos of documents made to look like scans.

Each page (a photo, or a page of a PDF) is decoded by Pillow, never by OpenCV, then:
the sheet of paper is found by its edges and flattened, so a page shot at an angle
becomes a rectangle and the table around it goes; text lines still at a slant are
levelled; and the background is evened out to white, so shadows and a yellow cast
go while the ink stays. Pages come out in colour only when they have colour worth
keeping (a stamp, a highlighter, a logo), otherwise in grey, or in black and white
if asked. OpenCV is an optional extra (``pip install "paperboy-mcp[scan]"``).
"""
from __future__ import annotations

import importlib.util
import io
import math
import os
import time
import zlib
from pathlib import Path

import pikepdf

from ..config import Settings
from ..errors import PdfUserError
from ..pagespec import parse_pages
from ..results import CleanScansResult
from .create import IMAGE_EXTS, MAX_TOTAL_PIXELS, _made, _open_image, _pixels
from .render import _close, _open, _page_size, _plain_copy, _render

MAX_INPUTS = 50
MAX_PAGES = 100             # pages one call cleans
TIME_LIMIT_S = 300          # per call, checked between pages
PDF_DPI = 200               # PDF pages are drawn at this resolution to be cleaned...
MAX_SIDE = 6000             # ...and every picture is worked on at most this long a side
WORK_SIDE = 1000            # pixels: the page is looked for on a copy this size
MIN_PAGE = 0.2              # the paper covers at least this much of the photo...
WHOLE_PHOTO = 0.95          # ...and if it covers this much, the photo is the page already
INSET = 0.02                # the outline found runs along the paper's edge: cut inside it
MARGIN = 0.005              # and a cut-out page's edges are cleared, as scanners do
MAX_SKEW = 20.0             # degrees: a larger slant is left alone (it's a layout)
MIN_SKEW = 0.2              # smaller ones aren't worth a resample
MAX_DPI = 300               # pages are stored at most this resolution
COLOUR_SHARE = 0.0003       # of the page in clear colour: a pen signature has more, grey none
SIZES = {"a4": (595.28, 841.89), "letter": (612.0, 792.0)}
MODES = ("auto", "color", "grayscale", "black_and_white")
INSTALL = ('clean_scans needs OpenCV, an optional part of Paperboy: install it with '
           'pip install "paperboy-mcp[scan]" (or uv pip install / pipx inject the same), '
           "then restart the server")
_THREADS = []   # OpenCV's thread pool, set once: calls at once mustn't each take every core


def _cv():
    try:
        import cv2
        import numpy as np
    except ImportError as exc:
        if importlib.util.find_spec("cv2") is not None:  # there, but won't load
            raise PdfUserError(f"OpenCV is installed but couldn't be loaded ({exc}); "
                               'reinstall it with pip install --force-reinstall '
                               '"paperboy-mcp[scan]", then restart the server') from None
        raise PdfUserError(INSTALL) from None
    if not _THREADS:
        _THREADS.append(min(4, os.cpu_count() or 1))
        cv2.setNumThreads(_THREADS[0])
    return cv2, np


def _shrink(pixels, side: float):
    """The picture made no longer than *side* pixels on its long side."""
    cv2, _ = _cv()
    height, width = pixels.shape[:2]
    if max(height, width) <= side:
        return pixels
    k = side / max(height, width)
    return cv2.resize(pixels, (max(1, round(width * k)), max(1, round(height * k))),
                      interpolation=cv2.INTER_AREA)


def _odd(n: float) -> int:
    n = max(3, int(n))
    return n if n % 2 else n + 1


# --- finding the page ----------------------------------------------------------------


def _order(quad):
    """Corners as top-left, top-right, bottom-right, bottom-left: in turn around their
    centre (clockwise on screen), from the one nearest the top left; None if two are
    the same point."""
    _, np = _cv()
    pts = quad.astype("float32")
    centre = pts.mean(axis=0)
    turn = pts[np.argsort(np.arctan2(pts[:, 1] - centre[1], pts[:, 0] - centre[0]))]
    turn = np.roll(turn, -int(turn.sum(axis=1).argmin()), axis=0)
    if min(np.linalg.norm(turn[i] - turn[i - 1]) for i in range(4)) < 1:
        return None
    return turn


def _find_page(rgb):
    """The four corners of the sheet of paper in the photo, in its pixels; None if no
    sheet stands out, or the photo shows nothing but the page."""
    cv2, np = _cv()
    height, width = rgb.shape[:2]
    scale = min(1.0, WORK_SIDE / max(height, width))
    small = cv2.resize(rgb, (max(1, round(width * scale)), max(1, round(height * scale))),
                       interpolation=cv2.INTER_AREA)
    gray = cv2.GaussianBlur(cv2.cvtColor(small, cv2.COLOR_RGB2GRAY), (5, 5), 0)
    area = gray.shape[0] * gray.shape[1]
    kernel = np.ones((5, 5), np.uint8)
    edges = cv2.dilate(cv2.Canny(gray, 40, 120), kernel, iterations=2)
    _, bright = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    bright = cv2.morphologyEx(bright, cv2.MORPH_CLOSE, np.ones((15, 15), np.uint8))
    for mask in (edges, bright):  # the paper's outline; failing that, its brightness
        contours = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)[-2]
        for contour in sorted(contours, key=cv2.contourArea, reverse=True)[:5]:
            hull = cv2.convexHull(contour)
            quad = cv2.approxPolyDP(hull, 0.02 * cv2.arcLength(hull, True), True)
            covered = cv2.contourArea(quad) / area
            if len(quad) == 4 and cv2.isContourConvex(quad) and covered >= MIN_PAGE:
                if covered >= WHOLE_PHOTO:
                    return None
                corners = _order(quad.reshape(4, 2) / scale)
                if corners is None:
                    continue
                centre = corners.mean(axis=0)
                return (corners + (centre - corners) * INSET).astype("float32")
    return None


def _aspect(corners, width: int, height: int) -> float:
    """The sheet's real width over height, from where its corners are in the photo:
    the far edge looks shorter than it is, so the edges' lengths alone mislead. Solved
    for a camera looking from the photo's centre (Zhang and He, "Whiteboard scanning
    and image enhancement", 2007); when the corners don't fit one (a photo taken
    square on, or a page already cut), the edges' average lengths are used."""
    _, np = _cv()
    tl, tr, br, bl = (np.array([*c, 1.0]) for c in corners.astype("float64"))
    u0, v0 = width / 2, height / 2
    edges = (np.linalg.norm(tr - tl) + np.linalg.norm(br - bl)) / \
        max(np.linalg.norm(bl - tl) + np.linalg.norm(br - tr), 1e-9)
    with np.errstate(all="ignore"):  # a degenerate shape gives inf or nan: edges below
        k2 = np.dot(np.cross(tl, br), bl) / np.dot(np.cross(tr, br), bl)
        k3 = np.dot(np.cross(tl, br), tr) / np.dot(np.cross(bl, br), tr)
        n2, n3 = k2 * tr - tl, k3 * bl - tl
        if abs(n2[2] * n3[2]) < 1e-9:   # square on: no perspective to undo
            return edges
        f2 = -((n2[0] * n3[0] - (n2[0] * n3[2] + n2[2] * n3[0]) * u0
                + n2[2] * n3[2] * u0 * u0)
               + (n2[1] * n3[1] - (n2[1] * n3[2] + n2[2] * n3[1]) * v0
                  + n2[2] * n3[2] * v0 * v0)) / (n2[2] * n3[2])
        if not 0 < f2 < 1e12:   # no camera sees the corners so: not a photo's perspective
            return edges
        inv = np.linalg.inv(np.array([[f2 ** 0.5, 0, u0], [0, f2 ** 0.5, v0], [0, 0, 1]]))
        aspect = float((n2 @ inv.T @ inv @ n2) / (n3 @ inv.T @ inv @ n3)) ** 0.5
    return aspect if 0.1 < aspect < 10 else edges


def _flatten(rgb, corners):
    """The page cut out along its corners and made rectangular, in its real shape."""
    cv2, np = _cv()
    tl, tr, br, bl = corners
    width = max(np.linalg.norm(tr - tl), np.linalg.norm(br - bl))
    height = max(np.linalg.norm(bl - tl), np.linalg.norm(br - tr))
    aspect = _aspect(corners, rgb.shape[1], rgb.shape[0])
    if width / height > aspect:   # keep the longer measure, set the other by the shape
        height = width / aspect
    else:
        width = height * aspect
    if max(width, height) > MAX_SIDE:  # however the corners lie, never bigger than this
        k = MAX_SIDE / max(width, height)
        width, height = width * k, height * k
    width, height = max(1, round(width)), max(1, round(height))
    target = np.array([[0, 0], [width - 1, 0], [width - 1, height - 1], [0, height - 1]],
                      dtype="float32")
    matrix = cv2.getPerspectiveTransform(corners, target)
    return cv2.warpPerspective(rgb, matrix, (width, height), flags=cv2.INTER_CUBIC,
                               borderMode=cv2.BORDER_REPLICATE)


# --- levelling ---------------------------------------------------------------------------


def _skew(gray) -> float:
    """How far the text lines slope, in degrees (positive: rising to the right); 0 when
    there are too few lines to tell."""
    cv2, np = _cv()
    height, width = gray.shape
    scale = min(1.0, 1500 / width)
    small = cv2.resize(gray, (max(1, round(width * scale)), max(1, round(height * scale))),
                       interpolation=cv2.INTER_AREA)
    ink = cv2.adaptiveThreshold(small, 255, cv2.ADAPTIVE_THRESH_MEAN_C,
                                cv2.THRESH_BINARY_INV, 25, 15)
    run = max(9, small.shape[1] // 40)  # joins the letters and words of a line
    lines = cv2.dilate(ink, cv2.getStructuringElement(cv2.MORPH_RECT, (run, 3)))
    contours = cv2.findContours(lines, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)[-2]
    angles, weights = [], []
    for contour in contours:
        x, y, w, h = cv2.boundingRect(contour)
        if w < small.shape[1] * 0.15 or len(contour) < 10:
            continue
        vx, vy = cv2.fitLine(contour, cv2.DIST_L2, 0, 0.01, 0.01).flatten()[:2]
        angle = math.degrees(math.atan2(float(vy), float(vx)))
        angle = (angle + 90) % 180 - 90           # a line has no direction
        if abs(angle) <= MAX_SKEW + 5:
            angles.append(-angle)                 # image rows run downwards
            weights.append(w)
    if len(angles) < 3:
        return 0.0
    order = sorted(range(len(angles)), key=angles.__getitem__)
    half, seen = sum(weights) / 2, 0.0
    for i in order:  # the weighted median: long lines count most
        seen += weights[i]
        if seen >= half:
            return angles[i]
    return 0.0  # pragma: no cover - the loop always reaches half


def _level(rgb, angle: float):
    """The page turned clockwise by *angle* degrees (levelling lines that rise by that
    much), on a canvas that fits it."""
    cv2, np = _cv()
    height, width = rgb.shape[:2]
    matrix = cv2.getRotationMatrix2D((width / 2, height / 2), -angle, 1.0)
    cos, sin = abs(matrix[0, 0]), abs(matrix[0, 1])
    new_w, new_h = round(height * sin + width * cos), round(height * cos + width * sin)
    matrix[0, 2] += new_w / 2 - width / 2
    matrix[1, 2] += new_h / 2 - height / 2
    return cv2.warpAffine(rgb, matrix, (new_w, new_h), flags=cv2.INTER_CUBIC,
                          borderMode=cv2.BORDER_REPLICATE)


# --- whitening ---------------------------------------------------------------------------


def _background(channel):
    """The paper's own brightness across the page: the channel with the ink taken out
    (a maximum over an area larger than any letter), smoothed."""
    cv2, np = _cv()
    height, width = channel.shape
    scale = min(1.0, 600 / max(height, width))
    small = cv2.resize(channel, (max(1, round(width * scale)), max(1, round(height * scale))),
                       interpolation=cv2.INTER_AREA)
    size = _odd(max(small.shape) / 25)
    paper = cv2.dilate(small, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (size, size)))
    paper = cv2.medianBlur(paper, _odd(size * 1.5) if size * 1.5 < 255 else 255)
    return cv2.resize(paper, (width, height), interpolation=cv2.INTER_LINEAR)


def _whiten(channel):
    """The channel with the paper made white and the ink kept dark."""
    cv2, np = _cv()
    paper = _background(channel)
    if float(np.median(paper)) < 40:  # no paper to whiten (a dark photo): left as it is
        return channel
    even = cv2.divide(channel, np.maximum(paper, 1), scale=255)
    dark = float(np.percentile(even, 0.5))
    if dark >= 235:  # a blank page: nothing to stretch
        return even
    stretched = (even.astype("float32") - dark) * (255.0 / (235.0 - dark))
    return np.clip(stretched, 0, 255).astype("uint8")


def _colourful(rgb) -> bool:
    cv2, np = _cv()
    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
    clear = (hsv[:, :, 1] > 80) & (hsv[:, :, 2] > 60)
    return float(clear.mean()) > COLOUR_SHARE


def _tone(rgb, mode: str):
    """The page whitened in the mode asked for (auto: colour only if it has some)."""
    cv2, np = _cv()
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    if mode in ("auto", "color"):
        colour = cv2.merge([_whiten(c) for c in cv2.split(rgb)])
        if mode == "color" or _colourful(colour):
            return colour, "color"
    clean = _whiten(gray)
    if mode == "black_and_white":
        block = _odd(max(clean.shape) / 40)
        bw = cv2.adaptiveThreshold(clean, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                   cv2.THRESH_BINARY, block, 12)
        return bw, "black_and_white"
    return clean, "grayscale"


# --- the PDF -----------------------------------------------------------------------------


def _page_box(width: int, height: int, size: str) -> tuple[float, float]:
    """The page, in points: a standard size the picture is close to (or the one asked
    for), else 11 inches on its long side."""
    aspect = width / height
    if size == "auto":
        for w, h in SIZES.values():
            for pw, ph in ((w, h), (h, w)):
                if abs(aspect - pw / ph) <= 0.04 * pw / ph:
                    return pw, ph
        long_side = 792.0
        return (long_side, long_side / aspect) if aspect >= 1 else (long_side * aspect,
                                                                    long_side)
    w, h = SIZES[size]
    return (h, w) if aspect > 1 else (w, h)


def _store_side(pixels, size: str) -> float:
    """The longest side, in pixels, a picture this shape is stored at (MAX_DPI)."""
    height, width = pixels.shape[:2]
    page_w, page_h = _page_box(width, height, size)
    fit = min(page_w / width, page_h / height)
    return MAX_DPI / 72 * max(width, height) * fit


def _add_page(pdf: pikepdf.Pdf, pixels, mode: str, size: str) -> tuple[int, int]:
    """Put the cleaned picture on a new page of its own, centred; returns its size."""
    from PIL import Image

    cv2, np = _cv()
    height, width = pixels.shape[:2]
    page_w, page_h = _page_box(width, height, size)
    fit = min(page_w / width, page_h / height)  # points per pixel
    draw_w, draw_h = width * fit, height * fit
    pixels = _shrink(pixels, _store_side(pixels, size))  # no more than the page shows
    height, width = pixels.shape[:2]
    x, y = (page_w - draw_w) / 2, (page_h - draw_h) / 2
    stream = io.BytesIO()
    if mode == "black_and_white":
        packed = Image.fromarray(pixels).convert("1", dither=Image.Dither.NONE).tobytes()
        image = pikepdf.Stream(pdf, zlib.compress(packed, 9))
        image.Filter, image.BitsPerComponent = pikepdf.Name.FlateDecode, 1
        image.ColorSpace = pikepdf.Name.DeviceGray
    else:
        Image.fromarray(pixels).save(stream, "JPEG", quality=85, optimize=True)
        image = pikepdf.Stream(pdf, stream.getvalue())
        image.Filter, image.BitsPerComponent = pikepdf.Name.DCTDecode, 8
        image.ColorSpace = (pikepdf.Name.DeviceRGB if mode == "color"
                            else pikepdf.Name.DeviceGray)
    image.Type, image.Subtype = pikepdf.Name.XObject, pikepdf.Name.Image
    image.Width, image.Height = width, height
    content = f"q {draw_w:.3f} 0 0 {draw_h:.3f} {x:.3f} {y:.3f} cm /Scan Do Q\n".encode()
    page = pikepdf.Dictionary(
        Type=pikepdf.Name.Page, MediaBox=[0, 0, page_w, page_h],
        Contents=pdf.make_stream(content),
        Resources=pikepdf.Dictionary(XObject=pikepdf.Dictionary(Scan=image)))
    pdf.pages.append(pikepdf.Page(page))
    return width, height


# --- the inputs ------------------------------------------------------------------------


def _rgb(img):
    """A decoded picture as 8-bit RGB pixels on white: 16-bit greys scaled down (not
    clipped to white), transparency over white paper (not black)."""
    from PIL import Image

    _, np = _cv()
    if img.mode in ("I;16", "I;16B", "I;16L", "I"):
        grey = np.asarray(img).astype("uint32")
        if img.mode == "I" and int(grey.max(initial=0)) <= 255:  # 8-bit values in 32 bits
            grey = grey * 257
        grey = np.clip(grey // 257, 0, 255).astype("uint8")
        return np.dstack([grey] * 3)
    if img.mode in ("RGBA", "LA", "P", "PA"):
        rgba = img.convert("RGBA")
        paper = Image.new("RGB", img.size, "white")
        paper.paste(rgba, mask=rgba.split()[-1])
        return np.asarray(paper)
    return np.asarray(img.convert("RGB"))


def _frames(img) -> int:
    """How many pages a picture holds: every frame of a TIFF (scanners and faxes write
    one page each), only the first of anything else (an animation isn't pages)."""
    return getattr(img, "n_frames", 1) if img.format == "TIFF" else 1


def _plan(paths: list[Path], settings: Settings, pages: str | None,
          password: str | None) -> list[tuple]:
    """Each input's pages, counted and checked before any is cleaned: (path, None,
    frames) for a picture, (path, the decrypted PDF, page indexes) for a PDF."""
    from PIL import Image

    limit = min(MAX_PAGES, settings.max_pages)
    images = [p for p in paths if p.suffix.lower() != ".pdf"]
    if images:
        _pixels(images)  # from their headers: a picture too big is refused here
    plan, total = [], 0
    for path in paths:
        if path.suffix.lower() == ".pdf":
            data, count = _plain_copy(path.read_bytes(), settings, password, exporting=True)
            wanted = list(parse_pages(pages, count, dedupe=True) if pages else range(count))
            plan.append((path, data, wanted))
            total += len(wanted)
        else:
            with Image.open(path) as img:  # the header and frame count only
                frames = _frames(img)
            plan.append((path, None, frames))
            total += frames
        if total > limit:
            raise PdfUserError(f"These files have over {limit} pages; clean fewer at a time")
    return plan


def _pictures(plan: list[tuple]):
    """Each page to clean, as (source, page number or None, RGB pixels), one at a time,
    none longer than MAX_SIDE, and all together within the pixel budget."""
    from PIL import ImageOps

    budget = MAX_TOTAL_PIXELS
    for path, data, which in plan:
        if data is None:
            img = _open_image(path)
            try:
                for frame in range(which):
                    img.seek(frame)
                    if img.format == "JPEG":
                        img.draft("RGB", (MAX_SIDE, MAX_SIDE))  # decode no bigger
                    upright = ImageOps.exif_transpose(img)   # as the phone held it
                    upright.thumbnail((MAX_SIDE, MAX_SIDE))
                    budget -= upright.width * upright.height
                    if budget < 0:
                        break
                    yield path, (frame + 1 if which > 1 else None), _rgb(upright)
            finally:
                img.close()
        else:
            doc = _open(data)
            try:
                for index in which:
                    w, h = _page_size(doc, index)   # points; never draw a huge page big
                    scale = min(PDF_DPI / 72, MAX_SIDE / max(w, h, 1))
                    budget -= round(w * scale) * round(h * scale)
                    if budget < 0:
                        break
                    image = _render(doc, index, scale)
                    yield path, index + 1, _rgb(image)
            finally:
                _close(doc)
        if budget < 0:
            raise PdfUserError(f"These pages add up to over {MAX_TOTAL_PIXELS // 1_000_000} "
                               "million pixels; clean fewer at a time")


def clean_scans(paths: list[Path], out: Path, settings: Settings, mode: str = "auto",
                find_page: bool = True, straighten: bool = True,
                page_size: str = "auto", pages: str | None = None,
                password: str | None = None) -> CleanScansResult:
    """Clean each photo (or page of a PDF) and put them, in order, in one PDF."""
    _cv()
    if not paths:
        raise PdfUserError("Give at least one photo or PDF")
    if len(paths) > MAX_INPUTS:
        raise PdfUserError(f"Give at most {MAX_INPUTS} files at a time")
    bad = [p.name for p in paths if p.suffix.lower() not in IMAGE_EXTS | {".pdf"}]
    if bad:
        raise PdfUserError(f"Not photos or PDFs: {', '.join(bad)}")
    if mode not in MODES:
        raise PdfUserError(f"mode must be one of: {', '.join(MODES)}")
    if page_size not in ("auto", *SIZES):
        raise PdfUserError(f"page_size must be one of: auto, {', '.join(SIZES)}")
    plan = _plan(paths, settings, pages, password)
    deadline = time.monotonic() + TIME_LIMIT_S
    report: list[dict] = []

    def make(made: Path) -> None:
        with pikepdf.new() as pdf:
            for source, page, rgb in _pictures(plan):
                if time.monotonic() > deadline:
                    raise PdfUserError(f"Cleaning took longer than {TIME_LIMIT_S} s; "
                                       "clean fewer pages at a time")
                report.append(_clean_one(pdf, source, page, rgb, mode, find_page,
                                         straighten, page_size))
            pdf.save(made)

    count = _made(out, settings, make)
    return {"output": str(out), "pages": count, "cleaned": report}


def _clean_one(pdf, source: Path, page: int | None, rgb, mode: str, find_page: bool,
               straighten: bool, page_size: str) -> dict:
    cv2, _ = _cv()
    corners = _find_page(rgb) if find_page else None
    if corners is not None:
        rgb = _flatten(rgb, corners)
    rgb = _shrink(rgb, _store_side(rgb, page_size))  # to its stored size before the work
    angle = 0.0
    if straighten:
        angle = _skew(cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY))
        if MIN_SKEW <= abs(angle) <= MAX_SKEW:
            rgb = _shrink(_level(rgb, angle), _store_side(rgb, page_size))
        else:
            angle = 0.0
    toned, used = _tone(rgb, mode)
    if corners is not None:  # what's left of the table at the very edge of the cut
        edge = max(2, round(MARGIN * max(toned.shape[:2])))
        toned[:edge], toned[-edge:], toned[:, :edge], toned[:, -edge:] = 255, 255, 255, 255
    width, height = _add_page(pdf, toned, used, page_size)
    return {"source": str(source), "page": page, "page_found": corners is not None,
            "straightened_degrees": round(angle, 1), "mode": used,
            "width_px": width, "height_px": height}
