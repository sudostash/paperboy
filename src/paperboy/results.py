"""Result shapes returned by the engine and advertised to MCP clients as outputSchema.

Every key is always present (nullable where a value can be missing). The MCP SDK
drops keys a schema doesn't declare and, on mcp 1.x, returns absent optional keys
as null; keeping every key required and explicit avoids both.

``typing_extensions.TypedDict`` is required: pydantic rejects ``typing.TypedDict``
on Python < 3.12.
"""
from __future__ import annotations

from typing import Literal

from typing_extensions import TypedDict


class Protected(TypedDict):
    """Protection of an output derived from a password-protected input."""

    password_protected: bool       # opening the output needs a password
    permissions_restricted: bool   # opens freely but restricts printing/copying/editing
    user_password_changed: bool    # the input's user password no longer opens it


class InfoResult(TypedDict):
    path: str
    size_bytes: int
    is_encrypted: bool
    note: str | None                       # why details are missing, if they are
    pages: int | None                      # None until the PDF can be opened
    pdf_version: str | None
    linearized: bool | None
    metadata: dict[str, str] | None
    first_page_size_pt: list[float] | None  # [width, height]; None for zero pages
    permissions: dict[str, bool] | None    # only for encrypted files


class CheckResult(TypedDict):
    path: str
    healthy: bool
    checker: Literal["qpdf", "pikepdf"]
    problems: list[str]
    raw: str | None                        # qpdf's full report (passwords redacted)


class TextResult(TypedDict):
    path: str
    pages_read: int
    truncated: bool
    text: str


class MetadataResult(TypedDict):
    path: str
    metadata: dict[str, str]


class EncryptResult(TypedDict):
    output: str
    algorithm: str
    pages: int


class DecryptResult(TypedDict):
    output: str
    pages: int


class RearrangeResult(Protected):
    output: str
    pages: int
    order: list[int]


class DeleteResult(Protected):
    output: str
    pages: int
    deleted: list[int]


class ExtractResult(Protected):
    output: str
    pages: int


class RotateResult(Protected):
    output: str
    rotated_pages: list[int]


class MergeResult(Protected):
    output: str
    pages: int
    sources: int


class SplitResult(Protected):
    output_dir: str
    files: list[str]
    count: int


class OptimizeResult(Protected):
    output: str
    pages: int
    size_before: int
    size_after: int
    saved_bytes: int
    linearized: bool


class RepairResult(Protected):
    output: str
    pages: int


class FlattenResult(Protected):
    output: str
    pages: int
    warnings: bool


class SetMetadataResult(Protected):
    output: str
    updated: list[str]


class ConvertResult(TypedDict):
    output: str
    pages: int
    source_kind: Literal["image", "text", "office"]


class ImagesResult(TypedDict):
    output: str
    pages: int


class RenderResult(TypedDict):
    output_dir: str
    files: list[str]
    pages: list[int]                       # 1-based, in the order rendered
    dpi: int
    format: Literal["png", "jpeg"]


class ViewedPage(TypedDict):
    page: int
    width_px: int
    height_px: int


class ViewResult(TypedDict):
    path: str
    total_pages: int
    pages: list[ViewedPage]                # one image per entry follows in the content


class BlankPage(TypedDict):
    page: int
    ink_percent: float                     # share of pixels differing from the background
    verdict: Literal["blank", "nearly_blank"]


class BlankPagesResult(TypedDict):
    path: str
    total_pages: int
    pages_checked: int
    complete: bool                         # false if the time limit stopped the scan
    blank_pages: list[int]                 # 1-based; safe to pass to delete_pages
    nearly_blank_pages: list[int]          # a little ink: look (view_pages) before deleting
    details: list[BlankPage]               # measurements for both lists
    ink_threshold_percent: float


class FormField(TypedDict):
    name: str                              # fully qualified, e.g. "address.city"
    type: Literal["text", "checkbox", "radio", "choice", "signature", "button", "other"]
    value: str | None                      # checkbox/radio: the state name, e.g. "Yes", "Off"
    options: list[str]                     # values to fill: choices, or "on" states
    option_labels: list[str]               # what the form shows for each option
    required: bool
    read_only: bool
    max_length: int | None
    page: int | None


class FormFieldsResult(TypedDict):
    path: str
    has_form: bool
    values_hidden: bool                    # copying forbidden: values need the owner password
    fields: list[FormField]
    count: int


class FillFormResult(Protected):
    output: str
    filled: list[str]
    flattened: bool
    unrenderable_fields: list[str]         # values the form's font can't draw (shown as ?)
    xfa_removed: bool                      # XFA data dropped so viewers show the filled fields
    usage_rights_removed: bool             # Reader extensions dropped (they break on change)


class WatermarkResult(Protected):
    output: str
    pages: list[int]                       # 1-based pages that were marked


class PageNumbersResult(Protected):
    output: str
    pages: list[int]                       # 1-based pages that were numbered
    first_label: str | None                # e.g. "Page 1 of 12" or "ACME-000041"
    last_label: str | None


class HiddenData(TypedDict):
    metadata_fields: list[str]             # e.g. ["author", "producer", "title"]
    xmp_metadata: bool                     # XMP anywhere: document, pages, images
    comment_authors: int                   # comments/attachments naming who wrote them
    image_metadata: int                    # JPEGs carrying EXIF/XMP/IPTC (camera, GPS)
    javascript: int                        # scripts, incl. chained ones and XFA forms
    risky_actions: int                     # launch a program, submit data, open remote files
    embedded_files: int                    # attachments
    comments: int                          # sticky notes, highlights, ... (with authors)
    private_app_data: int                  # data editors keep for themselves (/PieceInfo)
    thumbnails: int
    previous_revisions: int                # earlier saved versions still in the file
    reader_usage_rights: bool


class HiddenDataResult(TypedDict):
    path: str
    found: HiddenData
    clean: bool


class SanitizeResult(Protected):
    output: str
    removed: HiddenData
    document_id_reset: bool
