"""MCP server: a thin adapter exposing the :mod:`paperboy.engine` as MCP tools.

The server owns policy (path confinement, size/page caps, overwrite rules) and
error presentation; the engine owns PDF behavior. Keeping them separate means a
future streamable-HTTP transport can import the same engine unchanged.
"""
from __future__ import annotations

import functools
import logging
import os
from typing import Optional

from . import SERVER_NAME, __version__, engine
from .config import Settings
from .errors import PdfUserError
from .paths import prepare_output, prepare_output_dir, require_input, resolve

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


def _overwrite(flag: Optional[bool]) -> bool:
    return SETTINGS.default_overwrite if flag is None else flag


def _combined_output(output_path: str, inputs: list, overwrite: Optional[bool]):
    """Resolve the output of a many-to-one tool; it may never replace one of its inputs."""
    out = resolve(output_path, SETTINGS)
    if out in inputs:
        raise ToolError("Output path is the same as an input; choose a different output_path")
    if out.exists() and not _overwrite(overwrite):
        raise ToolError(f"{out} already exists (pass overwrite=true to replace it)")
    out.parent.mkdir(parents=True, exist_ok=True)
    return out


# --------------------------------------------------------------------------- #
# Inspection
# --------------------------------------------------------------------------- #


@mcp.tool()
@_guard
def pdf_info(input_path: str, password: Optional[str] = None) -> dict:
    """Report page count, file size, PDF version, encryption status and metadata.

    Works on an encrypted file without a password (returns is_encrypted only).
    """
    return engine.pdf_info(require_input(input_path, SETTINGS), SETTINGS, password)


@mcp.tool()
@_guard
def get_metadata(input_path: str, password: Optional[str] = None) -> dict:
    """Read document metadata: title, author, subject, keywords, creator, producer."""
    return engine.get_metadata(require_input(input_path, SETTINGS), SETTINGS, password)


@mcp.tool()
@_guard
def check_pdf(input_path: str, password: Optional[str] = None) -> dict:
    """Validate structural integrity and report any problems found."""
    return engine.check_pdf(require_input(input_path, SETTINGS), SETTINGS, password)


@mcp.tool()
@_guard
def extract_text(
    input_path: str,
    pages: Optional[str] = None,
    password: Optional[str] = None,
    max_chars: int = 50000,
) -> dict:
    """Extract text from all pages or a page spec (e.g. "1-3,7"). Returns text + truncation flag."""
    return engine.extract_text(
        require_input(input_path, SETTINGS), SETTINGS, pages, password, max_chars
    )


# --------------------------------------------------------------------------- #
# Security
# --------------------------------------------------------------------------- #


@mcp.tool()
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
) -> dict:
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


@mcp.tool()
@_guard
def decrypt_pdf(
    input_path: str,
    password: str,
    output_path: Optional[str] = None,
    overwrite: Optional[bool] = None,
) -> dict:
    """Remove the password from a PDF, given its correct password. Not a cracker."""
    src = require_input(input_path, SETTINGS)
    out = prepare_output(src, output_path, "_decrypted", _overwrite(overwrite), SETTINGS)
    return engine.decrypt_pdf(src, out, password, SETTINGS)


# --------------------------------------------------------------------------- #
# Pages
# --------------------------------------------------------------------------- #


@mcp.tool()
@_guard
def rearrange_pages(
    input_path: str,
    page_order: str,
    output_path: Optional[str] = None,
    append_unlisted: bool = False,
    password: Optional[str] = None,
    overwrite: Optional[bool] = None,
) -> dict:
    """Reorder/duplicate/reverse pages. page_order e.g. "3,1,2", "5-1" (reverse), "last,1-4".

    Unlisted pages are dropped unless append_unlisted=true. Pages may repeat to duplicate.
    """
    src = require_input(input_path, SETTINGS)
    out = prepare_output(src, output_path, "_reordered", _overwrite(overwrite), SETTINGS)
    return engine.rearrange_pages(src, out, page_order, SETTINGS, append_unlisted, password)


@mcp.tool()
@_guard
def delete_pages(
    input_path: str,
    pages: str,
    output_path: Optional[str] = None,
    password: Optional[str] = None,
    overwrite: Optional[bool] = None,
) -> dict:
    """Delete pages, e.g. pages="2,5-7"."""
    src = require_input(input_path, SETTINGS)
    out = prepare_output(src, output_path, "_trimmed", _overwrite(overwrite), SETTINGS)
    return engine.delete_pages(src, out, pages, SETTINGS, password)


@mcp.tool()
@_guard
def extract_pages(
    input_path: str,
    pages: str,
    output_path: Optional[str] = None,
    password: Optional[str] = None,
    overwrite: Optional[bool] = None,
) -> dict:
    """Save only the given pages as a new PDF, e.g. pages="1-3,10"."""
    src = require_input(input_path, SETTINGS)
    out = prepare_output(src, output_path, "_extract", _overwrite(overwrite), SETTINGS)
    return engine.extract_pages(src, out, pages, SETTINGS, password)


