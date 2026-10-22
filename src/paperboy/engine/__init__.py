"""Transport-agnostic PDF engine.

Every function here takes resolved ``pathlib.Path`` inputs and a
:class:`paperboy.config.Settings`, returns a plain ``dict``, and raises
:class:`paperboy.errors.PdfUserError` for anticipated failures. The MCP server
is a thin adapter over these; a future HTTP transport can reuse them as-is.
"""
from __future__ import annotations

from .create import convert_to_pdf, images_to_pdf
from .forms import fill_form, list_form_fields
from .info import check_pdf, pdf_info
from .metadata import get_metadata, set_metadata
from .optimize import flatten_pdf, optimize_pdf, repair_pdf
from .pages import (
    delete_pages,
    extract_pages,
    merge_pdfs,
    rearrange_pages,
    rotate_pages,
    split_pdf,
)
from .render import find_blank_pages, render_pages, view_pages
from .sanitize import inspect_hidden_data, sanitize_pdf
from .scan import scan_pdf
from .security import decrypt_pdf, encrypt_pdf
from .stamp import add_page_numbers, add_watermark
from .text import extract_text

__all__ = [
    "pdf_info", "check_pdf", "get_metadata", "set_metadata", "extract_text",
    "encrypt_pdf", "decrypt_pdf",
    "rearrange_pages", "delete_pages", "extract_pages", "rotate_pages",
    "merge_pdfs", "split_pdf",
    "optimize_pdf", "repair_pdf", "flatten_pdf",
    "convert_to_pdf", "images_to_pdf",
    "render_pages", "view_pages", "find_blank_pages",
    "list_form_fields", "fill_form",
    "add_watermark", "add_page_numbers",
    "inspect_hidden_data", "sanitize_pdf", "scan_pdf",
]
