"""Error types shared by the engine and the server layer."""
from __future__ import annotations


class PdfError(Exception):
    """Base class for all engine errors."""


class PdfUserError(PdfError):
    """An anticipated failure whose message is safe to show the caller.

    Bad paths, wrong passwords, out-of-range pages, unsupported file types, and
    exceeded limits all raise this. The server maps it to an MCP tool error with
    the message intact. Any *other* exception is treated as an internal crash and
    the message is withheld.
    """


class DependencyError(PdfUserError):
    """A required optional binary (LibreOffice, qpdf CLI) is not available."""
