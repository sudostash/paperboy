"""Hidden data: find it (inspect_hidden_data) and remove it (sanitize_pdf).

"Hidden" means what travels with a PDF but isn't on its pages: metadata, scripts
and risky actions, attachments, comments and their authors, application data,
thumbnails, camera data inside images, and earlier saved versions. Links,
destinations, the outline, form fields and the accessibility structure are
content, and are kept.

What sanitize_pdf reports as removed is measured: the edited document is scanned
again, and if anything in a category it was asked to remove is still reachable,
it refuses instead of writing a copy that only looks clean.
"""
from __future__ import annotations

import base64
import binascii
import re
import secrets
import zlib
from pathlib import Path

import pikepdf

from ..config import Settings
from ..errors import PdfUserError
from ..results import HiddenDataResult, SanitizeResult
from .base import inherit_protection, open_pdf, require_modify_permission, signature_state
from .metadata import read_metadata

SCRIPTS = {"/JavaScript"}
RISKY = {"/Launch", "/SubmitForm", "/ImportData", "/GoToR", "/GoToE", "/Rendition",
         "/RichMediaExecute"}
# Popups aren't counted: they're the window a comment opens in, and go with it.
COMMENTS = {"/Text", "/FreeText", "/Highlight", "/Underline", "/Squiggly", "/StrikeOut",
            "/Caret", "/Ink", "/Square", "/Circle", "/Line", "/Polygon", "/PolyLine",
            "/Stamp", "/Sound"}
AUTHORED = COMMENTS | {"/FileAttachment"}  # carry /T (author) and dates
BLOCKED = SCRIPTS | RISKY
# Keys of the catalog, page tree and structure elements. A dictionary with one isn't
# emptied by the sweep (that would break the document); only its payload is removed.
_NOT_ACTION = ("/Pages", "/Kids", "/Parent", "/Contents", "/MediaBox", "/K", "/P", "/Pg")
# What makes an action do something: the script, the program or file, the form fields.
_PAYLOAD = ("/JS", "/F", "/Win", "/Mac", "/Unix", "/Fields", "/R", "/TA", "/TI", "/CMD",
            "/AN")
_GENERAL_FILTERS = {"/FlateDecode", "/LZWDecode", "/ASCIIHexDecode", "/ASCII85Decode",
                    "/RunLengthDecode"}
MAX_JPEG_BYTES = 200 * 1024 * 1024  # a wrapped JPEG may not inflate beyond this
MAX_NODES = 1_000_000   # dictionaries, streams and arrays visited per file
_JPEG_STRIP = {0xE1, 0xED, 0xFE}  # APP1 (EXIF, XMP), APP13 (IPTC), COM (comments)


def _is_dict(obj) -> bool:
    return isinstance(obj, (pikepdf.Dictionary, pikepdf.Stream))


def _dictionaries(pdf: pikepdf.Pdf) -> list:
    """Every dictionary (and stream dictionary) reachable from the trailer, once.

    Only containers are walked and counted, so long arrays of numbers (font widths,
    coordinates) don't use up the budget.
    """
    found, seen, stack, visited = [], set(), [pdf.trailer], 0
    while stack:
        obj = stack.pop()
        if obj.is_indirect:
            if obj.objgen in seen:
                continue
            seen.add(obj.objgen)
        visited += 1
        if visited > MAX_NODES:
            raise PdfUserError("This PDF has too many objects to inspect")
        if isinstance(obj, pikepdf.Array):
            children = obj
        else:
            found.append(obj)
            children = (v for _, v in (obj.stream_dict.items()
                                       if isinstance(obj, pikepdf.Stream) else obj.items()))
        stack.extend(c for c in children
                     if isinstance(c, (pikepdf.Dictionary, pikepdf.Stream, pikepdf.Array)))
    return found


def _action_kind(obj) -> str | None:
    """The /S of a dictionary. Where an action is expected, this alone decides."""
    kind = obj.get("/S") if isinstance(obj, pikepdf.Dictionary) else None
    return str(kind) if isinstance(kind, pikepdf.Name) else None


