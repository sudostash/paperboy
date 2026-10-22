"""Getting files and pictures out of a PDF: its attachments and its images.

Everything read from the PDF is untrusted: attachment names become safe file names,
files never replace links, folders or the input and are written whole (and removed
again if the run fails), decoding and the work of one call are capped against bombs,
and attachments that can run on a computer are left in the PDF unless asked for.

Images are those drawn by pages and the forms inside them; inline images, and
images inside annotations, patterns and Type 3 fonts, are not extracted.
"""
from __future__ import annotations

import io
import logging
import os
import re
import tempfile
import unicodedata
from pathlib import Path

import pikepdf
from pikepdf import PdfImage
from PIL import Image

from ..config import Settings
from ..errors import PdfUserError
from ..pagespec import parse_pages
from ..results import AttachmentsResult, ExtractAttachmentsResult, ExtractImagesResult
from .base import open_pdf, require_copy_permission
from .outline import _name_tree
from .render import _check_target
from .scan import RUNNABLE_EXTENSIONS, _attachments
from .streams import decode, encoded_image, filters_of, parms_of, within

log = logging.getLogger(__name__)

MAX_ATTACHMENTS = 1_000
MAX_IMAGES = 2_000
MAX_PIXELS = 40_000_000        # per image (a 6,300 x 6,300 picture)
MAX_TOTAL_PIXELS = 400_000_000  # per call, like render_pages
MAX_FORM_DEPTH = 12            # forms inside forms, when looking for images
MAX_PARENTS = 64               # page-tree levels searched for inherited resources
MAX_NAME = 120                 # characters in a written file name
MAX_NAME_BYTES = 200           # its UTF-8 size: file systems allow 255, minus temp-name room
_UNSAFE = re.compile(r'[\x00-\x1f\x7f<>:"/\\|?*]')
# Names Windows won't create, whatever follows them (also with superscript digits).
_RESERVED = {"con", "prn", "aux", "nul", "conin$", "conout$",
             *(f"{d}{n}" for d in ("com", "lpt") for n in "0123456789¹²³")}


# --- writing files safely ----------------------------------------------------------------


def safe_filename(name: str, fallback: str) -> str:
    """A file name that stays in its folder on any system: no folders, no control,
    invisible or reserved characters, no reserved Windows names, at most MAX_NAME
    characters."""
    name = unicodedata.normalize("NFC", name)
    name = "".join(c for c in name if unicodedata.category(c) != "Cf")  # e.g. right-to-left
    base = _UNSAFE.sub("_", re.split(r"[/\\]", name)[-1]).strip(" .")
    if not base or base.split(".")[0].rstrip(" ").lower() in _RESERVED:
        base = f"{fallback}_{base}" if base else fallback
    if len(base) > MAX_NAME or len(base.encode()) > MAX_NAME_BYTES:
        stem, dot, ext = base.rpartition(".")
        ext = ext if dot and len(ext) <= 10 else ""
        suffix = "." + ext if ext else ""
        keep = MAX_NAME - len(suffix)
        while len((base[:keep] + suffix).encode()) > MAX_NAME_BYTES:
            keep -= 1
        base = base[:keep].rstrip(" .") + suffix
    return base


def _key(name: str) -> str:
    """Names a case-insensitive, normalizing file system (macOS, Windows) treats alike."""
    return unicodedata.normalize("NFC", name).casefold()


def _unique(name: str, used: set[str]) -> str:
    """*name*, or "name (2).ext" ... if a file earlier in this run already has it."""
    stem, dot, ext = name.rpartition(".")
    if not dot or not stem:
        stem, ext = name, ""
    candidate, n = name, 1
    while _key(candidate) in used:
        n += 1
        candidate = f"{stem} ({n})" + (f".{ext}" if ext else "")
    used.add(_key(candidate))
    return candidate


def _runnable_name(name: str) -> bool:
    return "." in name and name.rsplit(".", 1)[1].lower() in RUNNABLE_EXTENSIONS


def _check_all(paths: list[Path], src: Path, overwrite: bool) -> None:
    """Refuse before writing anything: links, folders and the input itself are never
    replaced, and existing files only with overwrite."""
    for path in paths:
        _check_target(path, src, overwrite)


