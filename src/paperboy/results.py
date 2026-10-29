"""Result shapes returned by the engine and advertised to MCP clients as outputSchema.

Every key is always present (nullable where a value can be missing). The MCP SDK
drops keys a schema doesn't declare and, on mcp 1.x, returns absent optional keys
as null; keeping every key required and explicit avoids both.

``typing_extensions.TypedDict`` is required: pydantic rejects ``typing.TypedDict``
on Python < 3.12.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import ConfigDict
from typing_extensions import NotRequired, TypedDict


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
    images_resampled: int           # pictures whose resolution was lowered
    image_dpi: int | None           # the resolution they were lowered to
    target_size_bytes: int | None   # the size asked for, if one was
    target_met: bool | None         # whether the output is that size or smaller


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


class ScanIndicator(TypedDict):
    code: str                              # e.g. "javascript_on_open", "launch_actions"
    severity: Literal["info", "warning", "high"]
    count: int
    detail: str


class ScannedFile(TypedDict):
    name: str
    size: int | None                       # bytes, when the file records it
    detected_type: str                     # from the file's own bytes, not its name
    executable: bool                       # can run on a computer if opened
    extension_mismatch: bool               # its name hides what it really is


class AntivirusResult(TypedDict):
    available: bool                        # ClamAV installed (and not skipped)
    engine: str | None                     # e.g. "ClamAV 1.4.1"
    database: str | None                   # signature version and date
    infected: bool | None                  # None if it couldn't scan
    findings: list[str]                    # signature names
    error: str | None


class ScanResult(TypedDict):
    path: str
    verdict: Literal["no_active_content_found", "active_content", "suspicious"]
    indicators: list[ScanIndicator]
    embedded_files: list[ScannedFile]
    antivirus: AntivirusResult
    limits: str                            # what a scan can't tell you


class OutlineEntry(TypedDict):
    level: int                             # 1 = top level, 2 = under the one before, ...
    title: str
    page: int | None                       # 1-based target page; None if not a page here
    link: str | None                       # web or email address the bookmark opens
    action: str | None                     # anything else a bookmark does, e.g. "JavaScript"


class OutlineResult(TypedDict):
    path: str
    page_count: int
    entries: list[OutlineEntry]            # in reading order
    truncated: bool                        # some left out: over 10,000, or over 20 levels deep


class Bookmark(TypedDict):
    """A bookmark to write (set_outline input); get_outline's entries fit as they are."""

    title: str
    page: NotRequired[int | None]          # 1-based; none: a heading that goes nowhere
    level: NotRequired[int]                # default 1; at most one deeper than the previous
    link: NotRequired[str | None]          # http(s) or mailto address, instead of a page
    action: NotRequired[str | None]        # accepted from get_outline, not written back


class SetOutlineResult(Protected):
    output: str
    entries: int                           # bookmarks written
    replaced: int                          # bookmarks the input had (up to 10,000)


class AttachmentInfo(TypedDict):
    name: str                              # as stored in the PDF
    description: str | None
    mime_type: str | None                  # as declared, e.g. "application/pdf"
    size: int | None                       # bytes, as declared
    detected_type: str                     # from the file's own bytes, not its name
    executable: bool                       # can run on a computer if opened
    extension_mismatch: bool               # its name hides what it really is
    page: int | None                       # attached to this page; None: to the document


class AttachmentsResult(TypedDict):
    path: str
    attachments: list[AttachmentInfo]
    truncated: bool                        # cut at 1,000 attachments


class ExtractedFile(TypedDict):
    name: str                              # attachment name in the PDF
    file: str                              # where it was saved (a safe file name)
    size: int
    executable: bool


class SkippedAttachment(TypedDict):
    name: str
    reason: str


class ExtractAttachmentsResult(TypedDict):
    output_dir: str
    files: list[ExtractedFile]
    skipped: list[SkippedAttachment]
    truncated: bool                        # only the first 1,000 attachments were read