def _action_shaped(d) -> bool:
    return (not any(key in d for key in _NOT_ACTION)
            and d.get("/Type") in (None, pikepdf.Name.Action))


def _threat(d) -> str | None:
    """What a dictionary found anywhere could run: a blocked action type, or a script.

    A blocked /S counts when the dictionary looks like an action or carries a payload,
    whatever other keys it has (extra keys must not hide an action); any /JS counts.
    """
    if not isinstance(d, pikepdf.Dictionary):
        return None
    kind = _action_kind(d)
    if kind in BLOCKED and (_action_shaped(d) or any(key in d for key in _PAYLOAD)):
        return kind
    return "/JavaScript" if "/JS" in d else None


def _subtype(obj) -> str:
    return str(obj.get("/Subtype")) if _is_dict(obj) else ""


def _is_annotation(d) -> bool:
    return isinstance(d, pikepdf.Dictionary) and ("/Rect" in d or d.get("/Type") == "/Annot")


# --- JPEG camera metadata ----------------------------------------------------------------


def _jpeg_filters(stream) -> list[str] | None:
    """The stream's filters if it holds a JPEG (DCTDecode last), else None."""
    filters = stream.get("/Filter")
    filters = list(filters) if isinstance(filters, pikepdf.Array) else [filters]
    if not filters or filters[-1] != pikepdf.Name.DCTDecode:
        return None
    return [str(f) for f in filters]


def _jpeg_bytes(stream) -> bytes | None:
    """The JPEG inside a DCTDecode stream, unwrapping any general filters; None if the
    wrapping can't be undone here."""
    filters = _jpeg_filters(stream)
    if filters is None:
        return None
    data = stream.read_raw_bytes()
    for name in filters[:-1]:  # decoded in the order listed
        data = _undo_filter(name, data)
        if data is None:
            return None
    return data


def _undo_filter(name: str, data: bytes) -> bytes | None:
    """Decode one general-purpose filter (no predictors), capped against bombs."""
    try:
        if name == "/FlateDecode":
            inflater = zlib.decompressobj()
            out = inflater.decompress(data, MAX_JPEG_BYTES)
            return None if inflater.unconsumed_tail else out
        if name == "/ASCIIHexDecode":
            return binascii.unhexlify(re.sub(rb"\s", b"", data).split(b">")[0])
        if name == "/ASCII85Decode":
            return base64.a85decode(data.strip().removesuffix(b"~>"), adobe=False)
    except (zlib.error, binascii.Error, ValueError):
        return None
    return None  # LZW, run-length: rare around JPEGs; not inspected


def _jpeg_metadata(data: bytes) -> list[tuple[int, int]]:
    """Byte ranges of metadata segments before the image data; [] if none or unparseable."""
    if data[:2] != b"\xff\xd8":
        return []
    spans, i = [], 2
    while i + 4 <= len(data):
        if data[i] != 0xFF:
            return []
        marker = data[i + 1]
        if marker == 0xFF:  # fill byte
            i += 1
            continue
        if marker in (0xDA, 0xD9):  # start of scan / end: no more headers
            break
        if marker == 0x01 or 0xD0 <= marker <= 0xD7:  # markers without a length
            i += 2
            continue
        length = int.from_bytes(data[i + 2:i + 4], "big")
        if length < 2 or i + 2 + length > len(data):
            return []
        if marker in _JPEG_STRIP:
            spans.append((i, i + 2 + length))
        i += 2 + length
    return spans


def _jpeg_streams(dicts: list) -> tuple[list, int]:
    """(JPEG streams with metadata segments, JPEGs whose wrapping can't be undone).

    The second may hold metadata too; counting them keeps sanitizing from calling a
    file clean that it couldn't actually check.
    """
    strippable, unknown = [], 0
    for d in dicts:
        if not isinstance(d, pikepdf.Stream) or _jpeg_filters(d) is None:
            continue
        data = _jpeg_bytes(d)
        if data is None:
            unknown += 1
        elif _jpeg_metadata(data):
            strippable.append(d)
    return strippable, unknown


