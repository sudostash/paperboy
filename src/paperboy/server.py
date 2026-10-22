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
from typing import Optional

from mcp.types import CallToolResult, ImageContent, TextContent, ToolAnnotations

from . import SERVER_NAME, __version__, engine
from .config import Settings
from .errors import PdfUserError
from .paths import prepare_output, prepare_output_dir, require_input, resolve, same_file
from .results import (
    BlankPagesResult,
    CheckResult,
    ConvertResult,
    DecryptResult,
    DeleteResult,
    EncryptResult,
    ExtractResult,
    FillFormResult,
    FlattenResult,
    FormFieldsResult,
    HiddenDataResult,
    ImagesResult,
    InfoResult,
    MergeResult,
    MetadataResult,
    OptimizeResult,
    PageNumbersResult,
    RearrangeResult,
    RenderResult,
    RepairResult,
    RotateResult,
    SanitizeResult,
    SetMetadataResult,
    SplitResult,
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


def _overwrite(flag: Optional[bool]) -> bool:
    return SETTINGS.default_overwrite if flag is None else flag


def _combined_output(output_path: str, inputs: list, overwrite: Optional[bool]):
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
def pdf_info(input_path: str, password: Optional[str] = None) -> InfoResult:
    """Report page count, file size, PDF version, encryption status and metadata.

    Works on an encrypted file without a password (returns is_encrypted only).
    """
    return engine.pdf_info(require_input(input_path, SETTINGS), SETTINGS, password)


@mcp.tool(annotations=_reads("Get metadata"))
@_guard
def get_metadata(input_path: str, password: Optional[str] = None) -> MetadataResult:
    """Read document metadata: title, author, subject, keywords, creator, producer."""
    return engine.get_metadata(require_input(input_path, SETTINGS), SETTINGS, password)


@mcp.tool(annotations=_reads("Check PDF"))
@_guard
def check_pdf(input_path: str, password: Optional[str] = None) -> CheckResult:
    """Validate structural integrity and report any problems found."""
    return engine.check_pdf(require_input(input_path, SETTINGS), SETTINGS, password)


@mcp.tool(annotations=_reads("Extract text"))
@_guard
def extract_text(
    input_path: str,
    pages: Optional[str] = None,
    password: Optional[str] = None,
    max_chars: int = 50000,
) -> TextResult:
    """Extract text from all pages or a page spec (e.g. "1-3,7"). Returns text + truncation flag."""
    return engine.extract_text(
        require_input(input_path, SETTINGS), SETTINGS, pages, password, max_chars
    )


# --------------------------------------------------------------------------- #
# Security
# --------------------------------------------------------------------------- #


@mcp.tool(annotations=_writes("Encrypt PDF"))
@_guard
def encrypt_pdf(
    input_path: str,
    user_password: str,
    output_path: Optional[str] = None,
    owner_password: Optional[str] = None,
    algorithm: str = "AES-256",
    allow_print: bool = True,
    allow_copy: bool = True,
    allow_modify: bool = True,
    current_password: Optional[str] = None,
    overwrite: Optional[bool] = None,
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
    output_path: Optional[str] = None,
    overwrite: Optional[bool] = None,
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
    output_path: Optional[str] = None,
    append_unlisted: bool = False,
    password: Optional[str] = None,
    overwrite: Optional[bool] = None,
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
    output_path: Optional[str] = None,
    password: Optional[str] = None,
    overwrite: Optional[bool] = None,
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
    output_path: Optional[str] = None,
    password: Optional[str] = None,
    overwrite: Optional[bool] = None,
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
    pages: Optional[str] = None,
    output_path: Optional[str] = None,
    password: Optional[str] = None,
    overwrite: Optional[bool] = None,
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
    passwords: Optional[dict[str, str]] = None,
    overwrite: Optional[bool] = None,
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


@mcp.tool(annotations=_writes("Split PDF"))
@_guard
def split_pdf(
    input_path: str,
    ranges: Optional[str] = None,
    output_dir: Optional[str] = None,
    password: Optional[str] = None,
    overwrite: Optional[bool] = None,
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
    output_path: Optional[str] = None,
    linearize: bool = True,
    compress: bool = True,
    password: Optional[str] = None,
    overwrite: Optional[bool] = None,
) -> OptimizeResult:
    """Shrink a PDF: linearize ("fast web view") and recompress object streams."""
    src = require_input(input_path, SETTINGS)
    out = prepare_output(src, output_path, "_optimized", _overwrite(overwrite), SETTINGS)
    return engine.optimize_pdf(src, out, SETTINGS, linearize, compress, password)


@mcp.tool(annotations=_writes("Repair PDF"))
@_guard
def repair_pdf(
    input_path: str,
    output_path: Optional[str] = None,
    password: Optional[str] = None,
    overwrite: Optional[bool] = None,
) -> RepairResult:
    """Rebuild a damaged PDF's structure by reading and rewriting it."""
    src = require_input(input_path, SETTINGS)
    out = prepare_output(src, output_path, "_repaired", _overwrite(overwrite), SETTINGS)
    return engine.repair_pdf(src, out, SETTINGS, password)


@mcp.tool(annotations=_writes("Flatten PDF"))
@_guard
def flatten_pdf(
    input_path: str,
    output_path: Optional[str] = None,
    password: Optional[str] = None,
    overwrite: Optional[bool] = None,
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
    output_path: Optional[str] = None,
    password: Optional[str] = None,
    overwrite: Optional[bool] = None,
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
    output_path: Optional[str] = None,
    overwrite: Optional[bool] = None,
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
    overwrite: Optional[bool] = None,
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
def list_form_fields(input_path: str, password: Optional[str] = None) -> FormFieldsResult:
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
    output_path: Optional[str] = None,
    flatten: bool = False,
    password: Optional[str] = None,
    overwrite: Optional[bool] = None,
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
# Hidden data
# --------------------------------------------------------------------------- #


@mcp.tool(annotations=_reads("Inspect hidden data"))
@_guard
def inspect_hidden_data(input_path: str, password: Optional[str] = None) -> HiddenDataResult:
    """Report hidden data a PDF carries before sharing it: metadata (author, software),
    scripts and risky actions, attachments, comments, application data, thumbnails,
    and earlier saved versions still inside the file. Changes nothing.
    """
    src = require_input(input_path, SETTINGS)
    return engine.inspect_hidden_data(src, SETTINGS, password)


@mcp.tool(annotations=_writes("Sanitize PDF"))
@_guard
def sanitize_pdf(
    input_path: str,
    output_path: Optional[str] = None,
    remove_metadata: bool = True,
    remove_scripts: bool = True,
    remove_attachments: bool = True,
    remove_comments: bool = False,
    password: Optional[str] = None,
    overwrite: Optional[bool] = None,
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
    text: Optional[str] = None,
    image_path: Optional[str] = None,
    position: str = "center",
    pages: Optional[str] = None,
    opacity: float = 0.2,
    rotation: Optional[float] = None,
    font_size: Optional[float] = None,
    scale: float = 0.5,
    color: str = "gray",
    layer: str = "over",
    output_path: Optional[str] = None,
    password: Optional[str] = None,
    overwrite: Optional[bool] = None,
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
    pages: Optional[str] = None,
    font_size: float = 10,
    margin: float = 36,
    color: str = "black",
    output_path: Optional[str] = None,
    password: Optional[str] = None,
    overwrite: Optional[bool] = None,
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
    pages: Optional[str] = None,
    max_side_px: int = 1568,
    password: Optional[str] = None,
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


@mcp.tool(annotations=_reads("Find blank pages"))
@_guard
def find_blank_pages(
    input_path: str,
    pages: Optional[str] = None,
    ink_threshold_percent: float = 0.005,
    password: Optional[str] = None,
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
    pages: Optional[str] = None,
    dpi: int = 150,
    format: str = "png",
    output_dir: Optional[str] = None,
    password: Optional[str] = None,
    overwrite: Optional[bool] = None,
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
