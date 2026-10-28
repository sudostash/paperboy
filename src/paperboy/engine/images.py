"""Lowering the resolution of the pictures in a PDF, to make the file smaller.

An image's resolution depends on how large the pages draw it, so the page content is
read (q, Q, cm and Do only, through forms) to find the largest size each image is
drawn at. An image whose every use can't be accounted for is left alone: lowering it
blindly could blur a picture shown large somewhere this didn't see (an annotation's
appearance, a pattern, a Type 3 glyph, a page or form too large or damaged to read).
Only plain 8-bit grey or RGB pictures are re-encoded; masks, palettes, CMYK and the
like stay as they are. Every image passes the same safety checks as extract_images
before it is decoded. All work is bounded: content is measured before it is parsed,
the objects followed are counted, and the call's deadline is checked throughout.
"""
from __future__ import annotations

import io
import logging
import math
import time
import zlib

import pikepdf
from pikepdf import PdfImage
from PIL import Image

from .extract import MAX_IMAGES, MAX_TOTAL_PIXELS, _page_resources, _problem, _size
from .streams import decode, encoded_image, filters_of, parms_of

log = logging.getLogger(__name__)

MAX_CONTENT = 16 * 2**20        # decoded bytes of one page's or form's content
MAX_TOTAL_CONTENT = 64 * 2**20  # decoded content bytes read per call
MAX_STREAM_TOKENS = 500_000     # tokens one page or form may have to be read
MAX_TOKENS = 1_500_000          # tokens parsed per call (each costs a Python object)
MAX_DRAWS = 1_000_000           # q/Q/cm/Do operators followed per call (forms re-entered)
MAX_FORM_DEPTH = 12
MAX_VISITS = 1_000_000          # resource entries looked at per call, finding images
MAX_JPEG_SCANS = 64             # progressive JPEGs with more are slow to decode: kept
MIN_SIDE = 8                    # pixels: images are never made smaller than this
SLACK = 1.1                     # an image within 10% of the resolution asked for is kept
_IDENTITY = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
_CODECS = {"/FlateDecode", "/DCTDecode"}
_GENERAL = {"/FlateDecode", "/ASCII85Decode", "/ASCIIHexDecode"}
# Every token in content ends at whitespace or a delimiter, and a delimiter can be a
# token itself, so whitespace plus twice the delimiters bounds the number of tokens
# without parsing.
_WHITESPACE = (b" ", b"\n", b"\r", b"\t", b"\f", b"\0", b"\x0b")
_DELIMITERS = (b"/", b"[", b"]", b"(", b")", b"<", b">", b"{", b"}", b"%")


class _Unknown(Exception):
    """Where some images are drawn can't be worked out."""


def _mul(m, n):
    """The matrix *m* then *n* (PDF row-vector convention)."""
    a, b, c, d, e, f = m
    p, q, r, s, t, u = n
    return (a * p + b * r, a * q + b * s, c * p + d * r, c * q + d * s,
            e * p + f * r + t, e * q + f * s + u)


def _matrix(values) -> tuple:
    try:
        m = tuple(float(v) for v in values)
    except (TypeError, ValueError):
        raise _Unknown from None
    if len(m) != 6 or not all(math.isfinite(v) for v in m):
        raise _Unknown
    return m


def _streams(contents) -> list:
    streams = list(contents) if isinstance(contents, pikepdf.Array) else \
        ([contents] if contents is not None else [])
    if not all(isinstance(s, pikepdf.Stream) for s in streams):
        raise _Unknown
    return streams


