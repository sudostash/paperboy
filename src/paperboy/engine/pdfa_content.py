"""What a PDF's pages actually draw, for convert_to_pdfa's own checks.

PDF/A asks about use, not presence: a font needs its program embedded only if text is
drawn with it (and not invisibly, as OCR layers are), and device colour spaces matter
only if something is painted in them. This reads page content (and the forms,
annotation appearances and Type 3 glyphs it draws) to find out, measuring every stream
before it is parsed and within one budget per call, so a hostile file can't make the
reading slow or large: it stops with TooComplex instead.
"""
from __future__ import annotations

import time

import pikepdf

from .streams import decode

MAX_STREAM = 16 * 2**20          # decoded bytes of one content stream
MAX_TOTAL = 128 * 2**20          # decoded content bytes read per call (repeats count)
MAX_STREAM_TOKENS = 1_000_000
MAX_TOKENS = 4_000_000           # per call
MAX_DRAWS = 1_000_000            # forms, glyphs and appearances entered per call
MAX_DEPTH = 12
MAX_RESOURCE_DEPTH = 32
INVISIBLE = 3                    # text render mode: neither filled nor stroked
EMBEDDED = ("/FontFile", "/FontFile2", "/FontFile3")
SHOW = {"Tj", "TJ", "'", '"'}
_WHITESPACE = (b" ", b"\n", b"\r", b"\t", b"\f", b"\0", b"\x0b")
_DELIMITERS = (b"/", b"[", b"]", b"(", b")", b"<", b">", b"{", b"}", b"%")
_DEVICE = {"/DeviceRGB": "RGB", "/RGB": "RGB", "/DeviceCMYK": "CMYK", "/CMYK": "CMYK"}


class TooComplex(Exception):
    """The content is too big, too deep or can't be measured: not checked here."""


def _key(obj):
    return obj.objgen if obj.is_indirect else id(obj)


def _font_name(font) -> str:
    name = font.get("/BaseFont")
    return str(name)[1:] if isinstance(name, pikepdf.Name) else "unnamed"


def embedded(font) -> bool:
    """Whether drawing with *font* needs nothing outside the file (Type 3 glyphs are
    content; a composite font is as embedded as its descendant)."""
    subtype = font.get("/Subtype")
    if subtype == pikepdf.Name.Type3:
        return True
    if subtype == pikepdf.Name.Type0:
        kids = font.get("/DescendantFonts")
        font = kids[0] if isinstance(kids, pikepdf.Array) and len(kids) else None
        if not isinstance(font, pikepdf.Dictionary):
            return False
    descriptor = font.get("/FontDescriptor")
    return isinstance(descriptor, pikepdf.Dictionary) and \
        any(isinstance(descriptor.get(k), pikepdf.Stream) for k in EMBEDDED)


def page_resources(page: pikepdf.Dictionary):
    node, depth = page, 0
    while isinstance(node, pikepdf.Dictionary) and depth < MAX_RESOURCE_DEPTH:
        resources = node.get("/Resources")
        if isinstance(resources, pikepdf.Dictionary):
            return resources
        node, depth = node.get("/Parent"), depth + 1
    return None