def _strip_jpeg(stream: pikepdf.Stream) -> None:
    """Drop metadata segments, keeping the compressed image data as it is (lossless)."""
    data = _jpeg_bytes(stream)
    kept, last = [], 0
    for start, end in _jpeg_metadata(data):
        kept.append(data[last:start])
        last = end
    kept.append(data[last:])
    parms = stream.get("/DecodeParms")
    if isinstance(parms, pikepdf.Array):  # one entry per filter; keep the DCTDecode one
        parms = parms[-1] if len(parms) else None
    stream.write(b"".join(kept), filter=pikepdf.Name.DCTDecode,
                 decode_parms=parms if isinstance(parms, pikepdf.Dictionary) else None)


# --- earlier saved versions -----------------------------------------------------------------


def _revisions(data: bytes) -> int:
    """Earlier saved versions: the cross-reference sections chained by /Prev, minus one
    (and minus a linearized file's first-page section, which isn't a version)."""
    pointer = re.match(rb"startxref\s+(\d+)", data[data.rfind(b"startxref"):][:64])
    if not pointer:
        return 0
    offset, seen, links = int(pointer.group(1)), set(), 0
    while 0 <= offset < len(data) and offset not in seen and len(seen) < 10_000:
        seen.add(offset)
        if data[offset:offset + 4] == b"xref":
            start = data.find(b"trailer", offset)
            end = data.find(b"startxref", start)
            window = data[start:end] if start >= 0 and end >= 0 else b""
        else:  # a cross-reference stream: /Prev is in its dictionary
            end = data.find(b"stream", offset)
            window = data[offset:end] if end >= 0 else b""
        prev = re.search(rb"/Prev\s+(\d+)", window)
        if not prev:
            break
        links += 1  # counted even if it points nowhere: the file claims a version
        offset = int(prev.group(1))
    if b"/Linearized" in data[:1024]:
        links -= 1
    return max(0, links)


# --- scanning -----------------------------------------------------------------------------------


def _scan(pdf: pikepdf.Pdf, dicts: list, revisions: int) -> dict:
    kinds = [_threat(d) for d in dicts]
    strippable, unknown = _jpeg_streams(dicts)
    acro = pdf.Root.get("/AcroForm")
    xfa = isinstance(acro, pikepdf.Dictionary) and "/XFA" in acro
    annotations = [d for d in dicts if _is_annotation(d)]
    return {
        "metadata_fields": sorted(read_metadata(pdf)),
        "xmp_metadata": any("/Metadata" in d for d in dicts),
        "comment_authors": sum(_subtype(a) in AUTHORED and "/T" in a for a in annotations),
        "image_metadata": len(strippable) + unknown,
        "javascript": kinds.count("/JavaScript") + int(xfa),
        "risky_actions": sum(k in RISKY for k in kinds),
        "embedded_files": sum("/EF" in d or "/RF" in d for d in dicts),
        "comments": sum(_subtype(a) in COMMENTS for a in annotations),
        "private_app_data": sum("/PieceInfo" in d for d in dicts),
        "thumbnails": sum("/Thumb" in d for d in dicts),
        "previous_revisions": revisions,
        "reader_usage_rights": signature_state(pdf)[1],
    }


def inspect_hidden_data(path: Path, settings: Settings,
                        password: str | None = None) -> HiddenDataResult:
    """Report the hidden data in a PDF without changing it."""
    data = path.read_bytes()
    with open_pdf(data, settings, password) as pdf:
        found = _scan(pdf, _dictionaries(pdf), _revisions(data))
    return {"path": str(path), "found": found, "clean": not any(found.values())}


# --- removing --------------------------------------------------------------------------------


def _blank(d) -> None:
    """Empty a dictionary in place, so anything still pointing at it reaches nothing."""
    for key in list(d.keys()):
        del d[key]


