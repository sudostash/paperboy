"""Parse human page specifications into 0-based page indices.

Examples (1-based, inclusive): ``"1,3,5-8"``, ``"4-"`` (to end), ``"last"``,
``"5-1"`` (reverse). Pages may repeat to duplicate them.
"""
from __future__ import annotations

from .errors import PdfUserError


def parse_pages(spec: str, total: int, *, dedupe: bool = False) -> list[int]:
    """Return 0-based page indices for *spec* against a document of *total* pages."""
    if not spec or not spec.strip():
        raise PdfUserError("Empty page specification")

    def num(tok: str) -> int:
        tok = tok.strip().lower()
        if tok in ("last", "end"):
            return total
        if tok in ("first",):
            return 1
        # isdigit() alone admits "²" (int() then crashes) and other scripts' digits.
        if not (tok.isascii() and tok.isdigit()):
            raise PdfUserError(f"Invalid page token: {tok!r}")
        return int(tok)

    result: list[int] = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            start = num(a) if a.strip() else 1
            end = num(b) if b.strip() else total
            step = 1 if start <= end else -1
            pages = list(range(start, end + step, step))
        else:
            pages = [num(part)]
        for p in pages:
            if p < 1 or p > total:
                raise PdfUserError(
                    f"Page {p} is out of range (document has {total} page(s))"
                )
            result.append(p - 1)

    if not result:
        raise PdfUserError("Page specification selected no pages")
    if dedupe:
        seen: set[int] = set()
        result = [p for p in result if not (p in seen or seen.add(p))]
    return result
