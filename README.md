<div align="center">

# 📰 Paperboy

**A production-ready [Model Context Protocol](https://modelcontextprotocol.io) server for PDF tools.**

Encrypt, decrypt, merge, split, reorder, rotate, optimize, repair, convert, and inspect PDFs — straight from your AI assistant.

Built by [Sudo Stash](https://sudostash.com) · MIT licensed · `pip install paperboy-mcp`

</div>

---

Connect it to Claude Desktop, Claude Code, Cursor, or any MCP client and ask in plain language:
*"Password-protect this contract with AES-256,"* *"merge these three PDFs and drop the blank pages,"*
*"turn this folder of scans into one PDF,"* *"this file won't open — can you repair it?"*

The engine is built on [**pikepdf**](https://github.com/pikepdf/pikepdf) (which wraps **qpdf**), so it handles
real-world, damaged, and encrypted PDFs that pure-Python tools choke on.

## Quickstart

Working in Claude Desktop in under a minute:

```bash
pip install paperboy-mcp                              # 1. install
paperboy install claude-desktop --root ~/Documents    # 2. wire it into Claude, scoped to one folder
```

**3. Fully quit and reopen Claude Desktop** (Cmd/Ctrl+Q — a window reload isn't enough) so it loads the new server.

**4. Put a PDF in `~/Documents` and just ask:**

> *"Encrypt ~/Documents/contract.pdf with the password hunter2 and save it as contract-locked.pdf"*
> *"Merge report-1.pdf and report-2.pdf into one file."*
> *"Rotate page 1 of scan.pdf 90° and drop the last page."*
> *"This file won't open — can you repair it?"*

Output files are written back into your `--root` folder. Remove the server anytime with
`paperboy uninstall claude-desktop`. Using Cursor, Windsurf, or Claude Code instead? See
[Connect it to your AI](#connect-it-to-your-ai).

> **Want to sanity-check without an AI?** With the package installed, drive the engine directly:
> ```python
> from pathlib import Path
> from paperboy.config import Settings
> from paperboy import engine as E
> s = Settings(root=Path.home() / "Documents")
> print(E.pdf_info(Path.home() / "Documents" / "test.pdf", s))
> E.encrypt_pdf(Path.home()/"Documents"/"test.pdf",
>               Path.home()/"Documents"/"test-locked.pdf", "secret", s)
> ```

## Tools

| Tool | What it does |
|---|---|
| `pdf_info` | Page count, size, version, encryption status, metadata |
| `get_metadata` / `set_metadata` | Read / write title, author, subject, keywords, creator, producer |
| `extract_text` | Pull text from all or selected pages |
| `search_text` | Find a word or phrase (literal, optional case and whole-word matching): each match's page, a snippet around it, and its rectangles on the page; lists pages with no text layer *(read-only)* |
| `extract_tables` | Find tables (ruled, or laid out by alignment) and return their rows *(read-only)* |
| `export_tables` | Save each table as a CSV file for Excel, with formula-like cells escaped so a spreadsheet won't run them |
| `compare_pdfs` | Compare two versions page by page: pages matched first (insertions don't shift everything), then the lines removed and added, and optionally how pages look (% of pixels changed, with red/green difference images) *(read-only)* |
| `run_pipeline` | Run several one-file tools in one call (e.g. decrypt → delete pages → optimize → encrypt), each on the result of the last; only the final file is saved, and nothing if a step fails |
| `list_pdfs` | List the PDFs under `PAPERBOY_ROOT` (or a folder in it): path, size, last changed; up to 1,000 *(read-only)* |
| `check_pdf` | Validate structural integrity and report problems |
| `encrypt_pdf` | Add a password — AES-256 (default), AES-128, RC4 — with print/copy/modify restrictions |
| `decrypt_pdf` | Remove a password you know (not a cracker) |
| `rearrange_pages` | Reorder, reverse, or duplicate pages |
| `delete_pages` / `extract_pages` | Remove or pull out pages |
| `rotate_pages` | Rotate all or selected pages 90/180/270° |
| `crop_pages` | Crop pages by margins (top, right, bottom, left as seen; pt, in or mm), or reset the crop. Hides, doesn't remove, content |
| `resize_pages` | Change pages to A3/A4/A5/Letter/Legal/Tabloid or a custom size, scaling content to fit; links, fields and comments move with it |
| `n_up` | 2 pages per landscape sheet or 4 per portrait sheet, in reading order, for handouts and printing; places page content only (flatten first to keep comments and filled-in fields) |
| `insert_blank_pages` | Insert blank pages after given page numbers (0 = before the first), sized like their neighbour |
| `interleave_pdfs` | Combine fronts and backs from a single-sided scanner into one PDF (backs reversed by default) |
| `merge_pdfs` | Combine PDFs in order (per-file passwords supported) |
| `split_pdf` | Split into ranges or one file per page |
| `optimize_pdf` | Linearize ("fast web view") and recompress to shrink; optionally lower picture resolution to a DPI (`max_image_dpi`) or step it down until the file fits a size (`target_size_mb`, keeping the smallest result and reporting `target_met`) |
| `repair_pdf` | Rebuild a damaged file's structure |
| `flatten_pdf` | Flatten form fields and annotations *(needs qpdf CLI)* |
| `convert_to_pdf` | Image / text / Office / HTML → PDF *(Office needs LibreOffice)* |
| `images_to_pdf` | Combine images into one PDF |
| `view_pages` | Show up to 5 pages to the assistant as images, so it can see layouts, scans, charts, and signatures *(read-only)* |
| `find_blank_pages` | Find blank pages (no text, no ink) to pass to `delete_pages`, and *nearly* blank ones (dust, a stamp, initials) to look at with `view_pages` first *(read-only)* |
| `list_form_fields` | List a form's fields: full name, type, value, allowed options, required/read-only *(read-only)* |
| `fill_form` | Fill in form fields by name (text, checkboxes, radio buttons, drop-downs); every value is checked first, appearances are regenerated, optional flatten. Signed PDFs are refused |
| `get_outline` | List the bookmarks (outline) in reading order: level, title, target page, and any link or other action *(read-only)* |
| `set_outline` | Replace the bookmarks from a list of title, page and level (nesting); an empty list removes them. Refuses signed PDFs |
| `list_attachments` | List embedded files (document or page attachments): name, description, declared and detected type, and whether it can run on a computer *(read-only)* |
| `extract_attachments` | Save embedded files to a folder under safe names; programs and scripts are skipped unless `allow_executables` |
| `extract_images` | Save the pictures drawn on pages (and in their forms) as image files, usually photos as the original JPEG, once each, optionally by page and minimum size; inline, annotation, pattern and Type 3 images are not extracted |
| `inspect_hidden_data` | Report what a PDF carries besides its pages: metadata (author, software), scripts and risky actions, attachments, comments, application data, earlier saved versions *(read-only)* |
| `scan_pdf` | Risk report for a PDF you didn't make: scripts (especially ones that run on open), actions that run without a click, program-launching actions, attachments that are programs or disguise their type (attached PDFs are scanned too), disguised names, data hidden around the file, and known malware if ClamAV is installed. Verdict: no active content found / active content / suspicious, never "safe" *(read-only)* |
| `sanitize_pdf` | Write a copy safe to share without that hidden data (comments optional); links, bookmarks and accessibility structure are kept |
| `redact_pdf` | Redact for real: text (words, phrases or regular expressions such as email addresses) and areas of pages (a signature, a photo) are removed from the file, not just covered. Characters are taken out of the page, pictures have their pixels blanked, drawings and annotations there go, and the same text is replaced in metadata, bookmarks, comments and form values. Every page is checked afterwards; one that can't be redacted exactly becomes a picture of itself |
| `add_watermark` | Text ("CONFIDENTIAL", "DRAFT") or image watermark, or a corner stamp ("APPROVED"): position, size, colour, opacity, rotation, over or under the content |
| `add_page_numbers` | Page numbers ("Page {n} of {total}") or Bates numbers ("ACME-{n:06}"), any corner or edge, with a start number and page selection |
| `render_pages` | Save pages as PNG or JPEG files at 36-600 dpi (copy-protected PDFs need the owner password) |

**Typed results.** Every tool publishes an MCP `outputSchema` and returns `structuredContent`,
so assistants read exact fields (`pages`, `password_protected`, `files`...) instead of parsing
text. Every field is always present; a value that doesn't apply is `null` (for example,
`pdf_info` on an encrypted file without its password returns `"pages": null` and a `note`).

**Page specs** are 1-based and accept ranges, `last`/`end`, open ranges (`4-`), and reversal (`5-1`):
e.g. `"1,3,5-8"`, `"last,1-4"`, `"5-1"`. Existing files are never overwritten unless `overwrite=true`.

### Resources and prompts

- **`paperboy://pdfs`** — a resource listing the PDFs under `PAPERBOY_ROOT` (as `list_pdfs`),
  for clients that let you attach resources.
- **Prompts** your client may offer as shortcuts: `prepare_for_signing`,
  `compress_for_email` (with a size in MB), `share_safely` and `check_untrusted_pdf`. Each
  takes a file path and guides the assistant through the right tools in order.

## Install

Requires Python 3.10+. The quickest path is [`uv`](https://docs.astral.sh/uv/):

```bash
uvx --from paperboy-mcp paperboy          # run without installing
# or
pip install paperboy-mcp
```

From source:

```bash
git clone https://github.com/sudostash/paperboy
cd paperboy
pip install -e ".[dev]"
```

### Optional system binaries

| Binary | Enables | Install |
|---|---|---|
| **LibreOffice** (`soffice`) | `convert_to_pdf` for Office/HTML docs | `apt install libreoffice` / `brew install --cask libreoffice` |
| **qpdf** CLI | `flatten_pdf` and deeper `check_pdf` | `apt install qpdf` / `brew install qpdf` |
| **ClamAV** (`clamscan`) | known-malware signatures in `scan_pdf` (encrypted files are also scanned as a decrypted copy in a private temporary folder, deleted afterwards) | `apt install clamav` / `brew install clamav`, then `freshclam` |

Everything else (encryption, merge, split, optimize, repair, image/text conversion) works with no system binaries — pikepdf ships qpdf in its wheel.

## Connect it to your AI

### One command (recommended)

The CLI writes the config into your client for you — no hand-editing JSON:

```bash
paperboy install claude-desktop --root ~/Documents
paperboy install cursor         --root ~/Documents
paperboy install claude-code    --root ~/Documents   # uses `claude mcp add`
paperboy install all            --root ~/Documents   # every detected client
```

Supported clients: `claude-desktop`, `claude-code`, `cursor`, `windsurf`.
Add `--uvx` to have the client launch the server via `uvx` (no global install needed).
`--root` sets the sandbox directory (see Security). Undo with `paperboy uninstall <client>`.

```bash
paperboy list-clients     # show which clients are detected and where their config lives
paperboy config --root ~/Documents   # print a snippet to paste into any other MCP client
```

If you haven't installed the package globally, run the installer through uv:

```bash
uvx --from paperboy-mcp paperboy install claude-desktop --root ~/Documents --uvx
```

### Manual config

Any MCP client takes the same shape — paste this into its config
(for Claude Desktop, `claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "paperboy": {
      "command": "uvx",
      "args": ["--from", "paperboy-mcp", "paperboy"],
      "env": { "PAPERBOY_ROOT": "/Users/you/Documents" }
    }
  }
}
```

If you installed with `pip`, use `"command": "paperboy"` and drop the `--from` args.
`paperboy config` prints this for you with your own paths filled in.

## Configuration

All configuration is environment variables. Sensible defaults; nothing is required.

| Variable | Default | Purpose |
|---|---|---|
| `PAPERBOY_ROOT` | *(unset)* | **Strongly recommended.** Confine all reads/writes to this directory tree. |
| `PAPERBOY_MAX_FILE_MB` | `200` | Reject inputs larger than this. |
| `PAPERBOY_MAX_PAGES` | `5000` | Reject documents with more pages than this. |
| `PAPERBOY_SOFFICE_TIMEOUT` | `180` | Seconds before a LibreOffice conversion is killed. |
| `PAPERBOY_ALLOW_OVERWRITE` | `false` | Default for the `overwrite` flag on every tool. |
| `PAPERBOY_REQUIRE_PASSWORD_REFERENCES` | `false` | Refuse passwords given in tool arguments; only `"ask"` (typed on Paperboy's local page) is accepted. |
| `PAPERBOY_SOFFICE` | *(auto)* | Path to the `soffice` binary. |
| `PAPERBOY_QPDF` | *(auto)* | Path to the `qpdf` binary. |
| `PAPERBOY_CLAMSCAN` | *(auto)* | Path to ClamAV's `clamscan`, used by `scan_pdf`. |
| `PAPERBOY_CLAMSCAN_TIMEOUT` | `300` | Seconds before a ClamAV scan is stopped. |
| `PAPERBOY_LOG_LEVEL` | `INFO` | Logging verbosity (logs go to stderr). |

## Security

- **Set `PAPERBOY_ROOT`.** Without it the server can read and write anywhere the OS user can; with it, every path is resolved (symlinks and `..` included) and rejected if it escapes the tree.
- **File-size and page-count caps** keep a malicious or runaway PDF from exhausting memory.
- **Subprocess timeouts** bound every LibreOffice and qpdf call.
- **No silent overwrites** — a tool refuses to clobber an existing file unless you opt in.
- **Protected stays protected.** Rotating, reordering, splitting, merging, optimizing,
  repairing, flattening, or editing the metadata of a password-protected PDF produces a
  protected output: AES-256, the same user password, and the same permissions. Only
  `decrypt_pdf` removes a password. Results say `"password_protected": true` (or
  `"permissions_restricted": true` for files that open freely but restrict printing/copying).
  - Supplying the **user** password: the output gets a new random owner password, so its
    restrictions stay enforced.
  - Supplying the **owner** password: older encryption (RC4, AES-128) lets qpdf recover the
    user password, so it is kept. For AES-256 it can't be recovered, so the output opens with
    the password you supplied and the result says `"user_password_changed": true`.
  - **Merging** protected files: they must share one password (otherwise the merge is refused,
    so no input's pages become readable with another input's password), and the output gets
    the strictest permissions of all its inputs.
- **Copy restrictions are enforced.** A protected PDF has a user ("open") password and an
  owner password that lifts its restrictions. When a PDF forbids copying, getting its content
  out (`extract_text`, `search_text`, `compare_pdfs` text, `render_pages`, `extract_attachments`,
  `extract_images`, `extract_tables`, `export_tables`, `decrypt_pdf`, re-encrypting with
  `encrypt_pdf`) needs the **owner** password; the open password is enough when copying is
  allowed. `view_pages` and `compare_pdfs` in visual mode (display) work with the open
  password, and tools whose output keeps the input's protection
  (rotate, merge, split, ...) keep the restriction on their output too.
- **Non-ASCII passwords work with every encryption scheme.** RC4/AES-128 files store passwords
  in PDFDocEncoding and AES-256 in UTF-8; every tool opens files the same way (through pikepdf)
  and tries the right encoding, so `café` or `p€ss` work everywhere.
- **Rendering is bounded.** Each image is capped at 40 megapixels and each `render_pages`
  call at 400 megapixels in total. PDFium calls are serialized (it is not thread-safe), and
  files are written via a temporary file and an atomic rename, never through a link.
- **Listing shows what's in the root.** `list_pdfs` and `paperboy://pdfs` let the assistant
  see the name, size and date of every PDF under `PAPERBOY_ROOT` (not hidden ones) without
  you naming a file; it never reads their contents, follows links out of the root, or walks
  more than 50,000 entries. File names are passed on as data; ones with control characters
  are left out.
- **Pipelines run each step under its tool's own rules.** `run_pipeline` checks every
  step's arguments before running any, applies the file-size cap to each intermediate
  file, and keeps the input's protection (and owner password) through the chain. Steps
  hand files over in a private temporary folder in the system's temp directory, which can
  briefly hold a decrypted copy after a `decrypt_pdf` step; each is removed once used.
- **Extraction is bounded and careful with names.** `extract_attachments` and
  `extract_images` decode with byte budgets and refuse data they can't measure (decompression
  bombs, predictor or JPEG headers that disagree with the image's size); each call has a total
  limit (4x the file-size cap, and 400 megapixels of images). Attachment names become safe
  file names (no folders, reserved or invisible characters); programs and scripts are skipped
  unless asked for; outputs never replace links, folders or the input; and a failed run
  removes what it wrote. Extracted files are not password-protected, even when the PDF is.
- **Lowering picture resolution is conservative.** `optimize_pdf` works out each picture's
  resolution from the largest size the pages draw it at (through forms, and `/UserUnit`), and
  leaves alone any picture it can't account for: one a page or form it couldn't read may draw,
  or one an annotation appearance, pattern or Type 3 glyph may draw. Only 8-bit grey and RGB
  pictures (Flate or JPEG, with or without a soft mask) are re-encoded; palettes, CMYK, masks,
  JPEG 2000 and very progressive JPEGs stay as they are, a shared soft mask is copied rather
  than changed, and a picture is replaced only when that makes it smaller. Pictures pass the
  same checks as `extract_images` before being decoded. Content is measured before it is
  parsed (16 MB and 500,000 tokens per page or form, 64 MB and 1.5 million tokens per call),
  the resources followed are counted, and a call stops trying smaller steps after 3 minutes, keeping the best result so
  far. Attempts are saved in a private folder beside the output, which is replaced only at
  the end, keeping its permissions. Text and vector drawings are never touched, but in scans the text is itself a
  picture and can blur at the lowest steps (72 dpi).
- **Redaction removes, then checks.** `redact_pdf` rewrites each page's content without
  what lies under the areas. A character whose centre is in an area is taken out of its
  string (the space it took is kept, so the rest of the line doesn't move); pictures
  partly covered have those pixels set to zero in a copy (masks too); pictures, forms and
  drawings wholly inside are dropped. Drawings crossing an area are cut exactly where they
  can be: a filled rectangle (a background, a barcode bar) becomes the rectangles around
  the area, and straight lines are cut at its edges (by half the line's width more, so
  their ends don't reach back in). Anything else crossing an area (a signature, lettering
  drawn as shapes or used as a clipping path, a dashed line, a pattern, a shading that can
  hold a picture) can't be cut exactly. Annotations touching
  an area are removed (a form field entirely, value included), as are the page's
  thumbnail, editor data and page metadata, XMP page thumbnails, and replacement or alt
  text (`/ActualText`, `/Alt`) describing what was removed. Texts and patterns are found in
  page text and in what annotations show (every appearance state, up to 10,000), and are also replaced in metadata (XMP is
  dropped if it matches), bookmarks, comments, form values, structure, scripts, rich text
  and XFA data; attachments can't be searched, so the result counts them (`sanitize_pdf`
  removes them). Shared pictures and forms are copied, so other pages keep theirs; then
  every original redaction replaced or removed is cut from the file wherever else it's
  referenced (shared or inherited resources, thumbnails, alternates, structure tree,
  actions, `/CO`), unless content that can be read still draws it (another page or form,
  or an annotation's appearance): those are counted in `originals_kept`, since that
  content shows them unredacted; look there. Pictures, forms and property lists a
  redacted page lists but never draws are removed from its resources. Renamed named
  destinations and other name-tree keys are rebuilt into one sorted list, so links still
  work. PDFium then
  reads every rewritten page again: nothing may remain under an area, and nothing outside
  it may change or move; the texts and patterns are searched for again too. A page that
  fails those checks, or can't be followed exactly (fonts without widths, vertical writing,
  CMaps it can't read, text used as a clipping path, a character mostly under an area,
  Type 3 glyphs reaching under one, JPEG 2000, JBIG2, CCITT or CMYK JPEG pictures, an
  `ActualText` only partly covered, too much content), is replaced by a picture of itself
  with the areas painted over; `rasterize="never"` refuses instead. Limits: text PDFium
  can't read (zero-width or overlapping duplicate glyphs) isn't found by texts or
  patterns, so check the result; a removed word leaves a gap of its width; text in
  pictures (scans) isn't text, so such pages are listed in `pages_without_text` and need
  areas; `matches` lists the text matched. Patterns run in a separate process with a CPU
  limit and a 30-second deadline, so a pattern that backtracks badly can't hang the server
  (memory is limited only on Linux); content is measured before it is parsed (16 MB and 1
  million tokens per page or form, 64 MB per call; fonts 64 MB), and a call stops after 3
  minutes, writing nothing. Signed PDFs are refused; Reader usage rights are dropped.
- **Tool annotations** tell clients what each tool does. The four inspection tools
  (`pdf_info`, `get_metadata`, `check_pdf`, `extract_text`) are marked `readOnlyHint`, so clients
  may run them without a prompt. Every tool that writes a file is marked `destructiveHint`
  (`overwrite=true` can replace an existing file), so clients keep asking first. No tool
  touches the network (`openWorldHint: false`). Hints are advisory; the server-side checks
  above are what enforce safety.
- **Passwords can stay out of the conversation.** Pass `"ask"` as any password argument and
  Paperboy opens a one-time page on `127.0.0.1` where you type the password; it goes straight
  to the server, never into the model's context, the client, or the tool-call log. Clients with
  URL-mode elicitation offer to open the page and the call carries on once you've typed it
  (the MCP spec forbids asking for passwords in a client form). With other clients the tool
  replies with the link; open it, type the password, and run the tool again with the
  `"ask:<ref>"` value from the reply. Each link has an unguessable token, works once, and
  expires after 10 minutes; the page answers only on `127.0.0.1` with the exact `Host`
  (no DNS rebinding) and is never logged. New passwords (`encrypt_pdf`, an encrypt step in
  `run_pipeline`) are typed twice. The file is checked before you're asked, a call asks at
  most 5 passwords, and file names on the page are shortened and stripped of control
  characters. A typed password works only for the tool, argument and file it was asked for, so
  an `"ask:<ref>"` can't be spent elsewhere; with a typed user password, `encrypt_pdf` under the
  older algorithms (AES-128, RC4) refuses an owner password given in the arguments, since those
  schemes let the owner password reveal the user password. Cancelling or declining forgets the
  link. When the tool replies with a link, the model sees that link too, so open only links on
  `127.0.0.1` and check the page names the file you expect. `"ask"` and `"ask:..."` are
  reserved, not passwords. Passwords given directly
  as arguments still work, and do appear in the log; `PAPERBOY_REQUIRE_PASSWORD_REFERENCES=true`
  refuses them. `decrypt_pdf` requires the real password; it does not crack anything.
  Passwords handed to the qpdf CLI go over stdin, never the command line, so other local users
  can't read them from the process list, and `check_pdf` redacts the password qpdf echoes back.
- **Internal errors stay on the server.** Anticipated problems (bad path, wrong password,
  out-of-range page) are returned with a clear message; anything unexpected is logged by the
  server and the client only sees a generic "internal error", on both mcp 1.x and 2.x.
- **Copy protection never blocks screen readers.** `allow_copy=false` stops text extraction but
  always keeps the accessibility permission, as PDF 2.0 requires (RC4-40, the oldest scheme,
  has no separate accessibility bit).

## Architecture

```
paperboy/
├── engine/        ← transport-agnostic library: pure functions, paths in, dict out
├── server.py      ← thin MCP adapter: path confinement, limits, error mapping
├── config.py      ← Settings from environment
├── paths.py       ← root confinement + size caps
├── pagespec.py    ← "1,3,5-8,last" parsing
└── errors.py      ← PdfUserError (shown to caller) vs internal crashes
```

The engine has **no dependency on MCP**. The server is a thin layer that enforces policy and
presents errors. That separation is deliberate: the same engine can back a remote
streamable-HTTP transport (with upload/download + auth) without touching the PDF logic.

## Development

```bash
pip install -e ".[dev]"
pytest          # engine, path confinement, server policy, CLI, installer, real stdio round-trip
pytest --cov=paperboy --cov-branch --cov-report=term-missing   # CI requires >= 95% branch coverage
ruff check .
```

Every module has a test file (`tests/test_<module>.py`). Tests that need the qpdf CLI skip when
it is absent; LibreOffice conversions are tested with a stand-in `soffice` script, so they run
everywhere. CI also runs the suite against mcp 1.x.

Before opening a pull request, run the CI jobs locally (GitHub Actions is currently turned
off for this repository):

```bash
scripts/ci-local.sh                 # lint, Python 3.10-3.13 with the coverage gate, mcp 1.x,
                                    # lowest dependency versions, and the browser demos
scripts/ci-local.sh lint test-3.12  # just some jobs
```

It needs [uv](https://docs.astral.sh/uv/) (which fetches any missing Python version) and
qpdf, and keeps one virtualenv per job in `.ci/`.

### Demo videos

`demos/` records a short tutorial video for each feature. Every scenario runs against the real
server over stdio, then a Playwright-driven viewer replays it (chat on the left, before/after
PDF pages on the right). See [demos/README.md](demos/README.md).

```bash
pip install -e ".[demo]" && playwright install chromium
python demos/record.py            # writes demos/out/videos/<scenario>.webm
```

## Roadmap

- Streamable-HTTP transport with OAuth + per-session file isolation (hosted mode)
- PDF/A conversion
- OCR for scanned documents

## License

MIT — see [LICENSE](LICENSE). Third-party component licenses are listed in [NOTICE.md](NOTICE.md).
