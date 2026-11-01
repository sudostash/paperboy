"""PDF/A-2b: a copy for long-term archiving, made with pikepdf and checked by veraPDF.

PDF/A (ISO 19005) is PDF that will look the same in any viewer decades from now: the
file must carry everything it needs (fonts, a colour profile) and nothing that
depends on the outside world or on code (scripts, attachments, encryption).

This module never redraws a page. It adds what PDF/A requires (an sRGB output intent,
PDF/A metadata), removes what it forbids (scripts and other actions, attachments,
annotations that can't be shown, encryption), and refuses what it can't fix without
changing how pages look, chiefly fonts that aren't embedded and CMYK colour. When the
veraPDF validator is installed (an optional system dependency; GPL/MPL), the result is
checked against the PDF/A-2b profile before it is written, and written only if it
passes. Rules follow the veraPDF profile for ISO 19005-2 (rule ids are clause-test).

Hostile input is bounded throughout: every walk counts what it visits and checks the
deadline, streams are measured before anything decodes them, page content is read only
by the budgeted reader in pdfa_content, and the file is saved without decoding streams.
"""
from __future__ import annotations

import functools
import io
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import warnings
import zlib
from pathlib import Path

import pikepdf

from ..config import Settings
from ..errors import DependencyError, PdfUserError
from ..results import PdfaResult
from .base import open_pdf, require_copy_permission, signature_state
from .pdfa_content import TooComplex, Usage
from .redact import _field_of
from .streams import _DAMAGED as DAMAGED
from .streams import _stage as decode_layer
from .streams import decode, filters_of, lzw, within

STANDARD = "PDF/A-2b"
VALIDATE_TIME_LIMIT_S = 300
CONVERT_TIME_LIMIT_S = 120
MAX_OBJECTS = 1_000_000        # objects visited fixing the document
MAX_REPORT = 20                # rules or font names listed in a refusal
MAX_NAME = 64                  # characters of a name from the file shown in a message
MAX_MESSAGE = 3000
MAX_VERAPDF_OUTPUT = 32 * 2**20
MAX_INTENTS = 16               # output intents looked at (files have one, or two)
MAX_PROFILE = 4 * 2**20        # an ICC profile (sRGB is 3 KB; big print ones about 1 MB)
MAX_XMP = 4 * 2**20
MAX_APPEARANCE = 2**20         # an appearance qpdf draws for a form field
MAX_LZW = 64 * 2**20           # decoded bytes of a stream converted from LZW
MAX_LZW_INPUT = 8 * 2**20      # LZW is decoded in Python to measure it
MAX_STRING = 32767             # 6.1.13-3, bytes
VERAPDF_MEMORY = "-Xmx1g"
SRGB = "sRGB IEC61966-2.1"
PAGE_SIZE = (3, 14400)         # 6.1.13-11, in default user space units
BOXES = ("/MediaBox", "/CropBox", "/BleedBox", "/TrimBox", "/ArtBox")

# 6.5.1-1 allows only these actions; 6.5.1-2 only these named ones.
ALLOWED_ACTIONS = {"/GoTo", "/GoToR", "/GoToE", "/Thread", "/URI", "/Named", "/SubmitForm"}
NAMED_ACTIONS = {"/NextPage", "/PrevPage", "/FirstPage", "/LastPage"}
# 6.3.1-1: the annotation types of ISO 32000-1 less 3D, Sound, Screen and Movie (and
# FileAttachment: PDF/A-2 allows embedded files only if they are PDF/A themselves).
ANNOTATIONS = {"/Text", "/Link", "/FreeText", "/Line", "/Square", "/Circle", "/Polygon",
               "/PolyLine", "/Highlight", "/Underline", "/Squiggly", "/StrikeOut", "/Stamp",
               "/Caret", "/Ink", "/Popup", "/Widget", "/PrinterMark", "/TrapNet",
               "/Watermark", "/Redact"}
INTENTS = {"/RelativeColorimetric", "/AbsoluteColorimetric", "/Perceptual", "/Saturation"}
BLEND_MODES = {"/Normal", "/Compatible", "/Multiply", "/Screen", "/Overlay", "/Darken",
               "/Lighten", "/ColorDodge", "/ColorBurn", "/HardLight", "/SoftLight",
               "/Difference", "/Exclusion", "/Hue", "/Saturation", "/Color", "/Luminosity"}
PRIMARIES = {"/Cyan", "/Magenta", "/Yellow", "/Black", "/Red", "/Green", "/Blue", "/Gray",
             "/Default"}
GENERAL_FILTERS = {"/FlateDecode", "/ASCIIHexDecode", "/ASCII85Decode", "/LZWDecode",
                   "/RunLengthDecode"}
# Annotation flags (ISO 32000-1 table 165).
INVISIBLE, HIDDEN, PRINT, NO_VIEW, TOGGLE_NO_VIEW = 1, 2, 4, 32, 256
# XMP properties carried over from the document's own metadata (standard schemas only:
# properties of other schemas would need extension schema descriptions, 6.6.2.3.1).
KEPT_XMP = ("dc:title", "dc:creator", "dc:description", "dc:subject", "dc:rights",
            "dc:language", "pdf:Keywords", "pdf:Producer", "xmp:CreatorTool",
            "xmp:CreateDate", "xmp:ModifyDate")
