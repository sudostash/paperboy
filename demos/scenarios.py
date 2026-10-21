"""Demo scenarios: one per feature, each a scripted conversation.

A scenario is the user's prompt plus the steps the assistant takes. ``record.py``
executes ``call`` steps against the real MCP server, so every result shown in a
video is genuine output, never mocked.

Step types:
    {"type": "tools"}                      list the server's tools and their hints
    {"type": "say", "text": ...}           an assistant message
    {"type": "call", "tool": ..., "args": {...},
     "show": {"before": file, "after": file}}  call a tool, optionally render PDFs

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
}
