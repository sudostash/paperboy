"""MCP server: a thin adapter exposing the :mod:`paperboy.engine` as MCP tools.

The server owns policy (path confinement, size/page caps, overwrite rules) and
error presentation; the engine owns PDF behavior. Keeping them separate means a
future streamable-HTTP transport can import the same engine unchanged.
"""

import base64
import functools
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Annotated

from mcp.types import CallToolResult, ImageContent, TextContent, ToolAnnotations
from pydantic import Field

from . import SERVER_NAME, __version__, engine
from .config import Settings
from .engine.base import KNOWN_USER_PASSWORD, known_user_password, open_pdf
from .errors import PdfUserError
from .paths import prepare_output, prepare_output_dir, require_input, resolve, same_file
from .results import (
    AttachmentsResult,
    BlankPagesResult,
    Bookmark,
    CheckResult,
    CompareResult,
    ConvertResult,
    CropResult,
    DecryptResult,
    DeleteResult,
    EncryptResult,
    ExportTablesResult,
    ExtractAttachmentsResult,
    ExtractImagesResult,
    ExtractResult,
    FillFormResult,
    FlattenResult,
    FormFieldsResult,
    HiddenDataResult,
    ImagesResult,
    InfoResult,
    InsertBlankResult,
    InterleaveResult,
    MergeResult,
    MetadataResult,
    NUpResult,
    OptimizeResult,
    OutlineResult,
    PageNumbersResult,
    PipelineResult,
    PipelineStepInput,
    RearrangeResult,
    RenderResult,
    RepairResult,
    ResizeResult,
    RotateResult,
    SanitizeResult,
    ScanResult,
    SearchResult,
    SetMetadataResult,
    SetOutlineResult,
    SplitResult,
    TablesResult,
    TextResult,
    ViewResult,
    WatermarkResult,
)

try:  # mcp >= 2.0 renamed FastMCP to MCPServer
    from mcp.server.mcpserver import MCPServer as _Server
    from mcp.server.mcpserver.exceptions import ToolError
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server
    from mcp.server.fastmcp.exceptions import ToolError

logger = logging.getLogger("paperboy")

SETTINGS = Settings.from_env()
mcp = _Server(SERVER_NAME)


def _guard(fn):
    """Turn anticipated engine failures into visible MCP tool errors.

    Any other exception is logged here and replaced with a generic message, so
    internal details never reach the client (mcp 1.x would forward them).
    ``functools.wraps`` sets ``__wrapped__`` so the MCP SDK still reads the real
    parameter signature when building each tool's input schema.
    """
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except ToolError:
            raise
        except PdfUserError as exc:
            raise ToolError(str(exc)) from exc
        except Exception as exc:
            logger.exception("Unexpected error in %s", fn.__name__)
            raise ToolError(
                f"Internal error in {fn.__name__}; details were logged by the server"
            ) from exc
    return wrapper


def _reads(title: str) -> ToolAnnotations:
    """Annotations for tools that only inspect a PDF; clients may run them unprompted."""
    return ToolAnnotations.model_validate({
        "title": title, "readOnlyHint": True, "idempotentHint": True, "openWorldHint": False,
    })


def _writes(title: str) -> ToolAnnotations:
    """Annotations for tools that write a file.

    Inputs are never modified, but ``overwrite=true`` can replace an existing
    output, so these stay marked destructive and clients keep confirming them.
    Validated by alias because mcp 1.x and 2.x name the fields differently.
    """
    return ToolAnnotations.model_validate({
        "title": title, "readOnlyHint": False, "destructiveHint": True,
        "idempotentHint": True, "openWorldHint": False,
    })


def _overwrite(flag: bool | None) -> bool:
    return SETTINGS.default_overwrite if flag is None else flag


def _combined_output(output_path: str, inputs: list, overwrite: bool | None):
    """Resolve the output of a many-to-one tool; it may never replace one of its inputs."""
    out = resolve(output_path, SETTINGS)
    if any(same_file(out, src) for src in inputs):
        raise ToolError("Output path is the same as an input; choose a different output_path")
    if out.exists() and not _overwrite(overwrite):
        raise ToolError(f"{out} already exists (pass overwrite=true to replace it)")
    out.parent.mkdir(parents=True, exist_ok=True)
    return out


# --------------------------------------------------------------------------- #
# Inspection
# --------------------------------------------------------------------------- #


@mcp.tool(annotations=_reads("PDF info"))
@_guard
def pdf_info(input_path: str, password: str | None = None) -> InfoResult:
    """Report page count, file size, PDF version, encryption status and metadata.

    Works on an encrypted file without a password (returns is_encrypted only).
    """
    return engine.pdf_info(require_input(input_path, SETTINGS), SETTINGS, password)


@mcp.tool(annotations=_reads("Get metadata"))
@_guard
def get_metadata(input_path: str, password: str | None = None) -> MetadataResult:
    """Read document metadata: title, author, subject, keywords, creator, producer."""
    return engine.get_metadata(require_input(input_path, SETTINGS), SETTINGS, password)


@mcp.tool(annotations=_reads("Check PDF"))
@_guard
def check_pdf(input_path: str, password: str | None = None) -> CheckResult:
    """Validate structural integrity and report any problems found."""
    return engine.check_pdf(require_input(input_path, SETTINGS), SETTINGS, password)


@mcp.tool(annotations=_reads("Extract text"))
@_guard
def extract_text(
    input_path: str,
    pages: str | None = None,
    password: str | None = None,
    max_chars: int = 50000,
) -> TextResult:
    """Extract text from all pages or a page spec (e.g. "1-3,7"). Returns text + truncation flag."""
    return engine.extract_text(
        require_input(input_path, SETTINGS), SETTINGS, pages, password, max_chars
    )


@mcp.tool(annotations=_reads("Search text"))
@_guard
def search_text(
    input_path: str,
    query: str,
    pages: str | None = None,
    match_case: bool = False,
    whole_word: bool = False,
    max_results: int = 100,
    password: str | None = None,
) -> SearchResult:
    """Find a word or phrase in a PDF's text (literal text, not a pattern), on all pages
    or a page spec (e.g. "1-3,7"). Each match has its page, the matched text, a snippet
    of the text around it, and its rectangles on the page ([left, bottom, right, top] in
    PDF points, in the page's own coordinates before rotation; one or more per line).

    The query is normalized (NFKC; whitespace runs count as one space; surrounding
    spaces are ignored, so use whole_word to match whole words only). A space can match
    across a line break. Matching ignores case (simple case folding) unless match_case.
    Emoji and other characters beyond U+FFFF can't be searched.

    Returns up to max_results (1-1000) in page order; truncated means there are more.
    pages_without_text lists pages with no extractable text (e.g. scans); pages_skipped
    lists pages with too much content to search safely; complete is false if the 60 s
    time limit stopped the search early. Needs the owner password if the PDF forbids
    copying.
    """
    return engine.search_text(require_input(input_path, SETTINGS), query, SETTINGS, pages,
                              match_case, whole_word, max_results, password)