def _write(path: Path, data: bytes) -> None:
    """Write via a temporary file and os.replace: never through a link, never in place."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.stem}.", suffix=path.suffix)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


class _Writer:
    """Writes outputs and, if the run fails part way, removes what it wrote (files it
    replaced are gone either way) and the folder if it made it."""

    def __init__(self, out_dir: Path):
        self.out_dir, self.written, self.made_dir = out_dir, [], False

    def write(self, path: Path, data: bytes) -> None:
        if not self.out_dir.exists():
            self.out_dir.mkdir(parents=True)
            self.made_dir = True
        _write(path, data)
        self.written.append(path)

    def __enter__(self):
        return self

    def __exit__(self, kind, exc, tb):
        if kind is not None:
            for path in self.written:
                path.unlink(missing_ok=True)
            if self.made_dir:
                try:
                    self.out_dir.rmdir()
                except OSError:
                    pass
        return False


def _reason(exc: BaseException, src: Path) -> str:
    """A short, path-free description of why something failed."""
    text = str(exc).strip().splitlines()[0][:200] if str(exc).strip() else type(exc).__name__
    return text.replace(str(src), src.name)


# --- attachments -----------------------------------------------------------------------------


def _filespecs(pdf: pikepdf.Pdf) -> list[tuple[pikepdf.Dictionary, int | None]]:
    """(file specification, 1-based page or None) for each attachment: the document's
    list of embedded files, then files attached to pages as annotations. Each once."""
    found, seen = [], set()

    def add(spec, page):
        if not isinstance(spec, pikepdf.Dictionary):
            return
        if spec.is_indirect:
            if spec.objgen in seen:
                return
            seen.add(spec.objgen)
        found.append((spec, page))

    names = pdf.Root.get("/Names")
    tree = names.get("/EmbeddedFiles") if isinstance(names, pikepdf.Dictionary) else None
    for spec in _name_tree(tree).values():
        add(spec, None)
    for n, page in enumerate(pdf.pages, 1):
        annots = page.obj.get("/Annots")
        for annot in annots if isinstance(annots, pikepdf.Array) else []:
            if isinstance(annot, pikepdf.Dictionary) and \
                    annot.get("/Subtype") == pikepdf.Name.FileAttachment:
                add(annot.get("/FS"), n)
    return found


def _collect(pdf: pikepdf.Pdf) -> tuple[list[tuple[dict, pikepdf.Stream]], bool]:
    """Each attachment's report and stream (at most MAX_ATTACHMENTS), and whether some
    were left out."""
    items = []
    for spec, page in _filespecs(pdf):
        found = _attachments([spec])
        if not found:  # a reference to a file outside the PDF: nothing embedded
            continue
        if len(items) >= MAX_ATTACHMENTS:
            return items, True
        info, stream = found[0]
        desc = spec.get("/Desc")
        subtype = stream.get("/Subtype")
        items.append(({
            "name": info["name"],
            "description": str(desc)[:500] if isinstance(desc, pikepdf.String) else None,
            "mime_type": str(subtype).lstrip("/") if isinstance(subtype, pikepdf.Name)
            else None,
            "size": info["size"],
            "detected_type": info["detected_type"],
            "executable": info["executable"],
            "extension_mismatch": info["extension_mismatch"],
            "page": page,
        }, stream))
    return items, False


def list_attachments(path: Path, settings: Settings,
                     password: str | None = None) -> AttachmentsResult:
    """The files embedded in a PDF, with what their bytes say they are."""
    with open_pdf(path, settings, password) as pdf:
        items, truncated = _collect(pdf)
        return {"path": str(path), "attachments": [info for info, _ in items],
                "truncated": truncated}


def extract_attachments(path: Path, out_dir: Path, settings: Settings,
                        names: list[str] | None = None, password: str | None = None,
                        overwrite: bool = False,
                        allow_executables: bool = False) -> ExtractAttachmentsResult:
    """Save embedded files into *out_dir*: all of them, or those named."""
    with open_pdf(path, settings, password) as pdf:
        require_copy_permission(pdf, "extracting attachments")
        items, truncated = _collect(pdf)
        if not items:
            raise PdfUserError("This PDF has no attachments")
        if names is not None:
            known = {info["name"] for info, _ in items}
            unknown = [n for n in names if n not in known]
            if unknown:
                more = " (only the first 1,000 are read)" if truncated else ""
                raise PdfUserError(f"No attachment named {', '.join(map(repr, unknown))}"
                                   f"{more}. Attachments: {', '.join(sorted(known)[:20])}")
            items = [(info, s) for info, s in items if info["name"] in names]
        used: set[str] = set()
        plan, skipped = [], []
        for info, stream in items:
            target = out_dir / _unique(safe_filename(info["name"], "attachment"), used)
            # Judged on the name it will have on disk too: shortening a long name can
            # leave a runnable extension the original didn't end with.
            runnable = info["executable"] or _runnable_name(target.name)
            if runnable and not allow_executables:
                skipped.append({"name": info["name"], "reason": (
                    "it can run on a computer; pass allow_executables=true to save it")})
                continue
            plan.append((info, stream, target, runnable))
        _check_all([target for _, _, target, _ in plan], path, overwrite)
        limit = settings.max_file_bytes
        work = 4 * settings.max_file_bytes  # bytes decoded in one call, kept or not
        files = []
        with _Writer(out_dir) as writer:
            for info, stream, target, runnable in plan:
                if work <= 0:
                    skipped.append({"name": info["name"], "reason": (
                        "the total size limit for one extraction was reached")})
                    continue
                data = decode(stream, min(limit, work) + 1, strict=True)
                work -= len(data) if data is not None else len(stream.read_raw_bytes())
                if data is None:
                    skipped.append({"name": info["name"], "reason": (
                        "it is damaged or stored in an encoding that can't be extracted here")})
                elif len(data) > limit:
                    skipped.append({"name": info["name"], "reason": (
                        f"it is larger than the {limit // 2**20} MB limit")})
                elif work < 0:
                    skipped.append({"name": info["name"], "reason": (
                        "the total size limit for one extraction was reached")})
                else:
                    writer.write(target, data)
                    files.append({"name": info["name"], "file": str(target),
                                  "size": len(data), "executable": runnable})
    return {"output_dir": str(out_dir), "files": files, "skipped": skipped,
            "truncated": truncated}


# --- images --------------------------------------------------------------------------------


def _page_resources(page: pikepdf.Dictionary):
    """A page's resources, inherited from the page tree if the page has none."""
    node = page
    for _ in range(MAX_PARENTS):
        if not isinstance(node, pikepdf.Dictionary):
            return None
        if "/Resources" in node:
            return node.get("/Resources")
        node = node.get("/Parent")
    return None