@mcp.tool()
@_guard
def rotate_pages(
    input_path: str,
    degrees: int,
    pages: Optional[str] = None,
    output_path: Optional[str] = None,
    password: Optional[str] = None,
    overwrite: Optional[bool] = None,
) -> dict:
    """Rotate pages clockwise by 90/180/270 degrees (all pages if pages omitted)."""
    src = require_input(input_path, SETTINGS)
    out = prepare_output(src, output_path, "_rotated", _overwrite(overwrite), SETTINGS)
    return engine.rotate_pages(src, out, degrees, SETTINGS, pages, password)


@mcp.tool()
@_guard
def merge_pdfs(
    input_paths: list[str],
    output_path: str,
    passwords: Optional[dict[str, str]] = None,
    overwrite: Optional[bool] = None,
) -> dict:
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


@mcp.tool()
@_guard
def split_pdf(
    input_path: str,
    ranges: Optional[str] = None,
    output_dir: Optional[str] = None,
    password: Optional[str] = None,
    overwrite: Optional[bool] = None,
) -> dict:
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


@mcp.tool()
@_guard
def optimize_pdf(
    input_path: str,
    output_path: Optional[str] = None,
    linearize: bool = True,
    compress: bool = True,
    password: Optional[str] = None,
    overwrite: Optional[bool] = None,
) -> dict:
    """Shrink a PDF: linearize ("fast web view") and recompress object streams."""
    src = require_input(input_path, SETTINGS)
    out = prepare_output(src, output_path, "_optimized", _overwrite(overwrite), SETTINGS)
    return engine.optimize_pdf(src, out, SETTINGS, linearize, compress, password)


@mcp.tool()
@_guard
def repair_pdf(
    input_path: str,
    output_path: Optional[str] = None,
    password: Optional[str] = None,
    overwrite: Optional[bool] = None,
) -> dict:
    """Rebuild a damaged PDF's structure by reading and rewriting it."""
    src = require_input(input_path, SETTINGS)
    out = prepare_output(src, output_path, "_repaired", _overwrite(overwrite), SETTINGS)
    return engine.repair_pdf(src, out, SETTINGS, password)


@mcp.tool()
@_guard
def flatten_pdf(
    input_path: str,
    output_path: Optional[str] = None,
    password: Optional[str] = None,
    overwrite: Optional[bool] = None,
) -> dict:
    """Flatten form fields and annotations into page content (requires the qpdf CLI)."""
    src = require_input(input_path, SETTINGS)
    out = prepare_output(src, output_path, "_flattened", _overwrite(overwrite), SETTINGS)
    return engine.flatten_pdf(src, out, SETTINGS, password)


# --------------------------------------------------------------------------- #
# Metadata write
# --------------------------------------------------------------------------- #


@mcp.tool()
@_guard
def set_metadata(
    input_path: str,
    fields: dict[str, str],
    output_path: Optional[str] = None,
    password: Optional[str] = None,
    overwrite: Optional[bool] = None,
) -> dict:
    """Set metadata. fields keys: title, author, subject, keywords, creator, producer."""
    if not fields:
        raise ToolError("Provide at least one metadata field to set")
    src = require_input(input_path, SETTINGS)
    out = prepare_output(src, output_path, "_meta", _overwrite(overwrite), SETTINGS)
    return engine.set_metadata(src, out, fields, SETTINGS, password)


# --------------------------------------------------------------------------- #
# Creation
# --------------------------------------------------------------------------- #


@mcp.tool()
@_guard
def convert_to_pdf(
    input_path: str,
    output_path: Optional[str] = None,
    overwrite: Optional[bool] = None,
) -> dict:
    """Create a PDF from an image, a text/markdown file, or an Office/HTML document.

    Office/HTML (docx, xlsx, pptx, odt, html, ...) requires LibreOffice installed.
    """
    src = require_input(input_path, SETTINGS)
    out = prepare_output(src, output_path, "", _overwrite(overwrite), SETTINGS)
    return engine.convert_to_pdf(src, out, SETTINGS)


@mcp.tool()
@_guard
def images_to_pdf(
    input_paths: list[str],
    output_path: str,
    overwrite: Optional[bool] = None,
) -> dict:
    """Combine images into a single PDF, one image per page, in order."""
    if not isinstance(input_paths, list) or not input_paths:
        raise ToolError("Provide at least one image path")
    srcs = [require_input(p, SETTINGS) for p in input_paths]
    out = _combined_output(output_path, srcs, overwrite)
    return engine.images_to_pdf(srcs, out, SETTINGS)


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