@mcp.tool(annotations=_reads("Extract tables"))
@_guard
def extract_tables(
    input_path: str,
    pages: str | None = None,
    strategy: str = "lines",
    max_tables: int = 20,
    password: str | None = None,
) -> TablesResult:
    """Find tables on all pages or a page spec (e.g. "1-3,7") and return each as rows of
    cell text (empty and merged cells are ""). strategy "lines" finds tables drawn with
    lines between cells; "text" finds tables laid out by alignment only (rougher).
    Returns up to max_tables (1-200; 50,000 cells in all; 1,000 characters per cell);
    truncated means there are more (complete is then still true). Pages that can't be
    read safely (too much content, lines too dense to analyse, damaged, or too slow)
    are listed in pages_skipped with the reason; complete is false if the 60 s limit
    per call stopped it before every page was read. Scanned pages have no text to read.
    To save tables as CSV, use export_tables. Needs the owner password if the PDF
    forbids copying.
    """
    return engine.extract_tables(require_input(input_path, SETTINGS), SETTINGS, pages,
                                 strategy, max_tables, password)


@mcp.tool(annotations=_writes("Export tables"))
@_guard
def export_tables(
    input_path: str,
    pages: str | None = None,
    strategy: str = "lines",
    max_tables: int = 20,
    escape_formulas: bool = True,
    output_dir: str | None = None,
    password: str | None = None,
    overwrite: bool | None = None,
) -> ExportTablesResult:
    """Save the tables extract_tables finds as CSV files (UTF-8, readable by Excel), one
    per table, named page001-table01.csv and so on, in a folder (default:
    "<name>_tables" next to the PDF). Spreadsheets run cells starting with = + - @ (or
    their full-width forms) as formulas, so such cells get a leading apostrophe unless
    escape_formulas=false (plain numbers like -5 are left alone). Limits and
    pages_skipped are as for extract_tables. Nothing is written if a file exists, unless
    overwrite. Needs the owner password if the PDF forbids copying.
    """
    src = require_input(input_path, SETTINGS)
    out_dir = _output_folder(src, output_dir, "_tables")
    return engine.export_tables(src, out_dir, SETTINGS, pages, strategy, max_tables,
                                escape_formulas, password, _overwrite(overwrite))


# --------------------------------------------------------------------------- #
# Security
# --------------------------------------------------------------------------- #


@mcp.tool(annotations=_writes("Encrypt PDF"))
@_guard
def encrypt_pdf(
    input_path: str,
    user_password: str,
    output_path: str | None = None,
    owner_password: str | None = None,
    algorithm: str = "AES-256",
    allow_print: bool = True,
    allow_copy: bool = True,
    allow_modify: bool = True,
    current_password: str | None = None,
    overwrite: bool | None = None,
) -> EncryptResult:
    """Add a password to a PDF.

    user_password opens the file; owner_password (defaults to it) controls
    permissions. algorithm: AES-256 (default), AES-128, RC4-128, RC4-40.
    allow_print/copy/modify set viewer-enforced restrictions. Use current_password
    if the input is already encrypted.
    """
    src = require_input(input_path, SETTINGS)
    out = prepare_output(src, output_path, "_encrypted", _overwrite(overwrite), SETTINGS)
    return engine.encrypt_pdf(
        src, out, user_password, SETTINGS, owner_password, algorithm,
        allow_print, allow_copy, allow_modify, current_password,
    )


@mcp.tool(annotations=_writes("Decrypt PDF"))
@_guard
def decrypt_pdf(
    input_path: str,
    password: str,
    output_path: str | None = None,
    overwrite: bool | None = None,
) -> DecryptResult:
    """Remove the password from a PDF, given its correct password. Not a cracker."""
    src = require_input(input_path, SETTINGS)
    out = prepare_output(src, output_path, "_decrypted", _overwrite(overwrite), SETTINGS)
    return engine.decrypt_pdf(src, out, password, SETTINGS)


# --------------------------------------------------------------------------- #
# Pages
# --------------------------------------------------------------------------- #


@mcp.tool(annotations=_writes("Rearrange pages"))
@_guard
def rearrange_pages(
    input_path: str,
    page_order: str,
    output_path: str | None = None,
    append_unlisted: bool = False,
    password: str | None = None,
    overwrite: bool | None = None,
) -> RearrangeResult:
    """Reorder/duplicate/reverse pages. page_order e.g. "3,1,2", "5-1" (reverse), "last,1-4".

    Unlisted pages are dropped unless append_unlisted=true. Pages may repeat to duplicate.
    """
    src = require_input(input_path, SETTINGS)
    out = prepare_output(src, output_path, "_reordered", _overwrite(overwrite), SETTINGS)
    return engine.rearrange_pages(src, out, page_order, SETTINGS, append_unlisted, password)


@mcp.tool(annotations=_writes("Delete pages"))
@_guard
def delete_pages(
    input_path: str,
    pages: str,
    output_path: str | None = None,
    password: str | None = None,
    overwrite: bool | None = None,
) -> DeleteResult:
    """Delete pages, e.g. pages="2,5-7"."""
    src = require_input(input_path, SETTINGS)
    out = prepare_output(src, output_path, "_trimmed", _overwrite(overwrite), SETTINGS)
    return engine.delete_pages(src, out, pages, SETTINGS, password)


@mcp.tool(annotations=_writes("Extract pages"))
@_guard
def extract_pages(
    input_path: str,
    pages: str,
    output_path: str | None = None,
    password: str | None = None,
    overwrite: bool | None = None,
) -> ExtractResult:
    """Save only the given pages as a new PDF, e.g. pages="1-3,10"."""
    src = require_input(input_path, SETTINGS)
    out = prepare_output(src, output_path, "_extract", _overwrite(overwrite), SETTINGS)
    return engine.extract_pages(src, out, pages, SETTINGS, password)


