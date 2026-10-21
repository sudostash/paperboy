# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
