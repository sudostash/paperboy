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
| `check_pdf` | Validate structural integrity and report problems |
| `encrypt_pdf` | Add a password — AES-256 (default), AES-128, RC4 — with print/copy/modify restrictions |
| `decrypt_pdf` | Remove a password you know (not a cracker) |
| `rearrange_pages` | Reorder, reverse, or duplicate pages |
| `delete_pages` / `extract_pages` | Remove or pull out pages |
| `rotate_pages` | Rotate all or selected pages 90/180/270° |
| `merge_pdfs` | Combine PDFs in order (per-file passwords supported) |
| `split_pdf` | Split into ranges or one file per page |
| `optimize_pdf` | Linearize ("fast web view") and recompress to shrink |
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
| `inspect_hidden_data` | Report what a PDF carries besides its pages: metadata (author, software), scripts and risky actions, attachments, comments, application data, earlier saved versions *(read-only)* |
| `scan_pdf` | Risk report for a PDF you didn't make: scripts (especially ones that run on open), actions that run without a click, program-launching actions, attachments that are programs or disguise their type (attached PDFs are scanned too), disguised names, data hidden around the file, and known malware if ClamAV is installed. Verdict: no active content found / active content / suspicious, never "safe" *(read-only)* |
| `sanitize_pdf` | Write a copy safe to share without that hidden data (comments optional); links, bookmarks and accessibility structure are kept |
| `add_watermark` | Text ("CONFIDENTIAL", "DRAFT") or image watermark, or a corner stamp ("APPROVED"): position, size, colour, opacity, rotation, over or under the content |
| `add_page_numbers` | Page numbers ("Page {n} of {total}") or Bates numbers ("ACME-{n:06}"), any corner or edge, with a start number and page selection |
| `render_pages` | Save pages as PNG or JPEG files at 36-600 dpi (copy-protected PDFs need the owner password) |

**Typed results.** Every tool publishes an MCP `outputSchema` and returns `structuredContent`,
so assistants read exact fields (`pages`, `password_protected`, `files`...) instead of parsing
text. Every field is always present; a value that doesn't apply is `null` (for example,
`pdf_info` on an encrypted file without its password returns `"pages": null` and a `note`).

**Page specs** are 1-based and accept ranges, `last`/`end`, open ranges (`4-`), and reversal (`5-1`):
e.g. `"1,3,5-8"`, `"last,1-4"`, `"5-1"`. Existing files are never overwritten unless `overwrite=true`.

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
  out (`extract_text`, `render_pages`, `decrypt_pdf`, re-encrypting with `encrypt_pdf`) needs
  the **owner** password; the open password is enough when copying is allowed. `view_pages`
  (display) works with the open password, and tools whose output keeps the input's protection
  (rotate, merge, split, ...) keep the restriction on their output too.
- **Non-ASCII passwords work with every encryption scheme.** RC4/AES-128 files store passwords
  in PDFDocEncoding and AES-256 in UTF-8; every tool opens files the same way (through pikepdf)
  and tries the right encoding, so `café` or `p€ss` work everywhere.
- **Rendering is bounded.** Each image is capped at 40 megapixels and each `render_pages`
  call at 400 megapixels in total. PDFium calls are serialized (it is not thread-safe), and
  files are written via a temporary file and an atomic rename, never through a link.
- **Tool annotations** tell clients what each tool does. The four inspection tools
  (`pdf_info`, `get_metadata`, `check_pdf`, `extract_text`) are marked `readOnlyHint`, so clients
  may run them without a prompt. Every tool that writes a file is marked `destructiveHint`
  (`overwrite=true` can replace an existing file), so clients keep asking first. No tool
  touches the network (`openWorldHint: false`). Hints are advisory; the server-side checks
  above are what enforce safety.
- **Passwords are tool arguments**, so they appear in the client's tool-call log. `decrypt_pdf` requires the real password; it does not crack anything.
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