def _images_of(resources, depth: int, seen_forms: set, cache: dict) -> list[pikepdf.Stream]:
    """The images drawn by a page or form, including inside its forms. Results are kept
    per resource dictionary, which many pages often share."""
    key = resources.objgen if isinstance(resources, pikepdf.Dictionary) and \
        resources.is_indirect else None
    if key is not None and key in cache:
        return cache[key]
    found = []
    xobjects = resources.get("/XObject") if isinstance(resources, pikepdf.Dictionary) else None
    for _, obj in xobjects.items() if isinstance(xobjects, pikepdf.Dictionary) else []:
        if not isinstance(obj, pikepdf.Stream):
            continue
        if obj.get("/Subtype") == pikepdf.Name.Image:
            found.append(obj)
        elif obj.get("/Subtype") == pikepdf.Name.Form and depth < MAX_FORM_DEPTH:
            if obj.objgen in seen_forms:
                continue
            seen_forms.add(obj.objgen)
            found += _images_of(obj.get("/Resources"), depth + 1, seen_forms, cache)
    if key is not None:
        cache[key] = found
    return found


def _size(image: pikepdf.Stream) -> tuple[int, int] | None:
    w, h = image.get("/Width"), image.get("/Height")
    if isinstance(w, int) and isinstance(h, int) and w > 0 and h > 0:
        return int(w), int(h)
    return None


def _problem(image: pikepdf.Stream, mask: bool = False) -> str | None:
    """Why an image (or its mask) can't be converted safely, or None. The data must be
    no bigger than its declared size calls for, before anything decodes it."""
    what = "its mask" if mask else "it"
    size = _size(image)
    if size is None:
        return f"{what} has no valid size"
    pixels = size[0] * size[1]
    if pixels > MAX_PIXELS:
        return (f"{what} is {size[0]} x {size[1]} pixels, over the "
                f"{MAX_PIXELS // 1_000_000} million pixel limit")
    # Predictors work row by row; rows wider than the image make decoders allocate
    # for the claimed width.
    for p in parms_of(image):
        if p.get("/Predictor", 1) != 1 and not (
                p.get("/Columns", 1) <= size[0] and p.get("/Colors", 1) <= 4
                and p.get("/BitsPerComponent", 8) <= 16):
            return f"{what} has decoding settings that don't match its size (possibly hostile)"
    cap = pixels * 8 + 2**20  # 4 colour channels of 16 bits is the most a pixel needs
    if not within(image, cap):
        return f"{what} has data far larger than its size calls for (possibly hostile)"
    codec = (filters_of(image) or [""])[-1]
    if codec in ("/DCTDecode", "/JPXDecode"):
        # JPEG and JPEG 2000 carry their own size, which is what a decoder uses.
        data = encoded_image(image, cap)
        try:
            with Image.open(io.BytesIO(data or b"")) as picture:  # reads the header only
                real = picture.size
        except Exception:  # noqa: BLE001 - any failure to read the header
            return f"{what} has picture data that can't be read"
        if real != size:
            return (f"{what} holds a {real[0]} x {real[1]} picture, not the "
                    f"{size[0]} x {size[1]} it declares (possibly hostile)")
    if not mask:
        for key in ("/SMask", "/Mask"):  # masks are decoded and merged with the image
            other = image.get(key)
            if isinstance(other, pikepdf.Stream):
                problem = _problem(other, mask=True)
                if problem:
                    return problem
    return None