class _Reach:
    """Images reachable from resource dictionaries, through forms. Forms and indirect
    XObject dictionaries are each looked through once per instance, so the work is
    linear in the file however its resources are shared."""

    def __init__(self, deadline: float, cap: int | None = None) -> None:
        self.found: dict[tuple, pikepdf.Stream] = {}
        self.deadline = deadline
        self.visits_left = MAX_VISITS
        self.cap = cap          # most images kept (None: all, as for those left alone)
        self._seen: set = set()

    def _entries(self, group) -> list:
        """The values of a resource group (/XObject, /Font, /Pattern), once each."""
        if not isinstance(group, pikepdf.Dictionary):
            return []
        if group.is_indirect:
            if group.objgen in self._seen:
                return []
            self._seen.add(group.objgen)
        found = []
        for _, obj in group.items():
            self.visits_left -= 1
            if self.visits_left < 0 or time.monotonic() > self.deadline:
                raise _Unknown
            if not isinstance(obj, (pikepdf.Dictionary, pikepdf.Stream)):
                continue
            if obj.is_indirect:
                if obj.objgen in self._seen:
                    continue
                self._seen.add(obj.objgen)
            found.append(obj)
        return found

    def add(self, resources) -> None:
        """The images these resources can draw: through forms, and through Type 3 glyphs
        and patterns (which use these resources when they have none of their own)."""
        pending = [resources]
        while pending:
            res = pending.pop()
            if not isinstance(res, pikepdf.Dictionary):
                continue
            for obj in self._entries(res.get("/XObject")):
                if obj.get("/Subtype") == pikepdf.Name.Image:
                    if self.cap is None or len(self.found) < self.cap:
                        self.found[obj.objgen] = obj
                elif obj.get("/Subtype") == pikepdf.Name.Form:
                    pending.append(obj.get("/Resources"))
            for obj in self._entries(res.get("/Font")):
                if obj.get("/Subtype") == pikepdf.Name.Type3:
                    pending.append(obj.get("/Resources"))
            for obj in self._entries(res.get("/Pattern")):
                pending.append(obj.get("/Resources"))


class _Placements:
    """The largest width and height, in inches, each image is drawn at, and the images
    to leave alone (gathered in *unknown*)."""

    def __init__(self, deadline: float) -> None:
        self.largest: dict[tuple, list[float]] = {}
        self.unknown = _Reach(deadline)
        self.read_forms: set = set()
        self.tokens_left = MAX_TOKENS
        self.content_left = MAX_TOTAL_CONTENT
        self.draws_left = MAX_DRAWS
        self.deadline = deadline
        self._forms: dict[tuple, list] = {}

    def _parse(self, source, streams) -> list:
        """The q/Q/cm/Do operators of a page or form, measured before being parsed."""
        tokens = size = 0
        for stream in streams:   # a page's streams together, however often repeated
            if time.monotonic() > self.deadline:
                raise _Unknown
            data = decode(stream, MAX_CONTENT + 1)
            if data is None:
                raise _Unknown
            size += len(data)
            if size > MAX_CONTENT or size > self.content_left:
                raise _Unknown
            tokens += 1 + sum(data.count(c) for c in _WHITESPACE) \
                + 2 * sum(data.count(c) for c in _DELIMITERS)
        if tokens > MAX_STREAM_TOKENS or tokens > self.tokens_left:
            raise _Unknown
        self.tokens_left -= tokens
        self.content_left -= size
        try:
            return [(str(op), list(operands)) for operands, op
                    in pikepdf.parse_content_stream(source, "q Q cm Do")]
        except Exception:  # noqa: BLE001 - malformed content (pikepdf raises several kinds)
            raise _Unknown from None

    def _hidden_users(self, resources) -> None:
        """Images drawn by this content's Type 3 fonts and patterns, where their size
        isn't followed: left alone. A Type 3 font or pattern without resources of its
        own uses these, so then all of these are left alone."""
        if not isinstance(resources, pikepdf.Dictionary):
            return
        for key in ("/Font", "/Pattern"):
            group = resources.get(key)
            for _, item in group.items() if isinstance(group, pikepdf.Dictionary) else []:
                if not isinstance(item, pikepdf.Dictionary) or (
                        key == "/Font" and item.get("/Subtype") != pikepdf.Name.Type3):
                    continue
                self.unknown.add(item.get("/Resources", resources))

    def walk(self, source, streams, resources, ctm, depth: int = 0,
             path: frozenset = frozenset(), key=None) -> None:
        if key is not None and key in self._forms:
            ops = self._forms[key]
        else:
            ops = self._parse(source, streams)
            if key is not None:
                self._forms[key] = ops   # a form is often drawn many times
        self._hidden_users(resources)
        xobjects = resources.get("/XObject") if isinstance(resources, pikepdf.Dictionary) \
            else None
        stack = []
        for op, operands in ops:
            self.draws_left -= 1
            if self.draws_left < 0 or time.monotonic() > self.deadline:
                raise _Unknown
            if op == "q":
                stack.append(ctm)
            elif op == "Q":
                ctm = stack.pop() if stack else ctm
            elif op == "cm":
                ctm = _mul(_matrix(operands), ctm)
            else:  # Do: the only other operator parsed
                if not operands or not isinstance(xobjects, pikepdf.Dictionary):
                    continue
                obj = xobjects.get(str(operands[0]))
                if not isinstance(obj, pikepdf.Stream):
                    continue
                if obj.get("/Subtype") == pikepdf.Name.Image:
                    a, b, c, d = ctm[:4]
                    size = self.largest.setdefault(obj.objgen, [0.0, 0.0])
                    size[0] = max(size[0], math.hypot(a, b) / 72)
                    size[1] = max(size[1], math.hypot(c, d) / 72)
                elif obj.get("/Subtype") == pikepdf.Name.Form:
                    if depth >= MAX_FORM_DEPTH or obj.objgen in path:
                        raise _Unknown
                    matrix = _matrix(obj.get("/Matrix", _IDENTITY))
                    # A form without resources uses those of what draws it (PDF 1.1).
                    self.walk(obj, [obj], obj.get("/Resources", resources),
                              _mul(matrix, ctm), depth + 1, path | {obj.objgen}, obj.objgen)
                    self.read_forms.add(obj.objgen)


