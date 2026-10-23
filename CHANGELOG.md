# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- `compare_pdfs` (read-only): compare two versions of a PDF page by page. Pages are
  matched first (anchored on pages identical and unique in both, then by how alike their
  words are), so an inserted or deleted page, or page-number footers, don't make every
  later page look changed; matched pages are compared by text (lines removed and added,
  and how alike they are) and, with `mode="visual"` or `"both"`, by how they look in
  colour (% of pixels that differ), returning images of the most changed pages with
  removals in red, additions in green and recolouring in blue. Whitespace and Unicode
  forms (NFKC) are ignored, case optionally. Every stage is bounded: page content is
  measured first and heavy pages skipped, matching and line differences have work
  budgets, listed lines share a 50,000-character budget, and a 60-second deadline covers
  the whole call (`complete: false` when it hits). Comparing text needs the owner
  password of a PDF that forbids copying; visual comparison doesn't.
- `extract_tables` (read-only) and `export_tables`: find tables with pdfplumber (ruled
  tables, or `strategy="text"` for ones laid out by alignment) and return their rows, or
  save each as a UTF-8 CSV file for Excel. Cells starting with `= + - @` (or their
  full-width forms) get a leading apostrophe so spreadsheets don't run them as formulas;
  plain numbers like `-5` are left alone. pdfplumber reads a plain copy pikepdf writes,
  and runs in a child process with CPU and memory limits that is stopped after 60
  seconds per call (pages read by then still count); pages whose content (each form as
  often as it is drawn) or font data is too large, or whose ruled lines are too dense to
  analyse, are skipped and listed with the reason; results are capped (200 tables,
  50,000 cells). Needs the owner password when the PDF forbids copying.
- Page layout tools. `crop_pages`: crop by margins measured on the page as seen
  (rotation applied; pt, in or mm), or reset; cropping hides content without removing it.
  `resize_pages`: change pages to a paper size (A3, A4, A5, Letter, Legal, Tabloid) or a
  custom one, scaling content to fit, centred, with links, form fields and comments moved
  along; landscape pages get landscape sheets unless an orientation is forced.
  `n_up`: 2 pages per landscape sheet or 4 per portrait sheet, in reading order, as
  viewers show each page (page content only: flatten first to keep comments and filled-in
  fields). `insert_blank_pages`: blank pages after given page numbers,
  sized like the page before. `interleave_pdfs`: fronts and backs of a double-sided
  document scanned single-sided, with the backs reversed by default. Page sizes honour
  `/UserUnit`; annotations shared between pages move once. Resizing and N-up
  need the owner password when the PDF forbids changes; outputs of protected PDFs stay
  protected.
- `search_text` (read-only): find a word or phrase in a PDF (literal text through PDFium's
  search, so a query can't run away; case-insensitive unless `match_case`, optional
  `whole_word`, a page spec, up to 1,000 results). Each match has its page, the matched
  text, a snippet of about 60 characters each side, and its rectangles on the page in PDF
  points (up to 20). The query is normalized (NFKC, whitespace) and control characters
  are refused. Pages with no text (scans) are listed; pages with more than 2 MB of
  content are skipped and listed rather than indexed (a few hundred bytes per character),
  and a call stops after 60 seconds with `complete: false`. Needs the owner password when
  the PDF forbids copying.
- `list_attachments` (read-only), `extract_attachments` and `extract_images`: list the
  files embedded in a PDF (document and page attachments, typed from their bytes) and
  save them, or save its pictures (usually photos as the original JPEG, others as PNG;
  each image once, also from inside forms; by page and minimum size; inline, annotation,
  pattern and Type 3 images are not extracted). Attachment names are made safe as file
  names (NFC, no folders, reserved or invisible characters, bounded length); programs
  and scripts are skipped unless asked for, judged also by the name on disk. Decoding
  is bounded: data that can't be measured, or that is larger than an image's size
  calls for (including predictor and JPEG headers that disagree with it, and masks), is
  skipped, and each call has a total limit. Outputs never replace links, folders or the
  input, nothing is written if any output exists without `overwrite`, the folder is
  only created when something is written, and a failed run removes what it wrote. Both
  need the owner password when the PDF forbids copying.
- `get_outline` (read-only) and `set_outline`: read a PDF's bookmarks as a flat list
  (level, title, 1-based page, plus any link or other action), and replace them from the
  same shape, so an outline can be read, edited and written back (titles, nesting, pages
  and web links are kept; scripts, styles, open state and scroll positions are not).
  Named destinations and go-to actions resolve to pages; damaged or hostile outlines
  (loops, repeats, deep name trees) are read once, within limits (10,000 bookmarks, 20
  levels, reported as truncated). Writing checks every entry first, opens the file with
  the bookmarks panel showing, keeps protected outputs protected, needs the owner
  password when the PDF forbids document assembly, and refuses signed PDFs.