class Usage:
    """Fonts drawn visibly without their program, device colours painted, and inline
    images PDF/A forbids, across everything read with one instance."""

    def __init__(self, deadline: float):
        self.deadline = deadline
        self.bytes_left, self.tokens_left, self.draws_left = MAX_TOTAL, MAX_TOKENS, MAX_DRAWS
        self.unembedded: set[str] = set()
        self.colours: set[str] = set()
        self.problems: set[str] = set()
        self._glyphs_read: set = set()

    def _spend_draw(self) -> None:
        self.draws_left -= 1
        if self.draws_left < 0 or time.monotonic() > self.deadline:
            raise TooComplex

    def _measure(self, streams: list) -> None:
        """Count what parsing *streams* together will cost; each distinct stream is
        decoded once (however often it is listed), cut at what the budget has left."""
        counts: dict = {}
        distinct: dict = {}
        for stream in streams:
            if not isinstance(stream, pikepdf.Stream):
                raise TooComplex
            key = _key(stream)
            counts[key] = counts.get(key, 0) + 1
            distinct[key] = stream
        size = tokens = 0
        for key, stream in distinct.items():
            if time.monotonic() > self.deadline:
                raise TooComplex
            room = min(MAX_STREAM, self.bytes_left) - size
            data = decode(stream, max(room, 0) + 1)
            if data is None or len(data) > room:
                raise TooComplex
            size += len(data) * counts[key]
            tokens += (1 + sum(data.count(c) for c in _WHITESPACE)
                       + 2 * sum(data.count(c) for c in _DELIMITERS)) * counts[key]
            if size > MAX_STREAM or tokens > MAX_STREAM_TOKENS or size > self.bytes_left \
                    or tokens > self.tokens_left:
                raise TooComplex
        self.bytes_left -= size
        self.tokens_left -= tokens

    def page(self, page: pikepdf.Page) -> None:
        contents = page.obj.get("/Contents")
        streams = list(contents) if isinstance(contents, pikepdf.Array) else \
            ([contents] if contents is not None else [])
        self._group(page.obj)
        if streams:
            self._measure(streams)
            self._run(page, page_resources(page.obj), [None, 0], 0)
        annots = page.obj.get("/Annots")
        for annot in annots if isinstance(annots, pikepdf.Array) else []:
            ap = annot.get("/AP") if isinstance(annot, pikepdf.Dictionary) else None
            normal = ap.get("/N") if isinstance(ap, pikepdf.Dictionary) else None
            looks = [normal] if isinstance(normal, pikepdf.Stream) else \
                [normal[k] for k in normal.keys()] if isinstance(normal, pikepdf.Dictionary) \
                else []
            for look in looks:
                if isinstance(look, pikepdf.Stream):
                    self.form(look, None, [None, 0], 0)

    def form(self, form: pikepdf.Stream, inherited, state: list, depth: int) -> None:
        self._spend_draw()
        if depth > MAX_DEPTH:
            raise TooComplex
        self._measure([form])
        resources = form.get("/Resources")
        resources = resources if isinstance(resources, pikepdf.Dictionary) else inherited
        self._group(form, resources)
        self._run(form, resources, list(state), depth)

    def _group(self, holder, resources=None) -> None:
        """A transparency group's blending colour space is painted in too."""
        group = holder.get("/Group")
        if isinstance(group, pikepdf.Dictionary):
            self._space(group.get("/CS"), resources)

    def _run(self, source, resources, state: list, depth: int) -> None:
        try:
            instructions = pikepdf.parse_content_stream(source)
        except Exception:  # noqa: BLE001 - malformed content (pikepdf raises several kinds)
            raise TooComplex from None
        stack: list = []
        for item in instructions:
            op = str(item.operator)
            if op == "INLINE IMAGE":
                self._inline(item.iimage, resources)
                continue
            operands = item.operands
            if op == "q":
                stack.append(list(state))
            elif op == "Q":
                if stack:
                    state = stack.pop()
            elif op == "Tf" and operands:
                state[0] = self._lookup(resources, "/Font", operands[0])
            elif op == "Tr" and operands and isinstance(operands[0], int):
                state[1] = int(operands[0])
            elif op in SHOW:
                self._show(state, resources, depth)
            elif op in ("rg", "RG"):
                self.colours.add("RGB")
            elif op in ("k", "K"):
                self.colours.add("CMYK")
            elif op in ("cs", "CS") and operands:
                self._space(operands[0], resources)
            elif op == "sh" and operands:
                shading = self._lookup(resources, "/Shading", operands[0])
                if isinstance(shading, (pikepdf.Dictionary, pikepdf.Stream)):
                    self._space(shading.get("/ColorSpace"), resources)
            elif op == "gs" and operands:
                self._gstate(self._lookup(resources, "/ExtGState", operands[0]), resources,
                             state, depth)
            elif op in ("scn", "SCN") and operands and isinstance(operands[-1], pikepdf.Name):
                self._pattern(self._lookup(resources, "/Pattern", operands[-1]), resources,
                              depth)
            elif op == "Do" and operands:
                self._do(self._lookup(resources, "/XObject", operands[0]), resources, state,
                         depth)

    def _gstate(self, gstate, resources, state: list, depth: int) -> None:
        if not isinstance(gstate, pikepdf.Dictionary):
            return
        font = gstate.get("/Font")  # [font size], as Tf
        if isinstance(font, pikepdf.Array) and len(font) and \
                isinstance(font[0], pikepdf.Dictionary):
            state[0] = font[0]
        mask = gstate.get("/SMask")
        group = mask.get("/G") if isinstance(mask, pikepdf.Dictionary) else None
        if isinstance(group, pikepdf.Stream):
            self.form(group, resources, [None, 0], depth + 1)

    def _pattern(self, pattern, resources, depth: int) -> None:
        if isinstance(pattern, pikepdf.Stream):  # tiling: content of its own
            self.form(pattern, resources, [None, 0], depth + 1)
        elif isinstance(pattern, pikepdf.Dictionary):  # shading
            shading = pattern.get("/Shading")
            if isinstance(shading, (pikepdf.Dictionary, pikepdf.Stream)):
                self._space(shading.get("/ColorSpace"), resources)

    def _lookup(self, resources, kind: str, name):
        table = resources.get(kind) if isinstance(resources, pikepdf.Dictionary) else None
        if not isinstance(table, pikepdf.Dictionary) or not isinstance(name, pikepdf.Name):
            return None
        return table.get(name)

    def _show(self, state: list, resources, depth: int) -> None:
        font, mode = state
        if not isinstance(font, pikepdf.Dictionary) or mode == INVISIBLE:
            return
        if not embedded(font):
            self.unembedded.add(_font_name(font))
        elif font.get("/Subtype") == pikepdf.Name.Type3 and _key(font) not in self._glyphs_read:
            self._glyphs_read.add(_key(font))
            procs = font.get("/CharProcs")
            own = font.get("/Resources")
            for name in list(procs.keys()) if isinstance(procs, pikepdf.Dictionary) else []:
                glyph = procs[name]
                if isinstance(glyph, pikepdf.Stream):
                    self.form(glyph, own if isinstance(own, pikepdf.Dictionary) else resources,
                              [None, 0], depth + 1)

    def _do(self, xobject, resources, state: list, depth: int) -> None:
        if not isinstance(xobject, pikepdf.Stream):
            return
        subtype = xobject.get("/Subtype")
        if subtype == pikepdf.Name.Form:
            self.form(xobject, resources, state, depth + 1)
        elif subtype == pikepdf.Name.Image and xobject.get("/ImageMask") is not True:
            self._space(xobject.get("/ColorSpace"), resources)

    def _space(self, space, resources, depth: int = 0) -> None:
        """Note the device colour spaces *space* paints in (as itself, or as the base
        or alternate of another space)."""
        if depth > 4:
            return
        if isinstance(space, pikepdf.Name):
            device = _DEVICE.get(str(space))
            if device:
                self.colours.add(device)
                return
            named = self._lookup(resources, "/ColorSpace", space)
            if named is not None and depth == 0:
                self._space(named, resources, depth + 1)
        elif isinstance(space, pikepdf.Array) and len(space) > 1:
            family = space[0]
            if family in (pikepdf.Name.Indexed, pikepdf.Name.Pattern):
                self._space(space[1], resources, depth + 1)
            elif family in (pikepdf.Name.Separation, pikepdf.Name.DeviceN) and len(space) > 2:
                self._space(space[2], resources, depth + 1)  # the alternate space

    def _inline(self, image, resources) -> None:
        try:
            info = image.obj
        except Exception:  # noqa: BLE001 - a malformed inline image
            self.problems.add("inline pictures PDF/A doesn't allow")
            return
        filters = info.get("/Filter")
        filters = list(filters) if isinstance(filters, pikepdf.Array) else [filters]
        # Older pikepdf leaves abbreviations in filter arrays, and reads /I (Interpolate)
        # as /Indexed, which isn't an inline image key.
        lzw = any(str(f) in ("/LZWDecode", "/LZW") for f in filters if f is not None)
        smooth = info.get("/Interpolate") is True or info.get("/Indexed") is True
        if lzw or smooth:
            self.problems.add("inline pictures PDF/A doesn't allow")
        if info.get("/ImageMask") is not True:
            self._space(info.get("/ColorSpace"), resources)