def _user_unit(page: pikepdf.Dictionary) -> float:
    unit = page.get("/UserUnit", 1)
    try:
        unit = float(unit)
    except (TypeError, ValueError):
        raise _Unknown from None
    if not math.isfinite(unit) or unit <= 0:
        raise _Unknown
    return unit


def _appearances(page: pikepdf.Dictionary) -> list:
    """The appearance streams of a page's annotations (any state)."""
    found = []
    annots = page.get("/Annots")
    for annot in annots if isinstance(annots, pikepdf.Array) else []:
        ap = annot.get("/AP") if isinstance(annot, pikepdf.Dictionary) else None
        for _, entry in ap.items() if isinstance(ap, pikepdf.Dictionary) else []:
            if isinstance(entry, pikepdf.Stream):
                found.append(entry)
            elif isinstance(entry, pikepdf.Dictionary):
                found += [s for _, s in entry.items() if isinstance(s, pikepdf.Stream)]
    return found


def placements(pdf: pikepdf.Pdf, deadline: float) -> tuple[dict, set | None]:
    """The largest size (inches) each image is drawn at, and the images to leave alone.
    None instead of that set means leave every image alone (the file couldn't be gone
    through in time, or is damaged)."""
    try:
        return _placements(pdf, deadline)
    except (_Unknown, pikepdf.PdfError):
        return {}, None


def _placements(pdf: pikepdf.Pdf, deadline: float) -> tuple[dict, set]:
    found = _Placements(deadline)
    for page in pdf.pages:
        resources = _page_resources(page.obj)
        try:
            unit = _user_unit(page.obj)
            found.walk(page, _streams(page.obj.get("/Contents")), resources,
                       (unit, 0.0, 0.0, unit, 0.0, 0.0))
        except _Unknown:
            found.unknown.add(resources)
        # Appearances are scaled to their annotation, not to anything followed here.
        for appearance in _appearances(page.obj):
            found.unknown.add(appearance.get("/Resources", resources))
    # Forms nothing above drew (field widgets off any page, say), patterns and Type 3
    # fonts elsewhere in the file.
    for n, obj in enumerate(pdf.objects):
        if n % 1000 == 0 and time.monotonic() > deadline:
            raise _Unknown
        if not isinstance(obj, (pikepdf.Dictionary, pikepdf.Stream)):
            continue
        form = isinstance(obj, pikepdf.Stream) and obj.get("/Subtype") == pikepdf.Name.Form
        if form and obj.objgen in found.read_forms:
            continue
        if form or "/PatternType" in obj or obj.get("/Subtype") == pikepdf.Name.Type3:
            found.unknown.add(obj.get("/Resources"))
    return found.largest, set(found.unknown.found)


