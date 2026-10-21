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
| `PAPERBOY_LOG_LEVEL` | `INFO` | Logging verbosity (logs go to stderr). |

## Security

- **Set `PAPERBOY_ROOT`.** Without it the server can read and write anywhere the OS user can; with it, every path is resolved (symlinks and `..` included) and rejected if it escapes the tree.
- **File-size and page-count caps** keep a malicious or runaway PDF from exhausting memory.
- **Subprocess timeouts** bound every LibreOffice and qpdf call.
- **No silent overwrites** — a tool refuses to clobber an existing file unless you opt in.
- **Passwords are tool arguments**, so they appear in the client's tool-call log. `decrypt_pdf` requires the real password; it does not crack anything.

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
pytest          # engine, path confinement, and a real stdio round-trip
ruff check .
```

Tests that need LibreOffice or the qpdf CLI skip automatically when those binaries are absent.

## Roadmap

- Streamable-HTTP transport with OAuth + per-session file isolation (hosted mode)
- Watermarks, page numbering, and stamping
- PDF → image (page rasterization) and PDF/A conversion
- OCR for scanned documents

## License

MIT — see [LICENSE](LICENSE). Third-party component licenses are listed in [NOTICE.md](NOTICE.md).
