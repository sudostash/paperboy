# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
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