- `scan_pdf` (read-only): grades a PDF as no active content found, active content, or
  suspicious, with graded indicators: scripts (document-level, on open, on page or field
  events, in action chains and media actions), actions that run without a click (web
  requests, form submissions, printing), program-launching and other risky actions, XFA
  (suspicious when it carries scripts), Flash/3D/media, JBIG2, attachments identified by
  their bytes through common encodings (programs, scripts, shortcuts, disk images,
  macro-capable Office files, disguised or double extensions), attached PDFs scanned in turn,
  names disguised with hex escapes, scripts in the file the document doesn't reach, data
  before the header or after the end (polyglots), unusual compression chains, damaged
  structure, and the sites the document links to. Decoding is capped so small files can't
  expand into gigabytes. Optional ClamAV (`PAPERBOY_CLAMSCAN`) adds known-malware
  signatures, also on a decrypted copy of encrypted files (written to a private temporary
  folder and deleted), with its size limits raised and overruns reported rather than
  passed as clean. It never calls a file "safe".
- `inspect_hidden_data` (read-only) and `sanitize_pdf`: find and remove what a PDF carries
  besides its pages: metadata (Info and XMP), JavaScript and risky actions (launch, submit,
  import, remote go-to, ...) including those hidden in action chains and XFA, automatic
  triggers, attachments, comments (optional), application data, thumbnails, earlier saved
  versions (the file is rewritten), unused resources, Reader usage rights, and the document
  ID. Links, destinations, outlines and structure are kept. Signed PDFs are refused.
  Also removes comment authors and dates, and camera data (EXIF, XMP, IPTC) inside JPEG
  images, losslessly. What it reports as removed is measured by re-scanning the result; if
  anything it was asked to remove remains, it refuses and writes nothing. Not covered:
  hidden layers, text hidden by colour or position, and form field values.
- `add_watermark`: text or image watermarks and corner stamps (position, size, colour,
  opacity, rotation, over or under the content). `add_page_numbers`: page or Bates numbers
  from a safe template (`{n}`, `{total}`, `{n:06}`), with a start number and page selection.
  Marks are drawn upright and inside the visible area on rotated pages (including rotation
  inherited from the page tree) and offset crop boxes, and the page's own graphics state
  can't move them. Both need permission to change pages (or the owner password) and keep
  protected PDFs protected. Text is Latin characters only (built-in fonts). Marks stay inside
  the area viewers actually show (a CropBox larger than the page is clipped), survive page
  content that over-restores its graphics state, don't leak into shared resources, and
  auto-sized text fits its rotated box; large images are downscaled and palette PNG
  transparency is kept.
- `list_form_fields` (read-only) and `fill_form`: list a form's fields (full dotted names,
  type, value, options, required/read-only, max length, page) and fill them by name. Every
  value is validated before anything is written; appearances are regenerated so every viewer
  shows the values; optional `flatten`. Honors the PDF's "no form filling" permission, keeps
  protected PDFs protected, refuses digitally signed PDFs, and drops XFA data so viewers use
  the filled fields. Values are withheld (`values_hidden`) when copying is forbidden and the
  owner password wasn't given. Only filled fields are redrawn (others keep their original
  appearance); values the form's font can't draw are reported (`unrenderable_fields`), left
  for viewers to redraw, and never flattened as "?". Any signature in the file (including
  certification signatures) is refused; Adobe Reader usage rights are removed and reported.
  `flatten` also needs permission to modify annotations, and is capped at 2000 widgets.
- `find_blank_pages` (read-only): a page with any text object is content; otherwise it is
  rendered small and measured against its own background (so off-white paper is fine).
  `blank_pages` (at most `ink_threshold_percent`, default 0.005%) can go straight to
  `delete_pages`; `nearly_blank_pages` (up to 0.5% ink: dust, but also a stamp or initials)
  should be looked at with `view_pages` first. Bounded per page and per call (60 s, then
  `complete: false`).
- PDFium now renders form fields, so `view_pages` and `render_pages` show filled-in values
  (they were blank before).
- `view_pages`: returns up to five pages as images (PNG, long side 200-2000 px, default 1568)
  so the assistant can see layouts, scans, charts, and signatures. Read-only.