def _prune_chain(action, blocked: set[str], seen: set) -> None:
    """Cut every blocked action out of an action's /Next chain (which may loop)."""
    while isinstance(action, pikepdf.Dictionary):
        if action.is_indirect:
            if action.objgen in seen:
                return
            seen.add(action.objgen)
        nxt = action.get("/Next")
        if isinstance(nxt, pikepdf.Array):
            kept = [a for a in nxt if _action_kind(a) not in blocked]
            for a in kept:
                _prune_chain(a, blocked, seen)
            if kept:
                action.Next = pikepdf.Array(kept)
            else:
                del action.Next
            return
        if nxt is None:
            return
        if _action_kind(nxt) in blocked:
            del action.Next
            return
        action = nxt


def _remove_action_keys(dicts: list, blocked: set[str]) -> None:
    """Detach blocked actions where documents normally hold them (keeping safe links)."""
    for d in dicts:
        if not isinstance(d, pikepdf.Dictionary):
            continue
        if "/AA" in d:  # automatic triggers (open, close, mouse over, ...)
            del d.AA
        for key in ("/A", "/OpenAction", "/PA"):
            action = d.get(key)
            if _action_kind(action) in blocked:
                del d[key]
            elif isinstance(action, pikepdf.Dictionary):
                _prune_chain(action, blocked, set())


def _neutralise_actions(dicts: list, blocked: set[str]) -> None:
    """Disarm anything left anywhere else that could run (custom keys, arrays, media):
    empty it if it is an action, else remove just its payload and keep the structure."""
    for d in dicts:
        if not _threat(d):
            continue
        if _action_shaped(d):
            _blank(d)
        else:
            for key in _PAYLOAD:
                if key in d:
                    del d[key]


def _drop_annotations(pdf: pikepdf.Pdf, dicts: list, subtypes: set[str]) -> None:
    """Remove annotations of *subtypes* (and popups/replies of removed ones) from pages,
    and empty them, since structure trees and popups can still point at them."""
    annotations = [d for d in dicts if _is_annotation(d)]
    targets = {a.objgen for a in annotations if _subtype(a) in subtypes and a.is_indirect}

    def drop(a) -> bool:  # pikepdf wrappers aren't stable: decide from the object itself
        if not _is_dict(a):
            return False
        if _subtype(a) in subtypes:
            return True
        return any(_is_dict(ref) and ref.is_indirect and ref.objgen in targets
                   for ref in (a.get("/Parent"), a.get("/IRT")))  # its popup or a reply

    doomed = [a for a in annotations if drop(a)]  # decided before anything is emptied
    for page in pdf.pages:
        annots = page.obj.get("/Annots")
        if isinstance(annots, pikepdf.Array):
            kept = [a for a in annots if not drop(a)]
            if len(kept) != len(annots):
                page.obj.Annots = pdf.make_indirect(pikepdf.Array(kept))
    for a in doomed:
        _blank(a)


_CATEGORIES = {
    "metadata": ("metadata_fields", "xmp_metadata", "comment_authors", "image_metadata"),
    "scripts": ("javascript", "risky_actions"),
    "attachments": ("embedded_files",),
    "comments": ("comments",),
    "always": ("private_app_data", "thumbnails", "reader_usage_rights"),
}


