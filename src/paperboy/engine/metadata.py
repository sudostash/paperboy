"""Read and write document metadata (title, author, subject, keywords...)."""
from __future__ import annotations

from pathlib import Path

from ..config import Settings
from ..results import MetadataResult, SetMetadataResult
from .base import inherit_protection, open_pdf

# Friendly names mapped to XMP/docinfo keys pikepdf understands.
_FIELDS = {
    "title": "dc:title",
    "author": "dc:creator",
    "subject": "dc:description",
    "keywords": "pdf:Keywords",
    "creator": "xmp:CreatorTool",
    "producer": "pdf:Producer",
}


def get_metadata(path: Path, settings: Settings, password: str | None = None) -> MetadataResult:
    with open_pdf(path, settings, password) as pdf:
        out: dict[str, str] = {}
        with pdf.open_metadata() as m:
            for friendly, key in _FIELDS.items():
                if key in m:
                    val = m[key]
                    out[friendly] = ", ".join(val) if isinstance(val, list) else str(val)
        return {"path": str(path), "metadata": out}


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
        # set_pikepdf_as_editor=False keeps pikepdf from restamping Producer/
        # MetadataDate and emitting "will be overwritten" warnings.
        with pdf.open_metadata(set_pikepdf_as_editor=False) as m:
            for friendly, value in fields.items():
                if friendly in seq_fields:
                    m[_FIELDS[friendly]] = [v.strip() for v in value.split(",") if v.strip()]
                else:
                    m[_FIELDS[friendly]] = value
        protection = inherit_protection(pdf, password)
        pdf.save(str(out), encryption=protection.encryption)
    return protection.report({"output": str(out), "updated": sorted(fields)})