@mcp.tool(annotations=_writes("Rotate pages"))
@_guard
def rotate_pages(
    input_path: str,
    degrees: int,
    pages: str | None = None,
    output_path: str | None = None,
    password: str | None = None,
    overwrite: bool | None = None,
) -> RotateResult:
    """Rotate pages clockwise by 90/180/270 degrees (all pages if pages omitted)."""
    src = require_input(input_path, SETTINGS)
    out = prepare_output(src, output_path, "_rotated", _overwrite(overwrite), SETTINGS)
    return engine.rotate_pages(src, out, degrees, SETTINGS, pages, password)


@mcp.tool(annotations=_writes("Merge PDFs"))
@_guard
def merge_pdfs(
    input_paths: list[str],
    output_path: str,
    passwords: dict[str, str] | None = None,
    overwrite: bool | None = None,
) -> MergeResult:
    """Merge PDFs in the given order into output_path.

    passwords optionally maps an input path to its password for encrypted inputs.
    """
    if not isinstance(input_paths, list) or len(input_paths) < 2:
        raise ToolError("Provide at least two input PDFs to merge")
    srcs = [require_input(p, SETTINGS) for p in input_paths]
    resolved_pw: dict[str, str] = {}
    for original, resolved in zip(input_paths, srcs, strict=True):
        if passwords and original in passwords:
            resolved_pw[str(resolved)] = passwords[original]
    out = _combined_output(output_path, srcs, overwrite)
    return engine.merge_pdfs(srcs, out, SETTINGS, resolved_pw)


@mcp.tool(annotations=_writes("Crop pages"))
@_guard
def crop_pages(
    input_path: str,
    margins: list[float] | None = None,
    unit: str = "pt",
    pages: str | None = None,
    reset: bool = False,
    output_path: str | None = None,
    password: str | None = None,
    overwrite: bool | None = None,
) -> CropResult:
    """Crop pages by margins [top, right, bottom, left], measured on the page as it is
    seen (rotation applied), in unit "pt" (points), "in" or "mm"; all pages or a page
    spec. reset=true instead shows the whole page again. Cropping only hides the
    margins: the content stays in the file (use sanitize or redaction to remove it).
    """
    src = require_input(input_path, SETTINGS)
    out = prepare_output(src, output_path, "_cropped", _overwrite(overwrite), SETTINGS)
    return engine.crop_pages(src, out, SETTINGS, margins, unit, pages, reset, password)


@mcp.tool(annotations=_writes("Resize pages"))
@_guard
def resize_pages(
    input_path: str,
    size: str | None = None,
    width: float | None = None,
    height: float | None = None,
    unit: str = "pt",
    orientation: str = "auto",
    pages: str | None = None,
    output_path: str | None = None,
    password: str | None = None,
    overwrite: bool | None = None,
) -> ResizeResult:
    """Change pages to a paper size (a3, a4, a5, letter, legal, tabloid) or a custom
    width and height in unit "pt", "in" or "mm", scaling each page's content to fit
    (proportions kept, centred). orientation "auto" keeps each page's own (landscape
    pages get a landscape sheet), or force "portrait"/"landscape". Links, form fields
    and comments move with the content (ones outside a crop stay hidden); scroll
    positions of bookmarks and links that point into resized pages are not adjusted.
    Needs the owner password if the PDF forbids changes.
    """
    src = require_input(input_path, SETTINGS)
    out = prepare_output(src, output_path, "_resized", _overwrite(overwrite), SETTINGS)
    return engine.resize_pages(src, out, SETTINGS, size, width, height, unit, orientation,
                               pages, password)


@mcp.tool(annotations=_writes("N-up"))
@_guard
def n_up(
    input_path: str,
    per_sheet: int = 2,
    size: str | None = None,
    pages: str | None = None,
    output_path: str | None = None,
    password: str | None = None,
    overwrite: bool | None = None,
) -> NUpResult:
    """Put 2 pages side by side on landscape sheets, or 4 in a 2 x 2 grid, in reading
    order (handouts, saving paper). Sheets are a paper size (a3, a4, a5, letter, legal,
    tabloid) or by default the first page's size. pages picks and orders the pages.
    The result is for printing and reading: only page content is placed, so links,
    comments, stamps, form fields (and their filled-in values) and visible signatures
    don't appear; use flatten_pdf first to keep them. Document info is copied, bookmarks
    are not. Needs the owner password if the PDF forbids changes.
    """
    src = require_input(input_path, SETTINGS)
    out = prepare_output(src, output_path, f"_{per_sheet}up", _overwrite(overwrite), SETTINGS)
    return engine.n_up(src, out, SETTINGS, per_sheet, size, pages, password)


@mcp.tool(annotations=_writes("Insert blank pages"))
@_guard
def insert_blank_pages(
    input_path: str,
    after: list[int],
    count: int = 1,
    output_path: str | None = None,
    password: str | None = None,
    overwrite: bool | None = None,
) -> InsertBlankResult:
    """Insert count blank pages (1-100) after each page number in after; 0 inserts
    before the first page. E.g. after=[3, 7] adds a blank page after pages 3 and 7.
    Each blank page is the size of the page before it. inserted lists the new pages'
    numbers in the output.
    """
    src = require_input(input_path, SETTINGS)
    out = prepare_output(src, output_path, "_blanks", _overwrite(overwrite), SETTINGS)
    return engine.insert_blank_pages(src, out, SETTINGS, after, count, password)


@mcp.tool(annotations=_writes("Interleave PDFs"))
@_guard
def interleave_pdfs(
    front_path: str,
    back_path: str,
    output_path: str,
    reverse_backs: bool = True,
    front_password: str | None = None,
    back_password: str | None = None,
    overwrite: bool | None = None,
) -> InterleaveResult:
    """Combine the fronts and backs of a double-sided document scanned on a single-sided
    scanner into one PDF: front 1, back 1, front 2, back 2, ... Turning the stack over
    to scan the backs puts them last page first, so they are reversed; pass
    reverse_backs=false if they were scanned in order. The backs must have as many
    pages as the fronts, or one fewer.
    """
    fronts = require_input(front_path, SETTINGS)
    backs = require_input(back_path, SETTINGS)
    out = _combined_output(output_path, [fronts, backs], overwrite)
    return engine.interleave_pdfs(fronts, backs, out, SETTINGS, reverse_backs,
                                  front_password, back_password)


