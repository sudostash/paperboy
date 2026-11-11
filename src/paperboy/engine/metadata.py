"""Read and write document metadata (title, author, subject, keywords...)."""
from __future__ import annotations

from pathlib import Path

import pikepdf

from ..config import Settings
from ..results import MetadataResult, SetMetadataResult
from .base import inherit_protection, open_pdf, refuse_signed, require_modify_permission

# Friendly names mapped to XMP/docinfo keys pikepdf understands.
_FIELDS = {
    "title": "dc:title",
    "author": "dc:creator",
    "subject": "dc:description",
    "keywords": "pdf:Keywords",
    "creator": "xmp:CreatorTool",
    "producer": "pdf:Producer",
}


# The same fields in the classic document Info dictionary.
_DOCINFO = {
    "title": "/Title",
    "author": "/Author",
    "subject": "/Subject",
    "keywords": "/Keywords",
    "creator": "/Creator",
    "producer": "/Producer",
}


def read_metadata(pdf: pikepdf.Pdf) -> dict[str, str]:
    """The friendly metadata fields that have a value. Never changes *pdf*.

    Each comes from XMP when present (authoritative per PDF 2.0), else from the
    classic Info dictionary, which many PDFs use exclusively.
    """
    xmp_values = {}
    # Opening XMP on a file without it would create it; a non-stream can't be parsed.
    if isinstance(pdf.Root.get("/Metadata"), pikepdf.Stream):
        with pdf.open_metadata(set_pikepdf_as_editor=False, update_docinfo=False) as xmp:
            xmp_values = {key: xmp.get(key) for key in _FIELDS.values() if key in xmp}
    out: dict[str, str] = {}
    for friendly, key in _FIELDS.items():
        val = xmp_values.get(key)
        if val is None:  # pikepdf returns None for an empty XMP value
            continue
        val = ", ".join(val) if isinstance(val, list) else str(val)
        if val.strip():  # an empty XMP value must not hide the Info dictionary's
            out[friendly] = val
    info = pdf.trailer.get("/Info")  # not pdf.docinfo, which creates one if missing
    if isinstance(info, pikepdf.Dictionary):
        for friendly, key in _DOCINFO.items():
            if friendly not in out and key in info:
                out[friendly] = str(info[key])
    return {k: v for k, v in out.items() if v.strip()}


def get_metadata(path: Path, settings: Settings, password: str | None = None) -> MetadataResult:
    with open_pdf(path, settings, password) as pdf:
        return {"path": str(path), "metadata": read_metadata(pdf)}


def set_metadata(
    path: Path,
    out: Path,
    fields: dict[str, str],
    settings: Settings,
    password: str | None = None,
) -> SetMetadataResult:
    """Set metadata fields. Valid keys: title, author, subject, keywords, creator, producer."""
    unknown = set(fields) - set(_FIELDS)
    if unknown:
        from ..errors import PdfUserError
        raise PdfUserError(
            f"Unknown metadata field(s): {', '.join(sorted(unknown))}. "
            f"Valid: {', '.join(_FIELDS)}"
        )
    # dc:creator is an ordered array (rdf:Seq); it must be assigned a list.
    seq_fields = {"author"}
    with open_pdf(path, settings, password) as pdf:
        require_modify_permission(pdf, "changing its document information")
        usage_rights = refuse_signed(pdf, "changing its document information")
        if usage_rights:  # Reader extensions break once the file changes; drop them
            del pdf.Root.Perms
        # set_pikepdf_as_editor=False keeps pikepdf from restamping Producer/
        # MetadataDate and emitting "will be overwritten" warnings. update_docinfo=False
        # because pikepdf would rebuild the Info dictionary from XMP alone, erasing
        # every field (title, dates...) that only Info held; only the changed fields
        # are written there, below.
        with pdf.open_metadata(set_pikepdf_as_editor=False, update_docinfo=False) as m:
            for friendly, value in fields.items():
                if friendly in seq_fields:
                    m[_FIELDS[friendly]] = [v.strip() for v in value.split(",") if v.strip()]
                else:
                    m[_FIELDS[friendly]] = value
        for friendly, value in fields.items():
            if friendly in seq_fields:
                value = ", ".join(v.strip() for v in value.split(",") if v.strip())
            pdf.docinfo[_DOCINFO[friendly]] = pikepdf.String(value)
        protection = inherit_protection(pdf, password)
        pdf.save(str(out), encryption=protection.encryption)
    return protection.report({"output": str(out), "updated": sorted(fields)})