- `render_pages`: saves pages as PNG or JPEG files at 36-600 dpi. Honors a PDF's copy
  restriction unless the owner password is given; refuses images over 40 megapixels or
  calls over 400 megapixels in total; writes via a temporary file and atomic rename (never
  through symlinks or hard links) and never leaves partial output behind.
- Page rendering uses PDFium via `pypdfium2` (BSD-3-Clause / Apache-2.0), serialized
  behind a lock because PDFium is not thread-safe.
- Structured output: every tool declares an MCP `outputSchema` (from typed result shapes in
  `paperboy.results`) and returns `structuredContent` alongside the JSON text.
- MCP tool annotations on every tool: a human-readable `title`, `readOnlyHint` on the four
  inspection tools, `destructiveHint` on tools that write files, and `openWorldHint: false`
  throughout, so clients can auto-run safe tools and keep confirming writes.
- `demos/`: Playwright harness that records a tutorial video per feature from real tool
  calls, plus a `demo` optional dependency group.
- Tests for every module (60% to 99% branch coverage), a 95% branch-coverage gate in CI,
  and a CI job that runs the suite against mcp 1.x.

### Dependencies
- New: `pdfplumber>=0.11.9` and `pdfminer.six>=20251230` (both MIT). Earlier pdfminer.six
  releases run code from malicious PDFs (CVE-2025-64512, CVE-2025-70559). pdfplumber pins
  an exact pdfminer.six, so a pdfminer.six security fix also needs a pdfplumber release.

### Changed
- Copy-restricted PDFs: `extract_text`, `decrypt_pdf`, and `encrypt_pdf` with
  `current_password` now need the owner password when the PDF forbids copying (as
  `render_pages` already did); the open password is enough when copying is allowed.
  `extract_text` also opens owner-only files (no open password) instead of asking for one.
- `extract_text` reports unreadable files and the page limit with the same wording as every
  other tool ("Could not open PDF", "... page limit").
- `pypdf[crypto]` is no longer required: pypdf only ever reads decrypted copies, so the
  `cryptography` dependency is gone.
- Minimum `mcp` raised to 1.22, the first release where a tool can return images alongside
  schema-checked structured content.
- `pdf_info`'s `metadata` now holds the same friendly fields as `get_metadata` (`title`,
  `author`, `subject`, `keywords`, `creator`, `producer`) instead of raw XMP keys.
- Tool results always contain every field: values that don't apply are `null` (e.g.
  `pdf_info` fields that need a password, `check_pdf`'s `raw` without qpdf), and writer tools
  always report `password_protected`, `permissions_restricted`, and `user_password_changed`.
  Previously these keys were omitted, which made schemas impossible to honour on mcp 1.x.