def _colour(image: pikepdf.Stream) -> str | None:
    """The PIL mode an image decodes to, if it's one this re-encodes (8-bit grey or
    RGB, no palette, no masks other than a soft mask, standard decoding)."""
    if image.get("/ImageMask") or "/Mask" in image or "/Decode" in image:
        return None
    if image.get("/BitsPerComponent") != 8:
        return None
    space = image.get("/ColorSpace")
    if space == pikepdf.Name.DeviceGray:
        return "L"
    if space == pikepdf.Name.DeviceRGB:
        return "RGB"
    if isinstance(space, pikepdf.Array) and len(space) == 2 \
            and space[0] == pikepdf.Name.ICCBased and isinstance(space[1], pikepdf.Stream):
        return {1: "L", 3: "RGB"}.get(space[1].get("/N"))
    return None


def _codec_ok(image: pikepdf.Stream) -> bool:
    """Plain, Flate or JPEG data, possibly under text encodings or Flate. JPEGs with a
    colour transform setting (which decoders may not honour) are left out."""
    filters = filters_of(image)
    if any("/ColorTransform" in p for p in parms_of(image)):
        return False
    return len(filters) <= 4 and (not filters or filters[-1] in _CODECS) \
        and all(f in _GENERAL for f in filters[:-1])


def _slow_jpeg(image: pikepdf.Stream) -> bool:
    """A JPEG with so many progressive scans that decoding it would take long."""
    if filters_of(image)[-1:] != ["/DCTDecode"]:
        return False
    data = encoded_image(image, 64 * 2**20)
    return data is None or data.count(b"\xff\xda") > MAX_JPEG_SCANS


def _soft_mask(image: pikepdf.Stream) -> pikepdf.Stream | None | bool:
    """The image's soft mask if it has one this can resize, None if it has none, False
    if it has one this can't."""
    mask = image.get("/SMask")
    if mask is None:
        return None
    if (isinstance(mask, pikepdf.Stream) and mask.get("/ColorSpace") == pikepdf.Name.DeviceGray
            and mask.get("/BitsPerComponent") == 8 and "/Matte" not in mask
            and "/Decode" not in mask and _codec_ok(mask) and not _slow_jpeg(mask)):
        return mask
    return False


def _decode(image: pikepdf.Stream, mode: str) -> Image.Image | None:
    """The picture alone: newer pikepdf would merge a soft mask in, so it's detached
    while decoding (the mask is resized on its own)."""
    mask = image.get("/SMask")
    if mask is not None:
        del image["/SMask"]
    try:
        picture = PdfImage(image).as_pil_image()
        picture.load()
    except Exception as exc:  # noqa: BLE001 - an image that won't decode is kept as it is
        log.info("image not decoded: %r", exc)
        return None
    finally:
        if mask is not None:
            image.SMask = mask
    return picture if picture.mode == mode and picture.size == _size(image) else None


def _jpeg(picture: Image.Image, quality: int) -> bytes:
    buf = io.BytesIO()
    picture.save(buf, "JPEG", quality=quality, optimize=True)
    return buf.getvalue()