def _collect_images(pdf: pikepdf.Pdf, targets: list[int], min_size: int
                    ) -> tuple[list[pikepdf.Stream], dict, bool]:
    """Distinct images on the target pages (at least *min_size* each way), in order,
    the pages each is on, and whether there were more than MAX_IMAGES."""
    order, where, cache = [], {}, {}
    for i in targets:
        for image in _images_of(_page_resources(pdf.pages[i].obj), 0, set(), cache):
            key = image.objgen
            if key not in where:
                size = _size(image)
                if size is not None and min(size) < min_size:
                    continue  # small decoration the caller asked to leave out
                if len(order) >= MAX_IMAGES:
                    return order, where, True
                where[key] = {}
                order.append(image)
            where[key][i + 1] = None  # an ordered set of pages
    return order, where, False


def extract_images(path: Path, out_dir: Path, settings: Settings, pages: str | None = None,
                   min_size: int = 0, password: str | None = None,
                   overwrite: bool = False) -> ExtractImagesResult:
    """Save the pictures on all pages, or the pages given, into *out_dir*. An image used
    on several pages is saved once."""
    if not isinstance(min_size, int) or isinstance(min_size, bool) or min_size < 0:
        raise PdfUserError("min_size must be 0 or more pixels")
    with open_pdf(path, settings, password) as pdf:
        require_copy_permission(pdf, "extracting images")
        total = len(pdf.pages)
        targets = parse_pages(pages, total, dedupe=True) if pages else list(range(total))
        order, where, truncated = _collect_images(pdf, targets, min_size)
        skipped, chosen, planned_pixels = [], [], 0
        for image in order:
            width, height = _size(image) or (0, 0)
            if planned_pixels + width * height > MAX_TOTAL_PIXELS:
                # Over this call's budget: not worth checking (checks decode data too).
                skipped.append({"page": next(iter(where[image.objgen])), "reason": (
                    "the total size limit for one extraction was reached")})
                continue
            problem = _problem(image)
            if problem:
                skipped.append({"page": next(iter(where[image.objgen])), "reason": problem})
            else:
                chosen.append(image)
                planned_pixels += width * height
        # Name every output, and check none exists, before writing any. The extension
        # (.png, .jpg, ...) is only known once converted, so any file with the stem counts.
        per_page: dict[int, int] = {}
        planned = []
        for image in chosen:
            first = next(iter(where[image.objgen]))
            per_page[first] = per_page.get(first, 0) + 1
            planned.append((image, first, f"page{first:03d}-image{per_page[first]:02d}"))
        _check_all([hit for _, _, stem in planned for hit in out_dir.glob(stem + ".*")],
                   path, overwrite)
        images, budget = [], 4 * settings.max_file_bytes
        with _Writer(out_dir) as writer:
            for image, first, stem in planned:
                width, height = _size(image)
                try:
                    buf = io.BytesIO()
                    ext = PdfImage(image).extract_to(stream=buf)
                except Exception as exc:  # noqa: BLE001 - any decoder failure skips the image
                    log.info("image on page %d not converted: %r", first, exc)
                    skipped.append({"page": first,
                                    "reason": f"it can't be converted: {_reason(exc, path)}"})
                    continue
                data = buf.getvalue()
                if len(data) > budget:
                    skipped.append({"page": first, "reason": (
                        "the total size limit for one extraction was reached")})
                    continue
                target = out_dir / f"{stem}{ext}"
                _check_target(target, path, overwrite)
                writer.write(target, data)
                budget -= len(data)
                images.append({"file": str(target), "page": first,
                               "pages": list(where[image.objgen]), "width": width,
                               "height": height, "format": ext.lstrip(".")})
    return {"output_dir": str(out_dir), "images": images, "skipped": skipped,
            "truncated": truncated}