- Minimum `mcp` raised to 1.10 (first release with structured output) and
  `typing-extensions` >= 4.11 is now declared (mcp 1.10 needs it but doesn't say so).
- Minimum `mcp` version raised from 1.2 to 1.7, the first release with tool annotations.
- `encrypt_pdf` with `allow_copy=false` no longer blocks screen readers (accessibility
  permission is always granted, per PDF 2.0).

### Fixed
- `search_text` counts a form's content once for every time a page draws it, and skips
  pages with forms nested more than 12 deep or drawing themselves (they could make one
  page cost minutes). "Could not open PDF" messages no longer include a memory address.
- `rearrange_pages` refuses orders longer than the page cap ("-,-,-" repeated every page
  without limit). Watermark and page-number tools ignore a malformed CropBox, as viewers do,
  instead of failing with an internal error.
- Reading metadata (`get_metadata`, `pdf_info`) no longer creates empty XMP or Info
  structures in the document being read.
- Images (`convert_to_pdf`, `images_to_pdf`, watermark images) are only opened as PNG, JPEG,
  GIF, BMP, TIFF or WebP; Pillow's other loaders include EPS, which runs Ghostscript.
- Non-ASCII passwords (`café`, `p€ss“`) never worked on RC4/AES-128 files: pikepdf sent them as
  UTF-8, but those schemes store PDFDocEncoding. Every tool now retries with PDFDocEncoding
  (AES-256 still requires the exact UTF-8 password). `extract_text`, `view_pages` and
  `render_pages` had the same problem through pypdf and PDFium; they now receive a copy
  decrypted by pikepdf, so passwords are decided the same way for every tool.
- `prepare_output_dir` (used by `split_pdf`'s `output_dir`) reported an existing regular file
  at that path as an internal error; it is now a clear error.
- `pdf_info` and `get_metadata` reported no metadata for PDFs that store it only in the
  classic Info dictionary (very common); fields now come from XMP when present, otherwise
  from the Info dictionary. An empty XMP value no longer shows up as the string "None".
- Reading AES-encrypted PDFs (`extract_text`, page counts) needs `cryptography`, which was
  only installed by accident through newer mcp releases; it is now declared via
  `pypdf[crypto]`.
- Declared minimum versions that did not work: `pikepdf` 8.0-8.11 cannot encrypt or open
  RC4/AES-128 files, and `reportlab` 4.0.0-4.0.3 require building `pycairo`. Minimums are now
  `pikepdf>=8.12` and `reportlab>=4.0.4`, and CI tests the lowest allowed versions.
- **Security:** rotating, reordering, deleting, extracting, splitting, merging, optimizing,
  repairing, or setting metadata on a password-protected PDF wrote an *unprotected* output.
  Outputs now keep the input's protection (AES-256, same user password and permissions);
  only `decrypt_pdf` removes it. Results report `"password_protected": true`,
  `"permissions_restricted": true`, or `"user_password_changed": true` as appropriate.
  Merging protected inputs with different passwords is refused; `flatten_pdf` outputs are
  re-saved with AES-256 instead of keeping the input's (possibly RC4) encryption.
- `check_pdf` reported qpdf's informational lines ("PDF Version: 1.3", "File is not encrypted")
  as problems on healthy files; only warnings and errors are reported now.
- `check_pdf` returned the supplied password in its output (qpdf echoes it); it is now redacted.
- Passwords given to the qpdf CLI (`check_pdf`, `flatten_pdf`) were visible in the process
  list; they are now sent over stdin.
- qpdf timeouts surfaced as internal errors; they are now clear user errors.
- `flatten_pdf` always failed on password-protected input.
- `repair_pdf` ignored the page-count limit.
- `merge_pdfs` and `images_to_pdf` could overwrite one of their own inputs when
  `overwrite=true`; they now refuse, like every other tool.
- Corrupt and oversized ("decompression bomb") images surfaced as internal errors; they are
  now clear user errors naming the file.
- Text conversion collapsed indentation and runs of spaces (breaking YAML, code, and logs).
- An output could replace its own input through a hard link or a differently-cased name on
  case-insensitive filesystems (macOS, Windows); outputs are now compared by file identity.
- `check_pdf` could return part of a user password that qpdf recovered from the owner
  password, and crashed on non-UTF-8 output; qpdf output is now decoded safely and fully
  redacted. Passwords with line breaks, which qpdf would silently truncate, are rejected.
- Text conversion and `images_to_pdf` had no page-count limit, and images in Pillow's
  "decompression bomb" warning range were converted; both are now refused.
- `flatten_pdf` checked the page limit only after qpdf ran and could leave a partial output
  behind on failure.
- Page specs containing non-ASCII digits (e.g. "²") crashed instead of being rejected.
- With mcp 1.x, unexpected exceptions leaked their internal message to the client; they are
  now logged server-side and replaced with a generic error.
- `paperboy install all`, as documented, was rejected; it now installs into every detected
  client.
- Client configs that were valid JSON but not an MCP config crashed the installer with a
  traceback; they are now reported and left untouched.

## [0.1.0] - 2026-10-12

### Added
- Initial release of `paperboy-mcp`.
- Transport-agnostic engine library (`paperboy.engine`) separate from the MCP layer.
- Inspection tools: `pdf_info`, `get_metadata`, `extract_text`, `check_pdf`.
- Security tools: `encrypt_pdf` (AES-256/AES-128/RC4), `decrypt_pdf`.
- Page tools: `rearrange_pages`, `delete_pages`, `extract_pages`, `rotate_pages`,
  `merge_pdfs`, `split_pdf`.
- Optimization tools: `optimize_pdf` (linearize + compress), `repair_pdf`, `flatten_pdf`.
- Metadata tool: `set_metadata`.
- Creation tools: `convert_to_pdf` (images, text, Office/HTML via LibreOffice), `images_to_pdf`.
- Hardening: path confinement (`PAPERBOY_ROOT`), file-size and page-count caps,
  subprocess timeouts, no-overwrite-by-default, structured user-facing errors.
- stdio transport (default); architecture ready for a streamable-HTTP transport.
- Self-installer CLI: `install` / `uninstall` / `list-clients` / `config`
  subcommands that write MCP client configs (Claude Desktop, Claude Code via
  `claude mcp add`, Cursor, Windsurf) automatically, with backups and idempotency.
