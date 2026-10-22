"""search_text: find words or phrases in a PDF, with where they are on the page.

Searching uses PDFium's own text search (literal text, not patterns, so a query
can't make it run for minutes), on the bytes pikepdf checked and decrypted. Each
match comes with a snippet of the text around it and its rectangles on the page,
for tools that highlight or redact.

PDFium builds a text index of a few hundred bytes per character, so a small
hostile file whose pages draw millions of characters could take gigabytes and
minutes. Pages are measured first (their content, forms included) and skipped
past a budget, and a call stops after a time limit, saying so.
"""
from __future__ import annotations

import io
import re
import time
import unicodedata
from pathlib import Path

import pikepdf
import pypdfium2 as pdfium

from ..config import Settings
from ..errors import PdfUserError
from ..pagespec import parse_pages
from ..results import SearchResult
from .base import open_pdf, require_copy_permission
from .render import _PDFIUM_LOCK, _close, _open
from .streams import decoded_size

MAX_QUERY = 200            # characters in a query
MAX_RESULTS = 1_000        # matches one call may return
MAX_RECTS = 20             # rectangles kept per match
CONTEXT = 60               # characters of text kept on each side of a match
MAX_LISTED = 1_000         # pages listed as without text, or skipped
MAX_PAGE_CONTENT = 2 * 2**20  # decoded content per page (forms included) searched
MAX_FORM_DEPTH = 12
SEARCH_TIME_LIMIT_S = 60   # per call


def _query(text) -> str:
    """The query as searched: NFKC (so "ﬁ" finds "fi" and a no-break space is a space),
    whitespace runs as one space, trimmed. Refuses what PDFium can't search for."""
    if not isinstance(text, str):
        raise PdfUserError(f"query must be 1 to {MAX_QUERY} characters of text")
    if any(unicodedata.category(c) == "Cs" for c in text):
        raise PdfUserError("query contains an invalid character")
    text = re.sub(r"\s+", " ", unicodedata.normalize("NFKC", text)).strip()
    if any(unicodedata.category(c) == "Cc" for c in text):
        raise PdfUserError("query must not contain control characters")
    if any(ord(c) > 0xFFFF for c in text):
        raise PdfUserError("query can't contain emoji or other characters beyond U+FFFF; "
                           "PDF text search can't match them")
    if not text or len(text) > MAX_QUERY:
        raise PdfUserError(f"query must be 1 to {MAX_QUERY} characters of text")
    return text


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("\x00", "")).strip()


def _snippet(textpage, index: int, count: int, total: int) -> str:
    start, end = max(0, index - CONTEXT), min(total, index + count + CONTEXT)
    text = _clean(textpage.get_text_range(start, end - start))
    return ("…" if start > 0 else "") + text + ("…" if end < total else "")


def _content_size(page: pikepdf.Page, cap: int, sizes: dict) -> int | None:
    """Decoded bytes of a page's content and the forms it draws, counted no further than
    past *cap*; None if some of it can't be measured. Streams shared between pages are
    measured once (*sizes*)."""
    total, seen, stack = 0, set(), [(page.obj.get("/Contents"), page.obj.get("/Resources"), 0)]
    while stack:
        contents, resources, depth = stack.pop()
        streams = list(contents) if isinstance(contents, pikepdf.Array) else [contents]
        for stream in streams:
            if not isinstance(stream, pikepdf.Stream):
                continue
            key = stream.objgen
            if key not in sizes:
                sizes[key] = decoded_size(stream, cap)
            if sizes[key] is None:
                return None
            total += sizes[key]
            if total > cap:
                return total
        xobjects = resources.get("/XObject") if isinstance(resources, pikepdf.Dictionary) \
            else None
        for _, form in xobjects.items() if isinstance(xobjects, pikepdf.Dictionary) else []:
            if isinstance(form, pikepdf.Stream) and form.get("/Subtype") == pikepdf.Name.Form \
                    and form.objgen not in seen and depth < MAX_FORM_DEPTH:
                seen.add(form.objgen)
                stack.append((form, form.get("/Resources"), depth + 1))
    return total