def sanitize_pdf(
    path: Path,
    out: Path,
    settings: Settings,
    remove_metadata: bool = True,
    remove_scripts: bool = True,
    remove_attachments: bool = True,
    remove_comments: bool = False,
    password: str | None = None,
) -> SanitizeResult:
    """Write a copy without hidden data.

    Always removes application data, thumbnails, earlier saved versions (the file is
    rewritten), unused resources and Reader usage rights, and gives the file a new
    random ID; the options choose metadata (including comment authors and camera
    data in images), scripts and risky actions, attachments, and comments.
    """
    data = path.read_bytes()
    with open_pdf(data, settings, password) as pdf:
        require_modify_permission(pdf)
        page_count = len(pdf.pages)
        signed, usage_rights = signature_state(pdf)
        if signed:
            raise PdfUserError(
                "This PDF is digitally signed; sanitizing it would invalidate the signature"
            )
        dicts = _dictionaries(pdf)
        found = _scan(pdf, dicts, _revisions(data))
        blocked = BLOCKED
        names = pdf.Root.get("/Names")
        names = names if isinstance(names, pikepdf.Dictionary) else pikepdf.Dictionary()

        for d in dicts:
            for key in ("/PieceInfo", "/Thumb"):
                if key in d:
                    del d[key]
            if remove_metadata:
                for key in ("/Metadata", "/LastModified"):
                    if key in d:
                        del d[key]
                if _is_annotation(d) and _subtype(d) in AUTHORED:
                    for key in ("/T", "/M", "/CreationDate"):  # who wrote it, and when
                        if key in d:
                            del d[key]
            if remove_attachments:
                for key in ("/AF", "/EF", "/RF"):
                    if key in d:
                        del d[key]
        if remove_metadata:
            if "/Info" in pdf.trailer:
                del pdf.trailer.Info
            for stream in _jpeg_streams(dicts)[0]:
                _strip_jpeg(stream)
        if remove_scripts:
            _remove_action_keys(dicts, blocked)
            if "/JavaScript" in names:
                del names.JavaScript
            acro = pdf.Root.get("/AcroForm")
            if isinstance(acro, pikepdf.Dictionary) and "/XFA" in acro:
                del acro.XFA  # XFA forms carry their own scripts
            _neutralise_actions(dicts, blocked)
        if remove_attachments and "/EmbeddedFiles" in names:
            del names.EmbeddedFiles
        drop = ({"/FileAttachment"} if remove_attachments else set()) | \
            (COMMENTS if remove_comments else set())
        if drop:
            _drop_annotations(pdf, dicts, drop)
        if usage_rights:
            del pdf.Root.Perms
        pdf.remove_unreferenced_resources()

        # Measure what's left rather than assume the edits worked.
        remaining = _scan(pdf, _dictionaries(pdf), 0)
        enabled = [*_CATEGORIES["always"],
                   *(_CATEGORIES["metadata"] if remove_metadata else ()),
                   *(_CATEGORIES["scripts"] if remove_scripts else ()),
                   *(_CATEGORIES["attachments"] if remove_attachments else ()),
                   *(_CATEGORIES["comments"] if remove_comments else ())]
        if len(pdf.pages) != page_count or "/Pages" not in pdf.Root:
            raise PdfUserError("Sanitizing would have damaged this PDF; nothing was written")
        leftover = [name for name in enabled if remaining[name]]
        if leftover:
            hint = ""
            if "image_metadata" in leftover:
                hint = (" Some images use compression this tool can't unwrap, so their "
                        "metadata can't be checked; pass remove_metadata=false to keep "
                        "image metadata, or re-encode the images first.")
            raise PdfUserError(
                f"Sanitizing could not remove all hidden data ({', '.join(leftover)}); "
                f"nothing was written.{hint}"
            )
        # A random ID can't link this copy to the original or to when it was made.
        pdf.trailer.ID = pikepdf.Array([pikepdf.String(secrets.token_bytes(16))] * 2)
        protection = inherit_protection(pdf, password)
        pdf.save(str(out), encryption=protection.encryption,
                 deterministic_id=not protection.encryption,
                 fix_metadata_version=False)  # leave XMP alone (it may be malformed)

    def gone(name):
        before, after = found[name], remaining[name]
        if isinstance(before, list):
            return [x for x in before if x not in after]
        if isinstance(before, bool):
            return before and not after
        return before - after

    removed = {name: gone(name) for name in found}
    removed["previous_revisions"] = found["previous_revisions"]  # the rewrite drops them
    return protection.report({"output": str(out), "removed": removed,
                              "document_id_reset": True})