XMP_DATES = {"xmp:CreateDate", "xmp:ModifyDate"}
_ISO_DATE = re.compile(r"\d{4}(-\d\d(-\d\d(T\d\d:\d\d(:\d\d(\.\d+)?)?"
                       r"(Z|[+-]\d\d:\d\d)?)?)?)?")
# Environment veraPDF gets: enough to find Java; nothing that changes what it prints or
# lifts its memory cap (_JAVA_OPTIONS, JDK_JAVA_OPTIONS, JAVA_VERSION, ...).
VERAPDF_ENV = ("PATH", "HOME", "JAVA_HOME", "LANG", "LC_ALL", "TMPDIR", "SYSTEMROOT")


_CONTAINERS = (pikepdf.Array, pikepdf.Dictionary, pikepdf.Stream)


def _short(value: pikepdf.String) -> pikepdf.String:
    """A string cut to MAX_STRING bytes, as bytes (it may not be text); UTF-16 text is
    cut on a character boundary."""
    data = bytes(value)[:MAX_STRING]
    if data.startswith(b"\xfe\xff") and len(data) % 2:
        data = data[:-1]
    return pikepdf.String(data)


def _clean(text: str) -> str:
    """A name from the file, safe to show: printable characters only, cut short."""
    text = "".join(ch if ch.isprintable() else "?" for ch in text)
    return text if len(text) <= MAX_NAME else text[:MAX_NAME - 1] + "…"