class ExtractedImage(TypedDict):
    file: str
    page: int                              # first page it appears on (1-based)
    pages: list[int]                       # every page (of those searched) it appears on
    width: int                             # pixels
    height: int
    format: str                            # file type written, e.g. "png", "jpg"


class SkippedImage(TypedDict):
    page: int
    reason: str


class ExtractImagesResult(TypedDict):
    output_dir: str
    images: list[ExtractedImage]
    skipped: list[SkippedImage]
    truncated: bool                        # only the first 2,000 distinct images were read


class SearchMatch(TypedDict):
    page: int                              # 1-based
    text: str                              # the matched text (whitespace collapsed)
    context: str                           # the match with about 60 characters each side
    # [left, bottom, right, top] in PDF points, in the page's own coordinates (before
    # /Rotate, not shifted to the MediaBox origin); one or more per line of the match.
    rects: list[list[float]]
    rects_truncated: bool                  # more than 20 rectangles; the first 20 are given


class SearchResult(TypedDict):
    path: str
    query: str
    pages_searched: int
    complete: bool                         # false: the time limit stopped it early
    match_count: int                       # matches returned
    matches: list[SearchMatch]             # in page order
    truncated: bool                        # stopped at max_results; there are more
    pages_without_text: list[int]          # no extractable text, e.g. scans (first 1,000)
    pages_skipped: list[int]               # too much content to search safely (first 1,000)


class CropResult(Protected):
    output: str
    pages: list[int]                       # 1-based pages cropped (or reset)
    sizes: list[list[float]]               # each one's size as seen afterwards, in points


class ResizeResult(Protected):
    output: str
    pages: list[int]                       # 1-based pages resized
    width: float                           # the paper size, portrait, in points
    height: float
    scales: list[float]                    # how much each page's content was scaled


class NUpResult(Protected):
    output: str
    sheets: int
    pages_placed: int
    sheet_width: float                     # points
    sheet_height: float


class InsertBlankResult(Protected):
    output: str
    pages: int                             # pages in the output
    inserted: list[int]                    # 1-based numbers of the blank pages in the output


class InterleaveResult(Protected):
    output: str
    pages: int
    fronts: int
    backs: int


class PageSkip(TypedDict):
    page: int                              # 1-based
    reason: str


class Table(TypedDict):
    page: int                              # 1-based
    index: int                             # 1-based, on its page
    rows: list[list[str]]                  # cell text; "" for empty or merged cells
    row_count: int
    column_count: int


class TablesResult(TypedDict):
    path: str
    tables: list[Table]
    pages_searched: int
    complete: bool                         # false: the time limit stopped it early
    truncated: bool                        # stopped at max_tables or the cell limit
    pages_skipped: list[PageSkip]          # not read, and why (first 1,000)


class TableFile(TypedDict):
    file: str
    page: int
    index: int
    row_count: int
    column_count: int


class ExportTablesResult(TypedDict):
    output_dir: str
    files: list[TableFile]
    pages_searched: int
    complete: bool
    truncated: bool
    pages_skipped: list[PageSkip]


class PageDifference(TypedDict):
    # changed / added (only in B) / removed (only in A) / unknown (not compared: no text,
    # skipped, or the time limit); pages that are the same aren't listed.
    status: Literal["same", "changed", "added", "removed", "unknown"]
    page_a: int | None                     # 1-based, in the first PDF
    page_b: int | None                     # 1-based, in the second PDF
    similarity: float | None               # 0-1, how alike the pages' text is
    added: list[str]                       # lines only in B (up to 50)
    removed: list[str]                     # lines only in A (up to 50)
    lines_truncated: bool                  # not every changed line is listed
    pixel_change: float | None             # % of pixels that differ (visual modes)


class CompareSkip(TypedDict):
    document: Literal["a", "b"]
    page: int
    reason: str


class DiffImage(TypedDict):
    page_a: int
    page_b: int
    pixel_change: float


class RedactArea(TypedDict):
    """An area of a page to redact (as search_text gives rectangles)."""

    __pydantic_config__ = ConfigDict(extra="forbid")  # type: ignore[misc]

    page: int                              # 1-based
    rect: list[float]                      # [left, bottom, right, top] in PDF points


