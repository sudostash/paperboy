"""paperboy-mcp: a production-ready MCP server for PDF tools.

The :mod:`paperboy.engine` subpackage is a transport-agnostic library: every
operation is a plain function that takes paths and returns a result dict. The
:mod:`paperboy.server` module is a thin MCP adapter over that library, so the
same engine can back a future hosted (streamable-HTTP) transport unchanged.
"""

__version__ = "0.1.0"

# --- Branding (single source of truth; the only place to change on a rename) ---
# DIST_NAME   : the PyPI / distribution name (used in the uvx one-liner)
# CLI_COMMAND : the console-script users type
# SERVER_NAME : the MCP server id advertised to clients and used as the default
#               config key when installing
DIST_NAME = "paperboy-mcp"
CLI_COMMAND = "paperboy"
SERVER_NAME = "paperboy"
DISPLAY_NAME = "Paperboy"