def _prepare(data: bytes, settings: Settings, password: str | None, pages: str | None
             ) -> tuple[bytes, list[int], set[int]]:
    """Check the password and copy permission, choose the pages, find those too large to
    search, and return the bytes decrypted for PDFium (which never sees a password)."""
    with open_pdf(data, settings, password) as pdf:
        require_copy_permission(pdf, "searching its text")
        total = len(pdf.pages)
        targets = parse_pages(pages, total, dedupe=True) if pages else list(range(total))
        sizes: dict = {}
        heavy = set()
        for i in targets:
            size = _content_size(pdf.pages[i], MAX_PAGE_CONTENT, sizes)
            if size is None or size > MAX_PAGE_CONTENT:
                heavy.add(i)
        if pdf.is_encrypted:
            plain = io.BytesIO()
            pdf.save(plain)  # without encryption=, the copy is decrypted
            data = plain.getvalue()
    return data, targets, heavy


def _page_matches(doc, index: int, query: str, match_case: bool, whole_word: bool,
                  room: int) -> tuple[list[dict], bool]:
    """Up to *room* matches on one page, and whether the page has any text at all."""
    with _PDFIUM_LOCK:
        try:
            page = doc[index]
        except pdfium.PdfiumError as exc:  # e.g. pikepdf repaired pages PDFium can't see
            raise PdfUserError(f"Could not read page {index + 1}: {exc}") from exc
        try:
            textpage = page.get_textpage()
            try:
                total = textpage.count_chars()
                if total <= 0:
                    return [], False
                searcher = textpage.search(query, match_case=match_case,
                                           match_whole_word=whole_word)
                found = []
                try:
                    while len(found) < room:
                        hit = searcher.get_next()
                        if hit is None:
                            break
                        start, count = hit
                        n = textpage.count_rects(start, count)
                        found.append({
                            "page": index + 1,
                            "text": _clean(textpage.get_text_range(start, count)),
                            "context": _snippet(textpage, start, count, total),
                            "rects": [[round(v, 2) for v in textpage.get_rect(r)]
                                      for r in range(min(n, MAX_RECTS))],
                            "rects_truncated": n > MAX_RECTS,
                        })
                finally:
                    searcher.close()
                return found, True
            finally:
                textpage.close()
        finally:
            page.close()


def search_text(path: Path, query: str, settings: Settings, pages: str | None = None,
                match_case: bool = False, whole_word: bool = False,
                max_results: int = 100, password: str | None = None) -> SearchResult:
    """Find *query* on all pages, or the pages given, in page order."""
    query = _query(query)
    if not isinstance(max_results, int) or isinstance(max_results, bool) \
            or not 1 <= max_results <= MAX_RESULTS:
        raise PdfUserError(f"max_results must be 1 to {MAX_RESULTS}")
    data, targets, heavy = _prepare(path.read_bytes(), settings, password, pages)
    doc = _open(data)
    matches, blank, skipped, searched, truncated = [], [], [], 0, False
    deadline = time.monotonic() + SEARCH_TIME_LIMIT_S
    try:
        for n, index in enumerate(targets):
            if n and time.monotonic() > deadline:
                break
            searched += 1
            if index in heavy:
                if len(skipped) < MAX_LISTED:
                    skipped.append(index + 1)
                continue
            found, has_text = _page_matches(doc, index, query, match_case, whole_word,
                                            max_results + 1 - len(matches))
            if not has_text and len(blank) < MAX_LISTED:
                blank.append(index + 1)
            matches += found
            if len(matches) > max_results:
                matches, truncated = matches[:max_results], True
                break
    finally:
        _close(doc)
    return {"path": str(path), "query": query, "pages_searched": searched,
            "complete": truncated or searched == len(targets),
            "match_count": len(matches), "matches": matches, "truncated": truncated,
            "pages_without_text": blank, "pages_skipped": skipped}