@mcp.tool(annotations=_writes("Split PDF"))
@_guard
def split_pdf(
    input_path: str,
    ranges: str | None = None,
    output_dir: str | None = None,
    password: str | None = None,
    overwrite: bool | None = None,
) -> SplitResult:
    """Split a PDF into several files.

    ranges: semicolon-separated page specs, one file each, e.g. "1-3;4-6;7-".
    If omitted, every page becomes its own file.
    """
    src = require_input(input_path, SETTINGS)
    out_dir = (
        prepare_output_dir(output_dir, SETTINGS)
        if output_dir
        else src.parent / f"{src.stem}_split"
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    return engine.split_pdf(src, out_dir, SETTINGS, ranges, password, _overwrite(overwrite))


# --------------------------------------------------------------------------- #
# Optimization
# --------------------------------------------------------------------------- #


@mcp.tool(annotations=_writes("Optimize PDF"))
@_guard
def optimize_pdf(
    input_path: str,
    output_path: str | None = None,
    linearize: bool = True,
    compress: bool = True,
    password: str | None = None,
    overwrite: bool | None = None,
) -> OptimizeResult:
    """Shrink a PDF: linearize ("fast web view") and recompress object streams."""
    src = require_input(input_path, SETTINGS)
    out = prepare_output(src, output_path, "_optimized", _overwrite(overwrite), SETTINGS)
    return engine.optimize_pdf(src, out, SETTINGS, linearize, compress, password)


@mcp.tool(annotations=_writes("Repair PDF"))
@_guard
def repair_pdf(
    input_path: str,
    output_path: str | None = None,
    password: str | None = None,
    overwrite: bool | None = None,
) -> RepairResult:
    """Rebuild a damaged PDF's structure by reading and rewriting it."""
    src = require_input(input_path, SETTINGS)
    out = prepare_output(src, output_path, "_repaired", _overwrite(overwrite), SETTINGS)
    return engine.repair_pdf(src, out, SETTINGS, password)


@mcp.tool(annotations=_writes("Flatten PDF"))
@_guard
def flatten_pdf(
    input_path: str,
    output_path: str | None = None,
    password: str | None = None,
    overwrite: bool | None = None,
) -> FlattenResult:
    """Flatten form fields and annotations into page content (requires the qpdf CLI)."""
    src = require_input(input_path, SETTINGS)
    out = prepare_output(src, output_path, "_flattened", _overwrite(overwrite), SETTINGS)
    return engine.flatten_pdf(src, out, SETTINGS, password)


# --------------------------------------------------------------------------- #
# Metadata write
# --------------------------------------------------------------------------- #


@mcp.tool(annotations=_writes("Set metadata"))
@_guard
def set_metadata(
    input_path: str,
    fields: dict[str, str],
    output_path: str | None = None,
    password: str | None = None,
    overwrite: bool | None = None,
) -> SetMetadataResult:
    """Set metadata. fields keys: title, author, subject, keywords, creator, producer."""
    if not fields:
        raise ToolError("Provide at least one metadata field to set")
    src = require_input(input_path, SETTINGS)
    out = prepare_output(src, output_path, "_meta", _overwrite(overwrite), SETTINGS)
    return engine.set_metadata(src, out, fields, SETTINGS, password)


# --------------------------------------------------------------------------- #
# Creation
# --------------------------------------------------------------------------- #


@mcp.tool(annotations=_writes("Convert to PDF"))
@_guard
def convert_to_pdf(
    input_path: str,
    output_path: str | None = None,
    overwrite: bool | None = None,
) -> ConvertResult:
    """Create a PDF from an image, a text/markdown file, or an Office/HTML document.

    Office/HTML (docx, xlsx, pptx, odt, html, ...) requires LibreOffice installed.
    """
    src = require_input(input_path, SETTINGS)
    out = prepare_output(src, output_path, "", _overwrite(overwrite), SETTINGS)
    return engine.convert_to_pdf(src, out, SETTINGS)


@mcp.tool(annotations=_writes("Images to PDF"))
@_guard
def images_to_pdf(
    input_paths: list[str],
    output_path: str,
    overwrite: bool | None = None,
) -> ImagesResult:
    """Combine images into a single PDF, one image per page, in order."""
    if not isinstance(input_paths, list) or not input_paths:
        raise ToolError("Provide at least one image path")
    srcs = [require_input(p, SETTINGS) for p in input_paths]
    out = _combined_output(output_path, srcs, overwrite)
    return engine.images_to_pdf(srcs, out, SETTINGS)


# --------------------------------------------------------------------------- #
# Forms
# --------------------------------------------------------------------------- #


@mcp.tool(annotations=_reads("List form fields"))
@_guard
def list_form_fields(input_path: str, password: str | None = None) -> FormFieldsResult:
    """List a PDF form's fields: full name, type, current value, allowed options, flags, page.

    Use the names with fill_form. If copying is forbidden and only the open password
    is given, values are withheld (values_hidden) but the fields are still listed.
    """
    src = require_input(input_path, SETTINGS)
    return engine.list_form_fields(src, SETTINGS, password)


@mcp.tool(annotations=_writes("Fill form"))
@_guard
def fill_form(
    input_path: str,
    values: dict[str, str | bool],
    output_path: str | None = None,
    flatten: bool = False,
    password: str | None = None,
    overwrite: bool | None = None,
) -> FillFormResult:
    """Fill in form fields: values maps each field's full name to its value.

    Text and choice fields take text; checkboxes take true/false; radio groups take one
    of their options, or "Off" to clear (see list_form_fields). Multi-select list boxes
    take a single value. Every value is checked first and nothing is written if any is
    invalid. If unrenderable_fields is not empty, the form's font can't draw those values:
    they are stored, and most viewers redraw them correctly. flatten=true makes the result
    static (no longer editable). Signed PDFs are refused because filling would break the
    signature.
    """
    src = require_input(input_path, SETTINGS)
    out = prepare_output(src, output_path, "_filled", _overwrite(overwrite), SETTINGS)
    return engine.fill_form(src, out, values, SETTINGS, password, flatten)


# --------------------------------------------------------------------------- #
# Bookmarks
# --------------------------------------------------------------------------- #


@mcp.tool(annotations=_reads("Get bookmarks"))
@_guard
def get_outline(input_path: str, password: str | None = None) -> OutlineResult:
    """List a PDF's bookmarks (its outline) in reading order: each one's level (1 = top,
    2 = nested under the one before, ...), title and 1-based target page (null if it
    doesn't go to a page here). Bookmarks that open a web or email address have link set;
    ones that do something else (e.g. run a script) have action set. truncated means some were
    left out (over 10,000, or nested over 20 levels). Edit the list and pass it to
    set_outline to change them.
    """
    src = require_input(input_path, SETTINGS)
    return engine.get_outline(src, SETTINGS, password)


@mcp.tool(annotations=_writes("Set bookmarks"))
@_guard
def set_outline(
    input_path: str,
    entries: list[Bookmark],
    output_path: str | None = None,
    password: str | None = None,
    overwrite: bool | None = None,
) -> SetOutlineResult:
    """Replace a PDF's bookmarks (its outline) with entries, in reading order. Each has a
    title, a level (default 1, up to 20; at most one deeper than the entry before it, so
    level 2 nests under the previous level-1 entry) and either a 1-based page or a link
    (http, https or mailto); with neither it is a heading that goes nowhere. An empty
    list removes all bookmarks. Everything is checked before anything is written.

    To add to existing bookmarks, read them with get_outline first and pass them back
    with yours (check its truncated flag). Rewriting keeps titles, nesting, pages and
    links; other actions (scripts and the like), colour and style, open/closed state and
    exact scroll positions are not kept. The result opens with the bookmarks panel
    showing. Needs the owner password if the PDF forbids document assembly; signed PDFs
    are refused because the change would break the signature.
    """
    src = require_input(input_path, SETTINGS)
    out = prepare_output(src, output_path, "_bookmarked", _overwrite(overwrite), SETTINGS)
    return engine.set_outline(src, out, list(entries), SETTINGS, password)


# --------------------------------------------------------------------------- #
# Attachments and images
# --------------------------------------------------------------------------- #


def _output_folder(src, output_dir: str | None, suffix: str):
    """The output folder: as given, or "<name><suffix>" next to the input; either way
    resolved and confined to the root (a planted link can't redirect it). It is only
    created when the first file is written."""
    folder = resolve(output_dir or str(src.parent / f"{src.stem}{suffix}"), SETTINGS)
    if folder.exists() and not folder.is_dir():
        raise PdfUserError(f"{folder} exists and is not a folder")
    return folder


@mcp.tool(annotations=_reads("List attachments"))
@_guard
def list_attachments(input_path: str, password: str | None = None) -> AttachmentsResult:
    """List the files embedded in a PDF (attached to the document or to a page): name,
    description, declared type and size, the type detected from the file's own bytes,
    and whether it can run on a computer or has a name that hides what it is.
    Use the names with extract_attachments.
    """
    src = require_input(input_path, SETTINGS)
    return engine.list_attachments(src, SETTINGS, password)


@mcp.tool(annotations=_writes("Extract attachments"))
@_guard
def extract_attachments(
    input_path: str,
    names: list[str] | None = None,
    output_dir: str | None = None,
    allow_executables: bool = False,
    password: str | None = None,
    overwrite: bool | None = None,
) -> ExtractAttachmentsResult:
    """Save files embedded in a PDF into a folder (default: "<name>_attachments" next to
    it): all of them, or those named (see list_attachments). Names are made safe to use
    as file names; prefer a new folder, since saved files can have any name. Attachments
    that can run on a computer (programs, scripts, ...) are skipped unless
    allow_executables=true; only set that if the user asked for them. Files that are
    damaged, over the file size limit or in an unsupported encoding are listed in
    skipped, as are files past a total of 4x that limit per call; only the first 1,000
    attachments are read (truncated). Saved files are not password-protected, even if
    the PDF is. Needs the owner password if the PDF forbids copying.
    """
    src = require_input(input_path, SETTINGS)
    out_dir = _output_folder(src, output_dir, "_attachments")
    return engine.extract_attachments(src, out_dir, SETTINGS, names, password,
                                      _overwrite(overwrite), allow_executables)


@mcp.tool(annotations=_writes("Extract images"))
@_guard
def extract_images(
    input_path: str,
    pages: str | None = None,
    min_size: int = 0,
    output_dir: str | None = None,
    password: str | None = None,
    overwrite: bool | None = None,
) -> ExtractImagesResult:
    """Save the pictures in a PDF as image files, in a folder (default: "<name>_images"
    next to it), named page001-image01.png and so on. pages limits it to a page spec
    (e.g. "1-3,7"); min_size (pixels) leaves out images narrower or shorter than that,
    such as icons and rules. Photos are usually saved as the original JPEG; others as
    PNG (or the format the image is stored in). An image used on several pages is saved
    once. Images drawn by pages and the forms on them are found; inline images and
    those inside annotations, patterns or Type 3 fonts are not. Images that are damaged,
    suspicious (data that doesn't match their size), over 40 million pixels, or past
    400 million pixels per call are listed in skipped; only the first 2,000 are read
    (truncated). Saved files are not password-protected, even if the PDF is. Needs the
    owner password if the PDF forbids copying.
    """
    src = require_input(input_path, SETTINGS)
    out_dir = _output_folder(src, output_dir, "_images")
    return engine.extract_images(src, out_dir, SETTINGS, pages, min_size, password,
                                 _overwrite(overwrite))


# --------------------------------------------------------------------------- #
# Hidden data
# --------------------------------------------------------------------------- #


@mcp.tool(annotations=_reads("Inspect hidden data"))
@_guard
def inspect_hidden_data(input_path: str, password: str | None = None) -> HiddenDataResult:
    """Report hidden data a PDF carries before sharing it: metadata (author, software),
    scripts and risky actions, attachments, comments, application data, thumbnails,
    and earlier saved versions still inside the file. Changes nothing.
    """
    src = require_input(input_path, SETTINGS)
    return engine.inspect_hidden_data(src, SETTINGS, password)


@mcp.tool(annotations=_reads("Scan PDF"))
@_guard
def scan_pdf(input_path: str, password: str | None = None,
             antivirus: bool = True) -> ScanResult:
    """Scan a PDF you didn't make for risks: scripts (especially ones that run on open),
    actions that launch programs, attachments that are programs or disguise their type,
    disguised names, hidden data around the file, and (if ClamAV is installed) known
    malware. verdict is no_active_content_found, active_content or suspicious, never "safe":
    a scan can't prove that. To remove active content, use sanitize_pdf.
    """
    src = require_input(input_path, SETTINGS)
    return engine.scan_pdf(src, SETTINGS, password, antivirus)


@mcp.tool(annotations=_writes("Sanitize PDF"))
@_guard
def sanitize_pdf(
    input_path: str,
    output_path: str | None = None,
    remove_metadata: bool = True,
    remove_scripts: bool = True,
    remove_attachments: bool = True,
    remove_comments: bool = False,
    password: str | None = None,
    overwrite: bool | None = None,
) -> SanitizeResult:
    """Write a copy without hidden data: metadata (incl. comment authors and camera data
    in images), scripts and risky actions, and attachments (comments too if
    remove_comments=true), plus application data, thumbnails, earlier saved versions and
    unused resources; gives it a new random ID. Links, bookmarks, form fields and
    accessibility structure are kept. Not removed: hidden layers, text hidden by colour or
    position, form field values, and the second half of an encrypted copy's ID (set by
    the PDF library from the time). removed is measured on the result; if anything asked
    for can't be removed, nothing is written. Signed PDFs are refused.
    """
    src = require_input(input_path, SETTINGS)
    out = prepare_output(src, output_path, "_sanitized", _overwrite(overwrite), SETTINGS)
    return engine.sanitize_pdf(src, out, SETTINGS, remove_metadata, remove_scripts,
                               remove_attachments, remove_comments, password)


# --------------------------------------------------------------------------- #
# Watermarks and page numbers
# --------------------------------------------------------------------------- #


@mcp.tool(annotations=_writes("Add watermark"))
@_guard
def add_watermark(
    input_path: str,
    text: str | None = None,
    image_path: str | None = None,
    position: str = "center",
    pages: str | None = None,
    opacity: float = 0.2,
    rotation: float | None = None,
    font_size: float | None = None,
    scale: float = 0.5,
    color: str = "gray",
    layer: str = "over",
    output_path: str | None = None,
    password: str | None = None,
    overwrite: bool | None = None,
) -> WatermarkResult:
    """Add a text watermark ("CONFIDENTIAL", "DRAFT") or an image (a logo) to pages.

    position: center (default) or top-/bottom- left/center/right; a corner with
    opacity=1 makes a stamp such as "APPROVED". opacity 0-1 (default 0.2); rotation in
    degrees (default 45 at the center, 0 elsewhere); font_size auto-fits if omitted; scale is the
    image width as a share of the page. color: #rrggbb, black, gray, red, blue, green.
    layer: over (default) or under the content. Text is Latin characters only.
    """
    src = require_input(input_path, SETTINGS)
    image = require_input(image_path, SETTINGS) if image_path else None
    out = prepare_output(src, output_path, "_watermarked", _overwrite(overwrite), SETTINGS)
    return engine.add_watermark(src, out, SETTINGS, text, image, position, pages, opacity,
                                rotation, font_size, scale, color, layer, password)


@mcp.tool(annotations=_writes("Add page numbers"))
@_guard
def add_page_numbers(
    input_path: str,
    format: str = "Page {n} of {total}",
    position: str = "bottom-center",
    start: int = 1,
    pages: str | None = None,
    font_size: float = 10,
    margin: float = 36,
    color: str = "black",
    output_path: str | None = None,
    password: str | None = None,
    overwrite: bool | None = None,
) -> PageNumbersResult:
    """Add page numbers, or Bates numbers for legal documents (format="ACME-{n:06}").

    format uses {n} (required), {total} and zero padding like {n:06}. Page k shows
    n = k + start - 1, so start=0 with pages="2-" skips a cover page and numbers the
    next page 1. position: top-/bottom- left/center/right (default bottom-center).
    """
    src = require_input(input_path, SETTINGS)
    out = prepare_output(src, output_path, "_numbered", _overwrite(overwrite), SETTINGS)
    return engine.add_page_numbers(src, out, SETTINGS, format, position, start, pages,
                                   font_size, margin, color, password)


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #


@mcp.tool(annotations=_reads("View pages"))
@_guard
def view_pages(
    input_path: str,
    pages: str | None = None,
    max_side_px: int = 1568,
    password: str | None = None,
) -> ViewResult:
    """Look at pages as images: layout, scans, charts, signatures, handwriting.

    pages: up to 5 pages, e.g. "1" (the default) or "2-4". max_side_px (200-2000,
    default 1568) is the long side of each image. Nothing is written to disk.
    """
    src = require_input(input_path, SETTINGS)
    result, images = engine.view_pages(src, SETTINGS, pages, max_side_px, password)
    # Images can't live in structured content, so return a full result: the JSON
    # summary as text (as for every tool), then one image per page.
    return CallToolResult(
        content=[
            TextContent(type="text", text=json.dumps(result, indent=2)),
            *(ImageContent(type="image", data=base64.b64encode(png).decode("ascii"),
                           mimeType="image/png") for png in images),
        ],
        structuredContent=result,
    )


@mcp.tool(annotations=_reads("Compare PDFs"))
@_guard
def compare_pdfs(
    path_a: str,
    path_b: str,
    mode: str = "text",
    ignore_case: bool = False,
    password_a: str | None = None,
    password_b: str | None = None,
) -> CompareResult:
    """Compare two versions of a PDF (A, the older, and B, the newer) page by page.

    Pages are matched first, by their text, so a page inserted or deleted (or page-number
    footers shifting) doesn't make every later page look changed; reordered pages show
    as removed and added. mode "text" compares each matched page's text and lists the
    lines removed and added (lines shown as written; up to 50 per page and 50,000
    characters in all); "visual" matches pages by position and compares how they look,
    in colour (the % of pixels that differ), returning images of the most changed pages
    with what was removed in red, added in green and recoloured in blue; "both" matches
    by text and does both. Pages with no text (e.g. scans) can only be compared
    visually; in text mode they are "unknown". Whitespace and Unicode forms are ignored;
    ignore_case ignores case too. complete is false if the 60 s limit stopped some pages
    being compared. Needs the owner password of a PDF that forbids copying, except in
    visual mode. Nothing is written to disk.
    """
    a, b = require_input(path_a, SETTINGS), require_input(path_b, SETTINGS)
    result, images = engine.compare_pdfs(a, b, SETTINGS, mode, ignore_case, password_a,
                                         password_b)
    if not images:
        return result
    return CallToolResult(
        content=[
            TextContent(type="text", text=json.dumps(result, indent=2)),
            *(ImageContent(type="image", data=base64.b64encode(png).decode("ascii"),
                           mimeType="image/png") for png in images),
        ],
        structuredContent=result,
    )


@mcp.tool(annotations=_reads("Find blank pages"))
@_guard
def find_blank_pages(
    input_path: str,
    pages: str | None = None,
    ink_threshold_percent: float = 0.005,
    password: str | None = None,
) -> BlankPagesResult:
    """Find blank pages (no text, no ink) and nearly blank ones (no text, a little ink).

    blank_pages can be passed to delete_pages. nearly_blank_pages may hold scanner dust
    but also a stamp, initials or faint pencil: look at them with view_pages before
    deleting. ink_threshold_percent (0-5, default 0.005) is how much of a page may
    differ from its background and still be blank. If complete is false, the time
    limit stopped the scan after pages_checked pages; call again for the rest.
    """
    src = require_input(input_path, SETTINGS)
    return engine.find_blank_pages(src, SETTINGS, pages, ink_threshold_percent, password)


@mcp.tool(annotations=_writes("Render pages to images"))
@_guard
def render_pages(
    input_path: str,
    pages: str | None = None,
    dpi: int = 150,
    format: str = "png",
    output_dir: str | None = None,
    password: str | None = None,
    overwrite: bool | None = None,
) -> RenderResult:
    """Save pages as PNG or JPEG image files, one per page (all pages if pages omitted).

    dpi: 36-600 (default 150). format: "png" (default) or "jpeg". Files are named
    <name>_p001.png in output_dir (default: a <name>_pages folder next to the PDF).
    Copy-protected PDFs need their owner password.
    """
    src = require_input(input_path, SETTINGS)
    # The default folder goes through resolve() too: a symlinked <name>_pages must not
    # lead outside the root.
    out_dir = (
        prepare_output_dir(output_dir, SETTINGS)
        if output_dir
        else resolve(str(src.parent / f"{src.stem}_pages"), SETTINGS)
    )
    return engine.render_pages(src, out_dir, SETTINGS, pages, dpi, format, password,
                               _overwrite(overwrite))


# --------------------------------------------------------------------------- #
# Pipelines
# --------------------------------------------------------------------------- #

PIPELINE_MAX_STEPS = 20
PIPELINE_TIME_LIMIT_S = 300  # checked between steps; each step has its own limits
# Arguments a pipeline supplies itself: the file each step reads and writes, and the
# password that opens it.
_PIPELINE_OWN = {"input_path", "output_path", "password", "overwrite", "current_password"}


def _step_runners() -> dict:
    """Each tool a pipeline can run: (the tool, a call of its engine function on one
    file). Each takes one PDF and writes one PDF, so steps chain."""
    S = SETTINGS
    return {
        "rotate_pages": lambda s, o, a, pw: engine.rotate_pages(s, o, a["degrees"], S,
                                                                a["pages"], pw),
        "delete_pages": lambda s, o, a, pw: engine.delete_pages(s, o, a["pages"], S, pw),
        "extract_pages": lambda s, o, a, pw: engine.extract_pages(s, o, a["pages"], S, pw),
        "rearrange_pages": lambda s, o, a, pw: engine.rearrange_pages(
            s, o, a["page_order"], S, a["append_unlisted"], pw),
        "decrypt_pdf": lambda s, o, a, pw: engine.decrypt_pdf(s, o, pw or "", S),
        "encrypt_pdf": lambda s, o, a, pw: engine.encrypt_pdf(
            s, o, a["user_password"], S, a["owner_password"], a["algorithm"],
            a["allow_print"], a["allow_copy"], a["allow_modify"], pw),
        "optimize_pdf": lambda s, o, a, pw: engine.optimize_pdf(s, o, S, a["linearize"],
                                                                a["compress"], pw),
        "repair_pdf": lambda s, o, a, pw: engine.repair_pdf(s, o, S, pw),
        "flatten_pdf": lambda s, o, a, pw: engine.flatten_pdf(s, o, S, pw),
        "set_metadata": lambda s, o, a, pw: engine.set_metadata(s, o, _fields(a), S, pw),
        "sanitize_pdf": lambda s, o, a, pw: engine.sanitize_pdf(
            s, o, S, a["remove_metadata"], a["remove_scripts"], a["remove_attachments"],
            a["remove_comments"], pw),
        "add_watermark": lambda s, o, a, pw: engine.add_watermark(
            s, o, S, a["text"],
            require_input(a["image_path"], S) if a["image_path"] else None,
            a["position"], a["pages"], a["opacity"], a["rotation"], a["font_size"],
            a["scale"], a["color"], a["layer"], pw),
        "add_page_numbers": lambda s, o, a, pw: engine.add_page_numbers(
            s, o, S, a["format"], a["position"], a["start"], a["pages"], a["font_size"],
            a["margin"], a["color"], pw),
        "fill_form": lambda s, o, a, pw: engine.fill_form(s, o, a["values"], S, pw,
                                                          a["flatten"]),
        "crop_pages": lambda s, o, a, pw: engine.crop_pages(s, o, S, a["margins"], a["unit"],
                                                            a["pages"], a["reset"], pw),
        "resize_pages": lambda s, o, a, pw: engine.resize_pages(
            s, o, S, a["size"], a["width"], a["height"], a["unit"], a["orientation"],
            a["pages"], pw),
        "n_up": lambda s, o, a, pw: engine.n_up(s, o, S, a["per_sheet"], a["size"],
                                                a["pages"], pw),
        "insert_blank_pages": lambda s, o, a, pw: engine.insert_blank_pages(
            s, o, S, a["after"], a["count"], pw),
        "set_outline": lambda s, o, a, pw: engine.set_outline(s, o, a["entries"], S, pw),
    }


def _fields(args: dict) -> dict:
    """set_metadata's check, as the tool does it."""
    if not args["fields"]:
        raise PdfUserError("Provide at least one metadata field to set")
    return args["fields"]


@functools.cache
def _step_model(tool: str):
    """Validation for a step's arguments, made from the tool's own parameters (types and
    defaults), less what the pipeline supplies; unknown arguments are refused."""
    import inspect

    from pydantic import ConfigDict, create_model

    params = inspect.signature(globals()[tool]).parameters
    fields = {name: (p.annotation, ... if p.default is inspect.Parameter.empty else p.default)
              for name, p in params.items() if name not in _PIPELINE_OWN}
    return create_model(f"{tool}_step", __config__=ConfigDict(extra="forbid"), **fields)


def _step_args(n: int, step, runners: dict) -> tuple[str, dict]:
    from pydantic import ValidationError

    if not isinstance(step, dict) or not isinstance(step.get("tool"), str):
        raise ToolError(f"Step {n} must be an object with a tool name")
    extra = sorted(set(step) - {"tool", "args"})
    if extra:  # e.g. "arguments" instead of "args": the step would run with defaults
        raise ToolError(f"Step {n}: unknown key {', '.join(map(repr, extra))}; a step has "
                        "\"tool\" and \"args\"")
    tool = step["tool"]
    if tool not in runners:
        raise ToolError(f"Step {n}: {tool!r} can't run in a pipeline. Steps can be: "
                        f"{', '.join(sorted(runners))}")
    args = step.get("args") or {}
    if not isinstance(args, dict):
        raise ToolError(f"Step {n} ({tool}): args must be an object")
    try:
        return tool, _step_model(tool)(**args).model_dump()
    except ValidationError as exc:
        problems = "; ".join(f"{'.'.join(map(str, e['loc'])) or 'args'}: {e['msg']}"
                             for e in exc.errors()[:5])
        raise ToolError(f"Step {n} ({tool}): {problems}") from None


def _publish(made, out, overwrite: bool) -> None:
    """Move the finished file into place: copied beside it, then renamed over it, so
    the output is never half-written and never written through a link. A file it
    replaces keeps its permissions; read-only files, links and anything that isn't a
    regular file are refused, as is a file that appeared during the run unless
    overwriting."""
    import shutil
    import stat
    import tempfile

    mode = None
    if os.path.lexists(out):
        if out.is_symlink() or not out.is_file():
            raise PdfUserError(f"{out} exists and is not a regular file; choose another "
                               "output_path")
        if not overwrite:
            raise PdfUserError(f"{out} already exists (pass overwrite=true to replace it)")
        if not os.access(out, os.W_OK):
            raise PdfUserError(f"{out} is read-only")
        mode = stat.S_IMODE(os.stat(out).st_mode)
    fd, tmp = tempfile.mkstemp(dir=out.parent, prefix=f".{out.stem}.", suffix=out.suffix)
    os.close(fd)
    try:
        shutil.copyfile(made, tmp)
        if mode is None:
            shutil.copymode(made, tmp)  # a new file: the usual mode, not mkstemp's private one
        else:
            os.chmod(tmp, mode)
        os.replace(tmp, out)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


@mcp.tool(annotations=_writes("Run pipeline"))
@_guard
def run_pipeline(
    input_path: str,
    steps: Annotated[list[PipelineStepInput], Field(min_length=1, max_length=20)],
    output_path: str | None = None,
    password: str | None = None,
    overwrite: bool | None = None,
) -> PipelineResult:
    """Run several tools on a PDF in one call, each on the result of the one before,
    and save only the final result (default: "<name>_processed.pdf"). Each step is
    {"tool": name, "args": {...}} with the same arguments as that tool, without
    input_path, output_path, password or overwrite, e.g.
    [{"tool": "decrypt_pdf"}, {"tool": "delete_pages", "args": {"pages": "1"}},
     {"tool": "optimize_pdf"}, {"tool": "encrypt_pdf", "args": {"user_password": "x"}}].
    Steps can be the tools that take one PDF and write one PDF: rotate_pages,
    delete_pages, extract_pages, rearrange_pages, decrypt_pdf, encrypt_pdf (last step
    only), optimize_pdf, repair_pdf, flatten_pdf, set_metadata, sanitize_pdf,
    add_watermark, add_page_numbers, fill_form, crop_pages, resize_pages, n_up,
    insert_blank_pages, set_outline; up to 20. password opens the input and is used by
    every step (decrypt_pdf removes it). Every step's arguments are checked before any
    step runs, and each step applies its tool's own rules. Steps hand files to each other
    in a private temporary folder, deleted afterwards; if a step fails, nothing is saved.
    """
    import tempfile

    src = require_input(input_path, SETTINGS)
    if not isinstance(steps, list) or not 1 <= len(steps) <= PIPELINE_MAX_STEPS:
        raise ToolError(f"Give 1 to {PIPELINE_MAX_STEPS} steps")
    runners = _step_runners()
    plan = [_step_args(n, step, runners) for n, step in enumerate(steps, 1)]
    for n, (tool, _) in enumerate(plan, 1):
        if tool == "encrypt_pdf" and n != len(plan):
            # Later steps would open it with the new password and re-encrypt it, and
            # the owner password given here would be lost.
            raise ToolError(f"Step {n}: encrypt_pdf must be the last step")
    replace = _overwrite(overwrite)
    out = prepare_output(src, output_path, "_processed", replace, SETTINGS)
    with open_pdf(src, SETTINGS, password) as first:
        known_user = known_user_password(first, password)
    done, current, pw, result, changed = [], src, password, {}, False
    deadline = time.monotonic() + PIPELINE_TIME_LIMIT_S
    token = KNOWN_USER_PASSWORD.set(known_user)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            for n, (tool, args) in enumerate(plan, 1):
                if n > 1 and time.monotonic() > deadline:
                    raise PdfUserError(f"The pipeline took over {PIPELINE_TIME_LIMIT_S} s; "
                                       f"stopped before step {n} ({tool}), nothing saved")
                made = Path(tmp) / f"step{n:02d}.pdf"
                try:
                    result = runners[tool](current, made, args, pw)
                except PdfUserError as exc:
                    # The file a step read may be a temporary one; don't show where.
                    detail = re.sub(re.escape(tmp) + r"[^\s:'\"]*", "the PDF",
                                    str(exc).replace(str(current), "the PDF"))
                    raise PdfUserError(f"Step {n} ({tool}) failed: {detail}") from exc
                if made.stat().st_size > SETTINGS.max_file_bytes:
                    raise PdfUserError(
                        f"Step {n} ({tool}) made a file over the "
                        f"{SETTINGS.max_file_bytes // 2**20} MB limit; nothing saved")
                done.append({"tool": tool, "result": {k: v for k, v in result.items()
                                                      if k != "output"}})
                changed = changed or bool(result.get("user_password_changed"))
                if current != src:
                    current.unlink()  # the previous step's file is no longer needed
                current = made
                if tool == "decrypt_pdf":
                    pw = None
                    KNOWN_USER_PASSWORD.set(None)
            _publish(current, out, replace)
    finally:
        KNOWN_USER_PASSWORD.reset(token)
    protection = {k: result.get(k, False) for k in
                  ("password_protected", "permissions_restricted")}
    protection["user_password_changed"] = changed
    if plan[-1][0] == "encrypt_pdf":  # it reports what it set up, not inheritance
        protection["password_protected"] = True
    return {"output": str(out), "steps": done, **protection}


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def serve() -> None:
    """Run the MCP server over stdio. Invoked by the CLI's default/`serve` path."""
    logging.basicConfig(
        level=os.environ.get("PAPERBOY_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if SETTINGS.root is None:
        logger.warning(
            "PAPERBOY_ROOT is not set; the server can read/write anywhere the OS user can. "
            "Set PAPERBOY_ROOT to confine it to one directory."
        )
    logger.info("Starting %s %s (root=%s)", SERVER_NAME, __version__, SETTINGS.root)
    mcp.run()


if __name__ == "__main__":
    serve()