def candidates(pdf: pikepdf.Pdf, largest: dict, unknown: set | None, deadline: float
               ) -> list[tuple[pikepdf.Stream, float]]:
    """The images whose resolution could be lowered, with the resolution (dpi) they are
    drawn at now: on the pages, drawn at a known size, and of a kind this re-encodes."""
    if unknown is None:
        return []
    reach = _Reach(deadline, cap=MAX_IMAGES)
    try:
        for page in pdf.pages:
            reach.add(_page_resources(page.obj))
    except _Unknown:
        pass   # images not reached in time are left alone
    found = []
    for key, image in reach.found.items():
        size, drawn = _size(image), largest.get(key)
        if size is None or drawn is None or key in unknown or min(drawn) <= 0:
            continue
        if _colour(image) is None or _soft_mask(image) is False or not _codec_ok(image) \
                or _slow_jpeg(image):
            continue
        found.append((image, min(size[0] / drawn[0], size[1] / drawn[1])))
    return found


def lower_resolution(pdf: pikepdf.Pdf, dpi: int, quality: int, found: list,
                     deadline: float, jpeg_all: bool) -> dict:
    """Re-encode, at no more than *dpi*, the images of *found* (from candidates, for
    this same file) drawn sharper than that. JPEG pictures stay JPEG (at *quality*);
    others stay lossless unless *jpeg_all*. An image is replaced only if that makes it
    smaller. Returns counts."""
    resampled = kept = 0
    pixels_left = MAX_TOTAL_PIXELS
    for image, current in found:
        if current <= dpi * SLACK:
            continue
        size, mask = _size(image), _soft_mask(image)
        scale = dpi / current
        new = (max(MIN_SIDE, round(size[0] * scale)), max(MIN_SIDE, round(size[1] * scale)))
        if new[0] >= size[0] or new[1] >= size[1]:
            continue
        if time.monotonic() > deadline or _problem(image):   # checks the mask's size too
            kept += 1
            continue
        mask_size = _size(mask) if mask is not None else (0, 0)
        pixels = size[0] * size[1] + mask_size[0] * mask_size[1]
        if pixels > pixels_left:
            kept += 1
            continue
        try:
            replaced = _shrink(pdf, image, mask, new, quality, jpeg_all)
        except pikepdf.PdfError as exc:   # damaged data: the image is kept as it is
            log.info("image not re-encoded: %r", exc)
            replaced = False
        pixels_left -= pixels
        if replaced:
            resampled += 1
        else:
            kept += 1
    return {"resampled": resampled, "kept": kept}


def _shrink(pdf: pikepdf.Pdf, image: pikepdf.Stream, mask, new: tuple[int, int],
            quality: int, jpeg_all: bool) -> bool:
    picture = _decode(image, _colour(image))
    if picture is None:
        return False
    jpeg = jpeg_all or filters_of(image)[-1:] == ["/DCTDecode"]
    smaller = picture.resize(new, Image.Resampling.LANCZOS)
    data = _jpeg(smaller, quality) if jpeg else zlib.compress(smaller.tobytes(), 9)
    mask_data = None
    if mask is not None:
        mask_picture = _decode(mask, "L")
        if mask_picture is None:
            return False
        mask_data = zlib.compress(mask_picture.resize(new, Image.Resampling.LANCZOS)
                                  .tobytes(), 9)
    # The old mask isn't counted: it may be shared, and then stays in the file.
    if len(data) + len(mask_data or b"") >= len(image.read_raw_bytes()):
        return False
    _replace(image, data, new, jpeg)
    if mask is not None:
        # A new mask of its own: the old one may be shared with other pictures.
        image.SMask = pdf.make_stream(mask_data, Type=pikepdf.Name.XObject,
                                      Subtype=pikepdf.Name.Image, Width=new[0],
                                      Height=new[1], ColorSpace=pikepdf.Name.DeviceGray,
                                      BitsPerComponent=8, Filter=pikepdf.Name.FlateDecode)
    return True


def _replace(image: pikepdf.Stream, data: bytes, size: tuple[int, int], jpeg: bool) -> None:
    if "/DecodeParms" in image:   # the old data's predictor doesn't apply to the new
        del image["/DecodeParms"]
    image.write(data, filter=pikepdf.Name.DCTDecode if jpeg else pikepdf.Name.FlateDecode)
    image.Width, image.Height = size
