"""One PDF's overview for a folder report: what pdf_info, check_pdf, scan_pdf,
inspect_hidden_data and list_form_fields each say about it, in brief."""
from __future__ import annotations

import logging
from pathlib import Path

from ..config import Settings
from ..errors import PdfUserError
from ..results import InspectedPdf
from .forms import list_form_fields
from .info import check_pdf, pdf_info
from .sanitize import inspect_hidden_data
from .scan import scan_pdf

MAX_PROBLEMS = 5       # a file's first problems; check_pdf lists them all
MAX_TEXT = 300         # characters kept of a problem, an error or a title

logger = logging.getLogger("paperboy")


def _short(text: str) -> str:
    text = "".join(c if c.isprintable() else " " for c in str(text)).strip()
    return text if len(text) <= MAX_TEXT else text[:MAX_TEXT - 3] + "..."


def blank(path: Path, status: str, error: str | None = None) -> InspectedPdf:
    """A file's entry before (or without) anything known about it."""
    return {"path": str(path), "status": status, "error": error and _short(error),
            "size_bytes": None,
            "pages": None, "pdf_version": None, "title": None, "encrypted": None,
            "healthy": None, "problems": [], "verdict": None, "risks": [],
            "hidden_data": [], "form_fields": None, "signature_fields": None,
            "attachments": None, "antivirus": None}


def _antivirus(av: dict) -> str:
    """How ClamAV's look at a file went, in a word; never silently "clean"."""
    if not av["available"]:
        return "unavailable"
    return {True: "infected", False: "clean"}.get(av["infected"], "error")


def survey_pdf(path: Path, settings: Settings, password: str | None = None,
               antivirus: bool = False) -> InspectedPdf:
    """What a PDF is and what to look at in it. *password* is tried only on a file that
    needs one to open, so one password can serve a folder of mixed files. A file that
    can't be read is reported (status failed, with what was learnt first), not raised."""
    entry = blank(path, "inspected")
    try:
        info = pdf_info(path, settings)
        entry.update(size_bytes=info["size_bytes"], encrypted=info["is_encrypted"])
        if info["pages"] is None:  # it needs a password to open
            if not password:
                entry.update(status="needs_password",
                             error="It's password protected; pass its password")
                return entry
            try:
                info = pdf_info(path, settings, password)
            except PdfUserError as exc:
                if str(exc) != "Incorrect password":  # it opens, but can't be read
                    raise
                entry.update(status="needs_password",
                             error="The password given doesn't open it")
                return entry
        else:
            password = None
        title = (info["metadata"] or {}).get("title")
        entry.update(pages=info["pages"], pdf_version=info["pdf_version"],
                     title=_short(title) if title else None)
        check = check_pdf(path, settings, password)
        # qpdf names the file in each problem; its name is enough beside its path.
        entry.update(healthy=check["healthy"], problems=[
            _short(p.replace(str(path), path.name)) for p in check["problems"][:MAX_PROBLEMS]])
        scan = scan_pdf(path, settings, password, antivirus)
        entry.update(verdict=scan["verdict"], risks=[i["code"] for i in scan["indicators"]],
                     attachments=len(scan["embedded_files"]),
                     antivirus=_antivirus(scan["antivirus"]) if antivirus else None)
        found = inspect_hidden_data(path, settings, password)["found"]
        entry["hidden_data"] = [kind for kind, value in found.items() if value]
        fields = list_form_fields(path, settings, password)["fields"]
        entry.update(form_fields=len(fields),
                     signature_fields=sum(f["type"] == "signature" for f in fields))
    except PdfUserError as exc:
        entry.update(status="failed", error=_short(exc))
        if entry["healthy"] is None and str(exc).startswith("Could not open PDF"):
            entry["healthy"] = False
    except Exception:  # a hostile file's surprise: keep what was learnt before it
        logger.exception("Unexpected error inspecting a PDF")
        entry.update(status="failed", error="Internal error; details were logged by the "
                                            "server")
    return entry
