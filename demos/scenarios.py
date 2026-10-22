"""Demo scenarios: one per feature, each a scripted conversation.

A scenario is the user's prompt plus the steps the assistant takes. ``record.py``
executes ``call`` steps against the real MCP server, so every result shown in a
video is genuine output, never mocked.

Step types:
    {"type": "tools"}                      list the server's tools and their hints
    {"type": "say", "text": ...}           an assistant message
    {"type": "call", "tool": ..., "args": {...},
     "show": {"before": file, "after": file},  call a tool, optionally render PDFs,
     "showSchema": True,                       or show its typed result vs. its schema,
     "showImages": True}                       or the images it returned / files it saved

File names in ``args`` and ``show`` are relative to the scenario workspace,
which is also the server's ``PAPERBOY_ROOT``.
"""
from __future__ import annotations

SCENARIOS: dict[str, dict] = {
    "tool-annotations": {
        "title": "Safe-by-default tool hints",
        "subtitle": "Read-only tools run instantly. Anything that writes a file asks first.",
        "files": ["report.pdf"],
        "prompt": "What can you do with my PDFs without asking me first? "
                  "Then check report.pdf and rotate its last page.",
        "steps": [
            {"type": "tools"},
            {"type": "say", "text": "Four inspection tools are marked read-only, so your "
                                    "client can run them straight away. Every tool that "
                                    "writes a file is marked destructive and asks first."},
            {"type": "call", "tool": "pdf_info", "args": {"input_path": "report.pdf"},
             "show": {"after": "report.pdf"}},
            {"type": "call", "tool": "rotate_pages",
             "args": {"input_path": "report.pdf", "degrees": 90, "pages": "last"},
             "show": {"before": "report.pdf", "after": "report_rotated.pdf"}},
            {"type": "say", "text": "Done. The last page of report.pdf is rotated 90° in "
                                    "report_rotated.pdf, and the original is untouched."},
        ],
    },
    "structured-output": {
        "title": "Typed, predictable results",
        "subtitle": "Every tool publishes an output schema, so assistants read exact fields "
                    "instead of parsing text.",
        "files": ["report.pdf"],
        "prompt": "Check report.pdf, then split it into one file per page.",
        "steps": [
            {"type": "call", "tool": "pdf_info", "args": {"input_path": "report.pdf"},
             "showSchema": True},
            {"type": "say", "text": "report.pdf has 3 pages, is not encrypted, and its first "
                                    "page is 612 × 792 pt (US Letter)."},
            {"type": "call", "tool": "split_pdf", "args": {"input_path": "report.pdf"},
             "showSchema": True},
            {"type": "say", "text": "Split into 3 files in report_split/, report_part001.pdf "
                                    "to report_part003.pdf. None of them needs a password."},
        ],
    },
    "render-pages": {
        "title": "See and export pages as images",
        "subtitle": "The assistant can look at a page, and save pages as PNG or JPEG.",
        "files": ["report.pdf"],
        "prompt": "What's on page 2 of report.pdf? Then save every page as a PNG.",
        "steps": [
            {"type": "call", "tool": "view_pages",
             "args": {"input_path": "report.pdf", "pages": "2"}, "showImages": True},
            {"type": "say", "text": "Page 2 is the Regional Breakdown: North 12,400 deliveries "
                                    "(98.1% on time), South 10,950, East 8,210 and West 9,775, "
                                    "with a bar chart underneath."},
            {"type": "call", "tool": "render_pages", "args": {"input_path": "report.pdf"},
             "showImages": True},
            {"type": "say", "text": "Saved 3 PNGs at 150 dpi in report_pages/: report_p001.png "
                                    "to report_p003.png."},
        ],
    },
}