class RedactMatch(TypedDict):
    page: int                              # 1-based
    source: str                            # the text or pattern that matched
    text: str                              # what it matched (whitespace collapsed)


class RedactedPage(TypedDict):
    page: int                              # 1-based
    areas: int                             # areas redacted on it (each match makes 1+)
    # "edited": the content was rewritten without what was under the areas, and the
    # rest of the page kept as it was; "rasterized": the page was replaced by a picture
    # of itself with the areas painted over (its text can no longer be selected).
    method: Literal["edited", "rasterized"]
    reason: str | None                     # why it was rasterized
    text_removed: int                      # characters
    images_edited: int                     # pictures with pixels blanked (copies)
    images_removed: int                    # pictures wholly under an area
    drawings_removed: int                  # drawings and forms wholly under an area
    annotations_removed: int               # comments, links, form fields touching an area


class RedactResult(Protected):
    output: str
    pages_searched: int                    # pages searched for the texts and patterns
    match_count: int
    matches: list[RedactMatch]             # in page order (first 1,000)
    matches_truncated: bool
    pages: list[RedactedPage]              # every page with something redacted
    pages_without_text: list[int]          # searched pages with no text, e.g. scans
    # Where else in the file the texts or patterns were found and replaced: "document
    # info", "XMP metadata", "bookmarks", "comments", "form fields", "accessibility
    # structure", "other".
    also_redacted_in: list[str]
    alt_text_removed: int                  # structure elements whose alt text described
    #                                        what was redacted
    attachments_not_searched: int          # attached files, which can't be searched
    # Pictures, forms or content redacted on a page that other content (another page,
    # a form) still draws unredacted, so their originals stay in the file: look there.
    originals_kept: int


class CompareResult(TypedDict):
    path_a: str
    path_b: str
    pages_a: int
    pages_b: int
    mode: Literal["text", "visual", "both"]
    identical: bool                        # every page pair compared and the same
    same: int                              # page pairs that are the same
    changed: int
    added: int
    removed: int
    unknown: int
    differences: list[PageDifference]      # every pair that isn't the same (first 500)
    truncated: bool                        # more than 500 differences
    complete: bool                         # false: limits stopped some pages being compared
    pages_skipped: list[CompareSkip]       # too much content to read safely
    images: list[DiffImage]                # the difference images returned, in order


PipelineTool = Literal[
    "rotate_pages", "delete_pages", "extract_pages", "rearrange_pages", "decrypt_pdf",
    "encrypt_pdf", "optimize_pdf", "repair_pdf", "flatten_pdf", "set_metadata",
    "sanitize_pdf", "add_watermark", "add_page_numbers", "fill_form", "crop_pages",
    "resize_pages", "n_up", "insert_blank_pages", "set_outline", "redact_pdf"]


class PipelineStepInput(TypedDict):
    """One step of run_pipeline: a tool and its arguments (as for the tool itself)."""

    # Other keys are refused, not dropped: a misspelled "args" would run the step with
    # its defaults.
    __pydantic_config__ = ConfigDict(extra="forbid")  # type: ignore[misc]

    tool: PipelineTool
    args: NotRequired[dict[str, Any]]


class PipelineStepResult(TypedDict):
    tool: str
    result: dict[str, Any]                 # what the tool reports (without its file paths)


class PipelineResult(Protected):
    output: str
    steps: list[PipelineStepResult]


class PdfFile(TypedDict):
    path: str
    relative_path: str                     # from the root
    size: int                              # bytes
    modified: str                          # ISO 8601, UTC


class PdfListResult(TypedDict):
    root: str
    folder: str
    files: list[PdfFile]                   # sorted by path
    count: int
    # Not every file was seen or listed: past max_results, or the walk's limits (entries,
    # depth, time). A truncated list is the files found so far, sorted.
    truncated: bool
    skipped_folders: int                   # folders that couldn't be read