class _Fixer:
    """Fixes what can be fixed and notes what can't, counting all it visits."""

    def __init__(self, pdf: pikepdf.Pdf, deadline: float):
        self.pdf, self.deadline = pdf, deadline
        self.changes: dict[str, int] = {}
        self.problems: dict[str, set[str]] = {}
        self.visits = MAX_OBJECTS
        self.seen: set[tuple[int, int]] = set()

    def change(self, what: str, n: int = 1) -> None:
        self.changes[what] = self.changes.get(what, 0) + n

    def problem(self, what: str, detail: str = "") -> None:
        self.problems.setdefault(what, set()).add(_clean(detail) if detail else "")

    def spend(self, n: int = 1) -> None:
        """More objects looked at; stop when there have been too many or too long."""
        self.visits -= n
        if self.visits < 0:
            raise PdfUserError("This PDF has too many objects to convert to PDF/A "
                               "within the limits")
        _check_time(self.deadline)

    def walk(self, root) -> None:
        catalog = self.pdf.Root.objgen
        stack = [root]
        while stack:
            obj = stack.pop()
            if obj.is_indirect:  # arrays too: one can contain itself
                if obj.objgen in self.seen:
                    continue
                self.seen.add(obj.objgen)
            self.spend()
            if isinstance(obj, pikepdf.Array):
                self.spend(len(obj) // 1000)  # long arrays of numbers cost time too
                for n, v in enumerate(obj):
                    if isinstance(v, _CONTAINERS):
                        stack.append(v)
                    elif isinstance(v, pikepdf.String) and len(bytes(v)) > MAX_STRING:
                        obj[n] = _short(v)  # 6.1.13-3, in arrays too (choices, names)
                        self.change("shortened over-long text")
                continue
            self.fix(obj, obj.is_indirect and obj.objgen == catalog)
            stack.extend(v for k, v in obj.items() if k != "/Parent"
                         and isinstance(v, _CONTAINERS))

    def fix(self, d, catalog: bool) -> None:
        if "/AA" in d:  # 6.5.2, 6.4.1: no triggered actions anywhere
            del d["/AA"]
            self.change("removed actions that run on events")
        for key in ("/A", "/OpenAction"):  # always actions (or, for OpenAction, a place)
            if _forbidden(d.get(key), True):
                del d[key]
                self.change("removed scripts and other actions PDF/A forbids")
        if _is_action(d):  # an action's /Next are actions (an outline item's is its sibling)
            nxt = d.get("/Next")
            if isinstance(nxt, pikepdf.Array) and any(_forbidden(a, True) for a in nxt):
                d.Next = pikepdf.Array([a for a in nxt if not _forbidden(a, True)])
                self.change("removed scripts and other actions PDF/A forbids")
            elif _forbidden(nxt, True):
                del d["/Next"]
                self.change("removed scripts and other actions PDF/A forbids")
        self._shorten(d)
        if "/EF" in d:  # 6.8-5: attached files only if they are PDF/A themselves
            del d["/EF"]
            self.change("removed attached files")
        if "/AF" in d:  # associated files belong to PDF/A-3
            del d["/AF"]
            self.change("removed associated files (a PDF/A-3 feature)")
        if "/Metadata" in d and not catalog:  # it would have to meet the XMP rules too
            del d["/Metadata"]
            self.change("removed metadata attached to pages and pictures")
        if isinstance(d, pikepdf.Stream):
            self._stream(d)
        if d.get("/Type") == pikepdf.Name.FontDescriptor:
            for key in ("/CharSet", "/CIDSet"):  # optional, and must be exact if present
                if key in d:
                    del d[key]
        if d.get("/Type") == pikepdf.Name.ExtGState or self._is_gstate(d):
            self._gstate(d)

    def _shorten(self, d) -> None:
        """6.1.13-3: strings of 32,767 bytes at most (comments, field values, titles)."""
        for key, value in list(d.items()):
            if isinstance(value, pikepdf.String) and len(bytes(value)) > MAX_STRING:
                d[key] = _short(value)
                self.change("shortened over-long text")

    def _stream(self, s: pikepdf.Stream) -> None:
        if any(k in s for k in ("/F", "/FFilter", "/FDecodeParms")):  # 6.1.7.1-3
            self.problem("streams whose data is in another file")
        filters = filters_of(s)
        if "/LZWDecode" in filters:  # 6.1.7.2-1
            self._unlzw(s, filters)
        subtype = s.get("/Subtype")
        if subtype == pikepdf.Name.Image:
            for key in ("/Alternates", "/OPI"):  # 6.2.8-1, 6.2.8-2
                if key in s:
                    del s[key]
                    self.change("removed alternate versions of pictures")
            if s.get("/Interpolate") is True:  # 6.2.8-3
                s.Interpolate = False
                self.change("turned off picture smoothing (Interpolate)")
            bits = s.get("/BitsPerComponent")
            if s.get("/ImageMask") is True:
                if bits not in (None, 1):
                    self.problem("pictures with an unusual number of bits per colour")
            elif bits is not None and bits not in (1, 2, 4, 8, 16):
                self.problem("pictures with an unusual number of bits per colour")
        elif subtype == pikepdf.Name.Form:
            for key in ("/OPI", "/PS", "/Ref"):  # 6.2.9-1, 6.2.9-2
                if key in s:
                    del s[key]
                    self.change("removed PostScript and references to other files")
            if s.get("/Subtype2") == pikepdf.Name.PS:
                del s["/Subtype2"]
        elif subtype == pikepdf.Name.PS:  # 6.2.9-3
            self.problem("PostScript drawings")

    def _unlzw(self, s: pikepdf.Stream, filters: list[str]) -> None:
        """Re-compress LZW data with Flate (decoding only the general filters, and only
        what is measured to stay small)."""
        # qpdf can only undo all of a stream's filters, so an image codec after LZW
        # (rare) can't be kept.
        data = self._unfilter(s, filters) if all(f in GENERAL_FILTERS for f in filters) \
            else None
        if data is None:
            self.problem("data compressed with LZW that can't be converted here")
            return
        if _predicted(s):  # qpdf undoes the predictor too, now the size is known
            try:
                data = s.read_bytes()
            except pikepdf.PdfError:
                self.problem("data compressed with LZW that can't be converted here")
                return
        # Predictors were undone with the rest; write drops the old /DecodeParms.
        s.write(zlib.compress(data), filter=pikepdf.Name.FlateDecode)
        self.change("re-compressed LZW data with Flate")

    def _unfilter(self, s: pikepdf.Stream, general: list[str]) -> bytes | None:
        """The stream's data with *general* undone (not predictors), decoded here with
        caps on size, LZW input and time; None if any is passed or the data is bad."""
        data = s.read_raw_bytes()
        parms = s.get("/DecodeParms")
        parms = list(parms) if isinstance(parms, pikepdf.Array) else [parms] * len(general)
        try:
            for n, name in enumerate(general):
                if name == "/LZWDecode":
                    if len(data) > MAX_LZW_INPUT:  # what reaches LZW, whatever came first
                        return None
                    p = parms[n] if n < len(parms) else None
                    early = p.get("/EarlyChange", 1) if isinstance(p, pikepdf.Dictionary) \
                        else 1
                    data = lzw(data, MAX_LZW, 0 if early == 0 else 1, self.deadline)
                elif name == "/RunLengthDecode":
                    return None
                else:
                    data = decode_layer(name, data, MAX_LZW + 1, False)
                if data is None or len(data) > MAX_LZW:
                    return None
        except DAMAGED:
            return None
        return data

    def _is_gstate(self, d) -> bool:
        return any(k in d for k in ("/TR", "/TR2", "/HTP", "/BM", "/RI", "/HT")) \
            and "/Type" not in d and "/Subtype" not in d

    def _gstate(self, g) -> None:
        if "/TR" in g:  # 6.2.5-1
            del g["/TR"]
            self.change("removed transfer functions")
        if "/TR2" in g and g.get("/TR2") != pikepdf.Name.Default:  # 6.2.5-2
            g.TR2 = pikepdf.Name.Default
            self.change("removed transfer functions")
        if "/HTP" in g:  # 6.2.5-3
            del g["/HTP"]
        halftone = g.get("/HT")
        if isinstance(halftone, (pikepdf.Dictionary, pikepdf.Stream)):
            if halftone.get("/HalftoneType") not in (1, 5):  # 6.2.5-4
                del g["/HT"]
                self.change("removed printer halftone settings")
            else:
                self._halftone(halftone)
        intent = g.get("/RI")
        if intent is not None and intent not in INTENTS:  # 6.2.6-1
            g.RI = pikepdf.Name.RelativeColorimetric
        mode = g.get("/BM")
        if mode is not None and not _blend_ok(mode):  # 6.2.10-1
            g.BM = pikepdf.Name.Normal
            self.change("replaced blend modes PDF/A doesn't know")

    def _halftone(self, halftone) -> None:
        """6.2.5-5: no halftone names; 6.2.5-6: no transfer function in a halftone for
        all colours (type 1). Type 5 holds one halftone per colorant."""
        parts = [halftone]
        if halftone.get("/HalftoneType") == 5:
            parts += [halftone[k] for k in list(halftone.keys())
                      if isinstance(halftone[k], (pikepdf.Dictionary, pikepdf.Stream))]
        for part in parts[:64]:
            if "/HalftoneName" in part:
                del part["/HalftoneName"]
        if halftone.get("/HalftoneType") == 1 and "/TransferFunction" in halftone:
            del halftone["/TransferFunction"]
            self.change("removed transfer functions")
        if halftone.get("/HalftoneType") == 5:  # allowed only for spot colorants
            for key in list(halftone.keys()):
                part = halftone[key]
                if key in PRIMARIES and isinstance(part, (pikepdf.Dictionary, pikepdf.Stream)) \
                        and "/TransferFunction" in part:
                    del part["/TransferFunction"]
                    self.change("removed transfer functions")


def _predicted(s: pikepdf.Stream) -> bool:
    parms = s.get("/DecodeParms")
    parms = list(parms) if isinstance(parms, pikepdf.Array) else [parms]
    return any(isinstance(p, pikepdf.Dictionary) and p.get("/Predictor", 1) != 1
               for p in parms)


def _blend_ok(mode) -> bool:
    if isinstance(mode, pikepdf.Array):
        return all(m in BLEND_MODES for m in mode)
    return mode in BLEND_MODES


def _is_action(d) -> bool:
    return isinstance(d, pikepdf.Dictionary) and (
        "/S" in d or d.get("/Type") == pikepdf.Name.Action)


def _forbidden(action, sure: bool = False) -> bool:
    """Whether *action* is one PDF/A doesn't allow. A dictionary that doesn't say it is
    an action counts as one only when it can only be one (*sure*)."""
    if not isinstance(action, pikepdf.Dictionary) or not (sure or _is_action(action)):
        return False
    kind = action.get("/S")
    if kind == pikepdf.Name.Named:
        return action.get("/N") not in NAMED_ACTIONS
    return not isinstance(kind, pikepdf.Name) or str(kind) not in ALLOWED_ACTIONS


def _catalog(pdf: pikepdf.Pdf, fixer: _Fixer) -> None:
    root = pdf.Root
    for key in ("/Requirements", "/NeedsRendering"):  # 6.11-1, 6.4.2-2
        if key in root:
            del root[key]
    names = root.get("/Names")
    if isinstance(names, pikepdf.Dictionary):
        if "/JavaScript" in names:
            del names["/JavaScript"]
            fixer.change("removed scripts and other actions PDF/A forbids")
        if "/EmbeddedFiles" in names:
            del names["/EmbeddedFiles"]
            fixer.change("removed attached files")
        if "/AlternatePresentations" in names:  # 6.10-1
            del names["/AlternatePresentations"]
    perms = root.get("/Perms")
    if isinstance(perms, pikepdf.Dictionary):  # 6.1.12-1; usage rights break anyway
        del root["/Perms"]
        fixer.change("removed Reader usage rights")
    version = root.get("/Version")
    if isinstance(version, pikepdf.Name) and str(version) > "/1.7":
        del root["/Version"]


def _forms(pdf: pikepdf.Pdf, fixer: _Fixer) -> None:
    acro = pdf.Root.get("/AcroForm")
    if not isinstance(acro, pikepdf.Dictionary):
        return
    if "/XFA" in acro:  # 6.4.2-1
        del acro["/XFA"]
        fixer.change("removed the XFA form (the standard form fields are kept)")
    if acro.get("/NeedAppearances") is True:  # 6.4.1-3: draw the fields instead
        _draw_fields(pdf, acro, fixer)
    if "/NeedAppearances" in acro:
        del acro["/NeedAppearances"]
    stack = list(acro.get("/Fields") or [])
    seen: set = set()
    while stack:  # 6.4.1-2: fields run no actions
        field = stack.pop()
        fixer.spend()
        if not isinstance(field, pikepdf.Dictionary):
            continue
        if field.is_indirect:
            if field.objgen in seen:
                continue
            seen.add(field.objgen)
        if "/A" in field:
            del field["/A"]
            fixer.change("removed actions from form fields")
        kids = field.get("/Kids")
        if isinstance(kids, pikepdf.Array):
            stack.extend(kids)


def _draw_fields(pdf: pikepdf.Pdf, acro: pikepdf.Dictionary, fixer: _Fixer) -> None:
    """Have qpdf draw the text and choice fields, then give each new appearance the
    resources its content names (qpdf leaves them out; 6.2.2-2)."""
    pdf.generate_appearance_streams()
    shared = acro.get("/DR")
    for page in pdf.pages:
        for annot in page.obj.get("/Annots") or []:
            fixer.spend()
            if not isinstance(annot, pikepdf.Dictionary) or \
                    annot.get("/Subtype") != pikepdf.Name.Widget:
                continue
            ap = annot.get("/AP")
            normal = ap.get("/N") if isinstance(ap, pikepdf.Dictionary) else None
            if not isinstance(normal, pikepdf.Stream) or not within(normal, MAX_APPEARANCE):
                continue
            normal.write(normal.read_bytes())  # qpdf 11 applies it on the first read only
            if "/Resources" not in normal and isinstance(shared, pikepdf.Dictionary):
                normal.Resources = shared
    fixer.change("drew form fields' appearances")


def _field_type(widget: pikepdf.Dictionary):
    node, depth = widget, 0
    while isinstance(node, pikepdf.Dictionary) and depth < 32:
        if "/FT" in node:
            return node.FT
        node, depth = node.get("/Parent"), depth + 1
    return None


def _annotations(pdf: pikepdf.Pdf, fixer: _Fixer) -> None:
    """6.3: annotations of permitted types, printed, visible, each with a normal
    appearance; those that can't be shown are removed (a hidden widget alone, its field
    only once no widget is left)."""
    doomed: set = set()
    widgets = []
    for page in pdf.pages:
        annots = page.obj.get("/Annots")
        if not isinstance(annots, pikepdf.Array):
            continue
        # Direct annotations get an identity of their own, so they can be told apart.
        page.obj.Annots = annots = pikepdf.Array(
            [pdf.make_indirect(a) if isinstance(a, pikepdf.Dictionary) and not a.is_indirect
             else a for a in annots])
        for annot in annots:
            fixer.spend()
            if not isinstance(annot, pikepdf.Dictionary):
                continue
            reason = _annotation_reason(annot)
            if reason:
                doomed.add(annot.objgen)
                fixer.change(f"removed {reason}")
                if annot.get("/Subtype") == pikepdf.Name.Widget:
                    widgets.append(annot)
                continue
            _fix_annotation(annot, fixer)
    _remove(pdf, doomed, widgets)


def _annotation_reason(annot: pikepdf.Dictionary) -> str | None:
    subtype = annot.get("/Subtype")
    if subtype not in ANNOTATIONS:  # one entry, whatever the (file-chosen) kinds
        return "annotations of kinds PDF/A doesn't allow (sound, video, 3D, files)"
    flags = annot.get("/F")
    flags = int(flags) if isinstance(flags, int) else 0
    if flags & (INVISIBLE | HIDDEN | NO_VIEW) and subtype != pikepdf.Name.Popup:
        return "hidden annotations"
    if subtype in (pikepdf.Name.Popup, pikepdf.Name.Link) or _zero_area(annot):
        return None
    ap = annot.get("/AP")
    normal = ap.get("/N") if isinstance(ap, pikepdf.Dictionary) else None
    if not isinstance(normal, (pikepdf.Stream, pikepdf.Dictionary)):
        return "annotations with no appearance to show"
    return None


def _zero_area(annot: pikepdf.Dictionary) -> bool:
    rect = annot.get("/Rect")
    try:
        x0, y0, x1, y1 = (float(v) for v in rect)
    except (TypeError, ValueError):
        return False
    return x0 == x1 and y0 == y1


def _fix_annotation(annot: pikepdf.Dictionary, fixer: _Fixer) -> None:
    subtype = annot.get("/Subtype")
    flags = annot.get("/F")
    flags = int(flags) if isinstance(flags, int) else 0
    # 6.3.2: printed and never out of view (a popup may go without flags, but flags it
    # has must say so too).
    if subtype != pikepdf.Name.Popup or "/F" in annot:
        wanted = (flags | PRINT) & ~(INVISIBLE | HIDDEN | NO_VIEW | TOGGLE_NO_VIEW)
        if wanted != flags or "/F" not in annot:
            annot.F = wanted
            fixer.change("set annotations to print")
    if subtype == pikepdf.Name.Widget:
        for key in ("/A", "/AA"):  # 6.4.1-1
            if key in annot:
                del annot[key]
                fixer.change("removed actions from form fields")
    ap = annot.get("/AP")
    if not isinstance(ap, pikepdf.Dictionary):
        return
    for key in ("/R", "/D"):  # 6.3.3-2: the normal appearance only
        if key in ap:
            del ap[key]
            fixer.change("removed rollover and pressed appearances")
    normal = ap.get("/N")
    button = subtype == pikepdf.Name.Widget and _field_type(annot) == pikepdf.Name.Btn
    if button and isinstance(normal, pikepdf.Stream):  # 6.3.3-3: states, even for one
        ap.N = pikepdf.Dictionary(On=normal)
        annot.AS = pikepdf.Name.On
    elif not button and isinstance(normal, pikepdf.Dictionary):  # 6.3.3-4: one stream
        state = annot.get("/AS")
        chosen = normal.get(state) if isinstance(state, pikepdf.Name) else None
        if not isinstance(chosen, pikepdf.Stream):
            chosen = next((normal[k] for k in normal.keys()
                           if isinstance(normal[k], pikepdf.Stream)), None)
        if chosen is not None:
            ap.N = chosen


def _without(holder, owner, key, gone: set) -> int:
    """Drop the members in *gone* from owner[key]; returns how many are left."""
    if not isinstance(holder, pikepdf.Array):
        return 0
    kept = [k for k in holder if not (isinstance(k, pikepdf.Dictionary) and k.is_indirect
                                      and k.objgen in gone)]
    if len(kept) != len(holder):
        owner[key] = pikepdf.Array(kept)
    return len(kept)


def _remove(pdf: pikepdf.Pdf, doomed: set, widgets: list) -> None:
    """Remove the doomed annotations from their pages; a removed widget leaves its
    field, and a field left with no widget leaves the form."""
    if not doomed:
        return
    acro = pdf.Root.get("/AcroForm")
    for widget in widgets:
        field = _field_of(widget)
        if not field.is_indirect:
            continue
        if field.objgen != widget.objgen:  # a field with widgets of its own
            if _without(field.get("/Kids"), field, "/Kids", {widget.objgen}):
                continue  # other widgets still show it
        parent = field.get("/Parent")
        owner, key = (parent, "/Kids") if isinstance(parent, pikepdf.Dictionary) \
            else (acro, "/Fields")
        if isinstance(owner, pikepdf.Dictionary):
            _without(owner.get(key), owner, key, {field.objgen})
    for page in pdf.pages:
        annots = page.obj.get("/Annots")
        if isinstance(annots, pikepdf.Array):
            _without(annots, page.obj, "/Annots", doomed)


def _pages(pdf: pikepdf.Pdf, fixer: _Fixer) -> None:
    low, high = PAGE_SIZE
    for n, page in enumerate(pdf.pages, 1):
        fixer.spend()
        if "/PresSteps" in page.obj:  # 6.10-2
            del page.obj["/PresSteps"]
        for key in BOXES:  # 6.1.13-11: every page boundary
            box = page.obj.get(key) if key != "/MediaBox" else page.mediabox
            if box is None:
                continue
            try:
                x0, y0, x1, y1 = (float(v) for v in box)
            except (TypeError, ValueError):
                fixer.problem("pages of a size PDF/A doesn't allow", str(n))
                break
            if not (low <= abs(x1 - x0) <= high and low <= abs(y1 - y0) <= high):
                fixer.problem("pages of a size PDF/A doesn't allow", str(n))
                break


def _optional_content(pdf: pikepdf.Pdf, fixer: _Fixer) -> None:
    """6.9: every configuration named (uniquely), none with AS, Order listing all."""
    props = pdf.Root.get("/OCProperties")
    if not isinstance(props, pikepdf.Dictionary):
        return
    groups = [g for g in props.get("/OCGs") or [] if isinstance(g, pikepdf.Dictionary)]
    configs = [props.get("/D")] + list(props.get("/Configs") or [])
    names: set[str] = set()
    for n, config in enumerate(configs):
        fixer.spend()
        if not isinstance(config, pikepdf.Dictionary):
            continue
        name = str(config.get("/Name", "")) if isinstance(config.get("/Name"), pikepdf.String) \
            else ""
        if not name or name in names:
            base = name or ("Default" if n == 0 else f"Configuration {n}")
            name, k = base, 2
            while name in names:
                name, k = f"{base} ({k})", k + 1
            config.Name = pikepdf.String(name)
            fixer.change("named layer configurations")
        names.add(name)
        if "/AS" in config:
            del config["/AS"]
        order = config.get("/Order")
        if isinstance(order, pikepdf.Array):
            listed, seen = set(), set()
            stack = list(order)
            while stack:
                item = stack.pop()
                fixer.spend()
                if isinstance(item, (pikepdf.Array, pikepdf.Dictionary)) and item.is_indirect:
                    if item.objgen in seen:  # an array can contain itself
                        continue
                    seen.add(item.objgen)
                if isinstance(item, pikepdf.Array):
                    stack.extend(item)
                elif isinstance(item, pikepdf.Dictionary) and item.is_indirect:
                    listed.add(item.objgen)
            missing = [g for g in groups if g.is_indirect and g.objgen not in listed]
            if missing:
                config.Order = pikepdf.Array(list(order) + missing)
                fixer.change("listed every layer in the layer order")


def _profile_space(profile) -> str | None:
    """The colour space of a profile PDF/A accepts as output intent (6.2.3-1: monitor or
    printer, RGB, CMYK or grey, version 4 at most, and small enough to read), or None."""
    if not isinstance(profile, pikepdf.Stream) or not within(profile, MAX_PROFILE):
        return None
    head = decode(profile, 20)
    if head is None or len(head) < 20 or head[8] >= 5 or head[12:16] not in (b"mntr",
                                                                            b"prtr"):
        return None
    return {b"RGB ": "RGB", b"CMYK": "CMYK", b"GRAY": "GRAY"}.get(head[16:20])


@functools.cache
def _srgb() -> bytes:
    try:
        from PIL import ImageCms
    except ImportError as exc:  # pragma: no cover - Pillow built without LittleCMS
        raise DependencyError("PDF/A needs Pillow with LittleCMS (ImageCms)") from exc
    return ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()


def _output_intent(pdf: pikepdf.Pdf, fixer: _Fixer) -> str:
    """Keep a valid RGB or CMYK PDF/A output intent, or add sRGB (a grey one gives way
    to sRGB, which serves grey too). Returns the intent's colour space."""
    intents = pdf.Root.get("/OutputIntents")
    kept, space, own = None, None, True
    looked: set = set()
    for intent in list(intents)[:MAX_INTENTS] if isinstance(intents, pikepdf.Array) else []:
        fixer.spend()
        if isinstance(intent, pikepdf.Dictionary) and \
                intent.get("/S") == pikepdf.Name("/GTS_PDFA1"):
            profile = intent.get("/DestOutputProfile")
            key = profile.objgen if isinstance(profile, pikepdf.Stream) and \
                profile.is_indirect else None
            if key is not None and key in looked:
                continue  # each profile is measured once
            looked.add(key)
            space = _profile_space(profile)
            if space in ("RGB", "CMYK"):
                kept = intent
                break
    if kept is None:
        profile = pdf.make_stream(zlib.compress(_srgb()))
        profile.Filter, profile.N = pikepdf.Name.FlateDecode, 3
        kept = pdf.make_indirect(pikepdf.Dictionary(
            Type=pikepdf.Name.OutputIntent, S=pikepdf.Name("/GTS_PDFA1"),
            OutputConditionIdentifier=pikepdf.String(SRGB), Info=pikepdf.String(SRGB),
            RegistryName=pikepdf.String("http://www.color.org"), DestOutputProfile=profile))
        space, own = "RGB", False
        fixer.change("added an sRGB colour profile (output intent)")
    alone = isinstance(intents, pikepdf.Array) and len(intents) == 1 and own
    if not alone:  # 6.2.3-2: all intents share one profile, so keep just the one
        if own:
            fixer.change("kept one colour profile (output intent)")
        elif isinstance(intents, pikepdf.Array) and len(intents):
            fixer.change("replaced colour profiles PDF/A can't use")
        pdf.Root.OutputIntents = pikepdf.Array([kept])
    return space


def _old_xmp(pdf: pikepdf.Pdf) -> dict:
    """The standard properties of the document's XMP that are worth keeping and valid."""
    stream = pdf.Root.get("/Metadata")
    if not isinstance(stream, pikepdf.Stream) or not within(stream, MAX_XMP):
        return {}
    kept: dict = {}
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            with pdf.open_metadata(set_pikepdf_as_editor=False, update_docinfo=False) as old:
                for key in KEPT_XMP:
                    value = old.get(key)
                    if not value:
                        continue
                    if key in XMP_DATES and not (isinstance(value, str)
                                                 and _ISO_DATE.fullmatch(value)):
                        continue  # 6.6.2.3.1-2: a value of the property's type
                    kept[key] = value
    except Exception:  # noqa: BLE001 - damaged XMP: start from the document info
        return {}
    return kept


def _cut(value):
    """Text values (alone or in a list) short enough for the document info."""
    if isinstance(value, (list, set)):
        return type(value)(_cut(v) for v in value)
    return value[:MAX_STRING // 4] if isinstance(value, str) else value


def _metadata(pdf: pikepdf.Pdf, fixer: _Fixer) -> None:
    """6.6: fresh XMP with the PDF/A identification, the document info and the
    standard properties of the old XMP."""
    if "/Metadata" in pdf.Root:
        kept = _old_xmp(pdf)
        del pdf.Root["/Metadata"]
        fixer.change("rewrote the XMP metadata for PDF/A")
    else:
        kept = {}
        fixer.change("added PDF/A metadata (XMP)")
    with pdf.open_metadata(set_pikepdf_as_editor=False, update_docinfo=True) as meta:
        with warnings.catch_warnings():  # entries with no XMP equivalent stay in the info
            warnings.simplefilter("ignore")
            meta.load_from_docinfo(pdf.docinfo, raise_failure=False)
        for key, value in kept.items():
            value = _cut(value)  # it goes back to the info too
            try:
                meta[key] = value
            except (ValueError, TypeError):  # a value of the wrong shape: leave it out
                pass
        meta["pdfaid:part"] = "2"
        meta["pdfaid:conformance"] = "B"


def _content(pdf: pikepdf.Pdf, fixer: _Fixer, intent: str, deadline: float) -> None:
    """What the pages draw: fonts used visibly without their program, and device
    colours the output intent doesn't cover."""
    usage = Usage(deadline)
    try:
        for page in pdf.pages:
            usage.page(page)
    except TooComplex:
        fixer.problem("content too large or complex to check here")
        return
    for name in usage.unembedded:
        fixer.problem("fonts that aren't embedded", name)
    for what in usage.problems:
        fixer.problem(what)
    if intent == "RGB" and "CMYK" in usage.colours:  # 6.2.4.3-3
        fixer.problem("CMYK colour (with an RGB colour profile)")
    if intent == "CMYK" and "RGB" in usage.colours:  # 6.2.4.3-2
        fixer.problem("RGB colour (with a CMYK colour profile)")


def _read_capped(stream, cap: int) -> bytes | None:
    """Everything *stream* gives, or None as soon as it is more than *cap* bytes."""
    chunks, total = [], 0
    while True:
        chunk = stream.read(1 << 16)
        if not chunk:
            return b"".join(chunks)
        total += len(chunk)
        if total > cap:
            return None
        chunks.append(chunk)


def _verapdf_report(exe: str, path: str) -> tuple[bool, list[dict], str]:
    """(compliant, failed rules, veraPDF's version) for the file at *path*."""
    env = {k: os.environ[k] for k in VERAPDF_ENV if k in os.environ}
    env["JAVA_OPTS"] = VERAPDF_MEMORY
    cmd = [exe, "--flavour", "2b", "--format", "json", "--maxfailuresdisplayed", "1",
           "--", path]
    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, env=env, start_new_session=True,
                                cwd=os.path.dirname(path))
    except OSError as exc:
        raise DependencyError(f"Could not run veraPDF: {exc}") from None
    timed_out = threading.Event()

    timer = threading.Timer(VALIDATE_TIME_LIMIT_S, _stop, [proc, timed_out])
    timer.start()
    try:
        out = _read_capped(proc.stdout, MAX_VERAPDF_OUTPUT)
        if out is None:
            _kill(proc)
        proc.wait()
    finally:
        timer.cancel()
        if proc.returncode is None:  # stopped by an exception: don't leave the JVM
            _kill(proc)
            proc.wait()
        proc.stdout.close()
    if timed_out.is_set():
        raise PdfUserError(f"veraPDF took over {VALIDATE_TIME_LIMIT_S} s to check the "
                           "result; nothing was written")
    if out is None:
        raise PdfUserError("veraPDF's report is too large; nothing was written")
    try:
        report = json.loads(out)["report"]
        job = report["jobs"][0]
        version = next((d["version"] for d in report["buildInformation"]["releaseDetails"]
                        if d.get("id") == "core"), "")
        if "validationResult" not in job:
            reason = _clean(str(job.get("taskException", {}).get("exceptionMessage") or
                                "no result"))
            raise PdfUserError(f"veraPDF could not check the result ({reason}); nothing "
                               "was written")
        result = job["validationResult"][0]
        rules = [{"rule": f"{r['clause']}-{r['testNumber']}",
                  "description": _clean(str(r.get("description", "")))[:300],
                  "failures": int(r.get("failedChecks", 0))}
                 for r in result["details"]["ruleSummaries"] if r.get("status") == "failed"]
        return bool(result["compliant"]), rules, f"veraPDF {_clean(str(version))}".strip()
    except (ValueError, KeyError, IndexError, TypeError, AttributeError):
        raise PdfUserError("veraPDF could not check the result; nothing was written") from None


def _stop(proc: subprocess.Popen, timed_out: threading.Event) -> None:
    """At the deadline: kill veraPDF if it is still running (once reaped, its process
    group number may belong to someone else)."""
    if proc.poll() is None:
        timed_out.set()
        _kill(proc)


def _kill(proc: subprocess.Popen) -> None:
    try:
        if hasattr(os, "killpg"):
            os.killpg(proc.pid, signal.SIGKILL)  # the JVM too, not just its launcher
        else:  # pragma: no cover - Windows
            proc.kill()
    except (ProcessLookupError, PermissionError):
        pass


def _summary(problems: dict[str, set[str]]) -> str:
    parts = []
    for what, details in sorted(problems.items()):
        named = sorted(d for d in details if d)
        if named:
            pages = what.startswith("pages")
            shown = named[:MAX_REPORT] if pages else [f'"{n}"' for n in named[:MAX_REPORT]]
            listed = ", ".join(shown) + (" …" if len(named) > MAX_REPORT else "")
            label = "pages " if pages else "named in the file: "
            parts.append(f"{what} ({label}{listed})")
        else:
            parts.append(what)
    return "; ".join(parts)


def _refuse(message: str) -> PdfUserError:
    if len(message) > MAX_MESSAGE:
        message = message[:MAX_MESSAGE - 1] + "…"
    return PdfUserError(message + " Nothing was written")


def _check_time(deadline: float) -> None:
    if time.monotonic() > deadline:
        raise PdfUserError(f"Converting to PDF/A took over {CONVERT_TIME_LIMIT_S} s; "
                           "nothing was written")


def convert_to_pdfa(path: Path, out: Path, settings: Settings,
                    password: str | None = None) -> PdfaResult:
    """Write a PDF/A-2b copy, checked by veraPDF when it is installed."""
    deadline = time.monotonic() + CONVERT_TIME_LIMIT_S
    with open_pdf(path.read_bytes(), settings, password) as pdf:
        protected = pdf.is_encrypted
        if protected:  # PDF/A can't be encrypted: the copy opens and copies freely
            require_copy_permission(pdf, "making a PDF/A copy (which can't be encrypted)")
        signed, _ = signature_state(pdf)
        if signed:
            raise PdfUserError("This PDF is digitally signed; converting it would invalidate "
                               "the signature")
        fixer = _Fixer(pdf, deadline)
        if protected:
            fixer.change("removed the password and restrictions (PDF/A can't be encrypted)")
        for step in (_catalog, _forms, _annotations, _pages, _optional_content):
            step(pdf, fixer)
            _check_time(deadline)
        fixer.walk(pdf.trailer)
        intent = _output_intent(pdf, fixer)
        _content(pdf, fixer, intent, deadline)
        _check_time(deadline)
        _metadata(pdf, fixer)
        buf = io.BytesIO()
        # PDF/A-2 is PDF 1.7 at most (6.1.2-1); qpdf writes the binary comment and the
        # file ID it requires, and decrypts (no encryption=). Streams are copied as they
        # are, never decoded (a few KB can inflate to gigabytes): LZW, which PDF/A
        # forbids, was converted above.
        pdf.save(buf, force_version="1.7" if pdf.pdf_version > "1.7" else None,
                 fix_metadata_version=False, compress_streams=False,
                 stream_decode_level=pikepdf.StreamDecodeLevel.none)
        data = buf.getvalue()
    validator = None
    found = _summary(fixer.problems)
    if settings.verapdf_path:  # it decides: it reads content more fully than this module
        compliant, rules, validator = _validate(settings.verapdf_path, data)
        if not compliant:
            failed = "; ".join(f"{r['rule']} {r['description']} ({r['failures']}x)"
                               for r in rules[:MAX_REPORT])
            raise _refuse("This PDF can't be made PDF/A-2b here: "
                          f"{found + '. ' if found else ''}veraPDF's report: {failed}.")
    elif fixer.problems:
        raise _refuse(f"This PDF can't be made PDF/A-2b here: {found}.")
    with open(out, "wb") as f:
        f.write(data)
    return {"output": str(out), "standard": STANDARD, "validated": validator is not None,
            "validator": validator,
            "changes": [f"{what} ({n})" if n > 1 else what
                        for what, n in sorted(fixer.changes.items())],
            "protection_removed": protected}


def _validate(exe: str, data: bytes) -> tuple[bool, list[dict], str]:
    work = tempfile.mkdtemp(prefix="paperboy-pdfa-")  # private (0700), removed below
    try:
        target = os.path.join(work, "candidate.pdf")
        with open(target, "wb") as f:
            f.write(data)
        return _verapdf_report(exe, target)
    finally:
        shutil.rmtree(work, ignore_errors=True)
