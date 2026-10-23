"""compare_pdfs: what changed between two versions of a PDF.

Pages are matched first, so an inserted or deleted page doesn't make every page
after it look changed: pages whose text is identical and unique in both documents
anchor the match, and the pages between anchors are matched by how alike their
lines are. Matched pages are then compared by their text and, optionally, by how
they look (the share of pixels that differ, with images that show what was removed
in red, added in green, and recoloured in blue).

Every stage is bounded: page content is measured first and heavy pages skipped;
matching and line differences have work budgets (past them, simpler methods take
over); listed lines share one size budget; and a deadline covers the whole call,
from the first page read to the last image, reporting complete=false when it hits.
"""
from __future__ import annotations

import bisect
import difflib
import hashlib
import heapq
import io
import re
import time
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import pypdfium2 as pdfium
from PIL import Image, ImageChops

from ..config import Settings
from ..errors import PdfUserError
from ..results import CompareResult
from .base import open_pdf, require_copy_permission
from .render import _PDFIUM_LOCK, _close, _open, _page_size, _render
from .search import _content_size

MODES = ("text", "visual", "both")
MAX_PAGE_CONTENT = 2 * 2**20   # decoded content per page (forms as often as drawn) read
MAX_LINES = 2_000              # lines of a page compared line by line
MAX_LINE = 500                 # characters of a line shown
SIGNATURE_WORDS = 2_000        # words of a page used to tell how alike two pages are
MATCH_CELLS = 250_000          # page pairs scored when matching; past it, by position
MIN_LIKENESS = 0.3             # pages less alike than this aren't matched as versions
DIFF_CELLS = 40_000            # line pairs one page diff may weigh; past it, by set
MAX_DIFF_LINES = 50            # added or removed lines listed per page
MAX_LISTED_CHARS = 50_000      # characters of added and removed lines in one result
MAX_DIFFERENCES = 500          # pages listed as different
MAX_VISUAL_PAIRS = 200         # page pairs rendered per call
VISUAL_SIDE = 1_000            # pixels on a rendered page's long side
PIXEL_CONTRAST = 24            # a colour channel this far apart counts as changed
MAX_IMAGES = 5                 # difference images returned
COMPARE_TIME_LIMIT_S = 60      # per call


@dataclass
class _Page:
    """A page's text as compared: *digest* is of all of it; *keys* (and *shown*, the
    same lines as written) are its first MAX_LINES lines, each cut to MAX_LINE."""
    digest: str | None             # None: not read (skipped, or out of time)
    keys: list[str] = field(default_factory=list)
    shown: list[str] = field(default_factory=list)
    cut: bool = False              # lines or characters past the limits weren't listed
    signature: frozenset = frozenset()

    @property
    def empty(self) -> bool:
        return self.digest is not None and not self.keys


def _read(text: str, ignore_case: bool) -> _Page:
    """Normalise a page's text: NFKC, whitespace runs as one space, no empty lines; for
    ignore_case, compared case-folded (shown as written)."""
    keys, shown, cut, digest = [], [], False, hashlib.sha256()
    for line in text.splitlines():
        line = re.sub(r"\s+", " ", unicodedata.normalize("NFKC", line.replace("\x00", "")))
        line = line.strip()
        if not line:
            continue
        key = line.casefold() if ignore_case else line
        digest.update(key.encode("utf-8", "surrogatepass") + b"\n")
        if len(keys) < MAX_LINES:
            keys.append(key[:MAX_LINE])
            shown.append(line[:MAX_LINE])
            cut = cut or len(line) > MAX_LINE
        else:
            cut = True
    words = [w for k in keys for w in k.split()][:SIGNATURE_WORDS]
    return _Page(digest.hexdigest(), keys, shown, cut, frozenset(map(hash, words)))


def _likeness(a: _Page, b: _Page) -> float:
    """How alike two pages' words are (0-1): shared distinct words over all of them.
    Pages without text are like nothing: blank pages are paired by position."""
    if not a.signature or not b.signature:
        return 0.0
    return len(a.signature & b.signature) / len(a.signature | b.signature)


# --- matching pages -------------------------------------------------------------------------


class _Clock:
    def __init__(self, seconds: float):
        self.deadline = time.monotonic() + seconds

    @property
    def out(self) -> bool:
        return time.monotonic() > self.deadline


def _anchors(a: list[_Page], b: list[_Page]) -> list[tuple[int, int]]:
    """Pages identical and unique in both documents (blank pages, repeated in either,
    can't anchor), kept in the longest order both documents agree on."""
    def unique(pages):
        seen = Counter(p.digest for p in pages if p.digest is not None and not p.empty)
        return {p.digest: i for i, p in enumerate(pages) if p.digest and seen[p.digest] == 1}
    in_a, in_b = unique(a), unique(b)
    candidates = sorted((i, in_b[d]) for d, i in in_a.items() if d in in_b)
    # Longest increasing subsequence of B positions (patience sorting).
    tails, links, ends = [], [None] * len(candidates), []
    for n, (_, j) in enumerate(candidates):
        k = bisect.bisect_left(tails, j)
        links[n] = ends[k - 1] if k else None
        if k == len(tails):
            tails.append(j)
            ends.append(n)
        else:
            tails[k], ends[k] = j, n
    chain, n = [], ends[-1] if ends else None
    while n is not None:
        chain.append(candidates[n])
        n = links[n]
    return chain[::-1]


def _in_order(a0: int, a1: int, b0: int, b1: int) -> list[tuple[int | None, int | None]]:
    """Unmatched runs side by side: pair in order, then what's left over."""
    common = min(a1 - a0, b1 - b0)
    return ([(a0 + k, b0 + k) for k in range(common)]
            + [(i, None) for i in range(a0 + common, a1)]
            + [(None, j) for j in range(b0 + common, b1)])


def _gap(a: list[_Page], b: list[_Page], a0: int, a1: int, b0: int, b1: int,
         budget: list[int], clock: _Clock) -> list[tuple[int | None, int | None]]:
    """Match the pages between two anchors by likeness (an alignment that keeps order
    and maximises it), within the shared *budget* of scored pairs and the deadline."""
    rows, cols = a1 - a0, b1 - b0
    if rows == 0 or cols == 0 or rows * cols > budget[0]:
        return _in_order(a0, a1, b0, b1)
    budget[0] -= rows * cols
    score = [[0.0] * (cols + 1) for _ in range(rows + 1)]
    for r in range(rows - 1, -1, -1):
        if clock.out:
            return _in_order(a0, a1, b0, b1)
        for c in range(cols - 1, -1, -1):
            best = max(score[r + 1][c], score[r][c + 1])
            like = _likeness(a[a0 + r], b[b0 + c])
            if like >= MIN_LIKENESS:
                best = max(best, like + score[r + 1][c + 1])
            score[r][c] = best
    pairs, r, c, run_r, run_c = [], 0, 0, 0, 0
    while r < rows and c < cols:
        like = _likeness(a[a0 + r], b[b0 + c])
        if like >= MIN_LIKENESS and score[r][c] == like + score[r + 1][c + 1]:
            pairs += _in_order(a0 + run_r, a0 + r, b0 + run_c, b0 + c)
            pairs.append((a0 + r, b0 + c))
            r, c = r + 1, c + 1
            run_r, run_c = r, c
        elif score[r + 1][c] >= score[r][c + 1]:
            r += 1
        else:
            c += 1
    return pairs + _in_order(a0 + run_r, a1, b0 + run_c, b1)


def _match(a: list[_Page], b: list[_Page], clock: _Clock
           ) -> list[tuple[int | None, int | None]]:
    pairs, budget, last = [], [MATCH_CELLS], (0, 0)
    for i, j in [*_anchors(a, b), (len(a), len(b))]:
        pairs += _gap(a, b, last[0], i, last[1], j, budget, clock)
        if i < len(a):
            pairs.append((i, j))
        last = (i + 1, j + 1)
    return pairs


# --- comparing a pair -----------------------------------------------------------------------


def _line_changes(a: _Page, b: _Page) -> tuple[list[str], list[str], float]:
    """(removed lines, added lines, how alike 0-1). Lines are aligned with difflib
    when the pages are small enough; past DIFF_CELLS, as a difference of line counts
    (order of appearance kept), which can't take long whatever the lines."""
    if len(a.keys) * len(b.keys) <= DIFF_CELLS:
        matcher = difflib.SequenceMatcher(None, a.keys, b.keys, autojunk=False)
        removed, added = [], []
        for tag, a0, a1, b0, b1 in matcher.get_opcodes():
            if tag in ("replace", "delete"):
                removed += a.shown[a0:a1]
            if tag in ("replace", "insert"):
                added += b.shown[b0:b1]
        return removed, added, matcher.ratio()
    left, right = Counter(b.keys), Counter(a.keys)
    removed, added = [], []
    for key, line in zip(a.keys, a.shown, strict=True):
        if left[key]:
            left[key] -= 1
        else:
            removed.append(line)
    for key, line in zip(b.keys, b.shown, strict=True):
        if right[key]:
            right[key] -= 1
        else:
            added.append(line)
    total = len(a.keys) + len(b.keys)
    return removed, added, (total - len(removed) - len(added)) / total if total else 1.0


def _picture(doc, index: int) -> Image.Image:
    """The page in colour, its long side VISUAL_SIDE pixels."""
    scale = VISUAL_SIDE / max(_page_size(doc, index))
    return _render(doc, index, scale).convert("RGB")


def _visual(a: Image.Image, b: Image.Image) -> tuple[float, Image.Image]:
    """The share of pixels that differ (in %), and an image of B with what A had and B
    lacks in red, what B added in green, what changed colour in blue, the rest faded.
    Pages of different sizes are compared on a white canvas big enough for both."""
    size = (max(a.width, b.width), max(a.height, b.height))
    if a.size != size or b.size != size:
        a, b = (_on_canvas(p, size) for p in (a, b))
    changed = _mask(_strongest(ImageChops.difference(a, b)), PIXEL_CONTRAST)
    grey_a, grey_b = a.convert("L"), b.convert("L")
    removed = ImageChops.multiply(changed, _mask(ImageChops.subtract(grey_b, grey_a), 12))
    added = ImageChops.multiply(changed, _mask(ImageChops.subtract(grey_a, grey_b), 12))
    share = sum(changed.histogram()[255:]) / (size[0] * size[1]) * 100
    faded = grey_b.point(lambda v: 170 + v * 85 // 255)
    picture = Image.merge("RGB", (faded, faded, faded))
    picture.paste((40, 90, 220), mask=changed)          # recoloured, unless below
    picture.paste((220, 30, 30), mask=removed)
    picture.paste((20, 160, 60), mask=added)
    return share, picture


def _on_canvas(image: Image.Image, size: tuple[int, int]) -> Image.Image:
    canvas = Image.new("RGB", size, "white")
    canvas.paste(image, (0, 0))
    return canvas


def _strongest(diff: Image.Image) -> Image.Image:
    r, g, b = diff.split()
    return ImageChops.lighter(ImageChops.lighter(r, g), b)


def _mask(image: Image.Image, threshold: int) -> Image.Image:
    return image.point(lambda v: 255 if v > threshold else 0)


# --- the comparison -------------------------------------------------------------------------


def _prepare(path: Path, settings: Settings, password: str | None, mode: str,
             clock: _Clock) -> tuple[bytes, int, set[int], set[int]]:
    """Check the password (and, to compare text, the copy permission); find pages too
    large to read, and those not measured in time; return the bytes for PDFium,
    decrypted if need be (PDFium never sees a password)."""
    data = path.read_bytes()
    with open_pdf(data, settings, password) as pdf:
        if mode != "visual":
            require_copy_permission(pdf, "comparing its text")
        total = len(pdf.pages)
        if not total:
            raise PdfUserError(f"{path.name} has no pages")
        sizes: dict = {}
        heavy, late = set(), set()
        for i, page in enumerate(pdf.pages):
            if clock.out:
                late.add(i)
                continue
            size = _content_size(page, MAX_PAGE_CONTENT, sizes)
            if size is None or size > MAX_PAGE_CONTENT:
                heavy.add(i)
        if pdf.is_encrypted:
            plain = io.BytesIO()
            pdf.save(plain)  # without encryption=, the copy is decrypted
            data = plain.getvalue()
    return data, total, heavy, late


def _page_text(doc, index: int) -> str:
    with _PDFIUM_LOCK:
        try:
            page = doc[index]
        except pdfium.PdfiumError as exc:
            raise PdfUserError(f"Could not read page {index + 1}: {exc}") from exc
        try:
            textpage = page.get_textpage()
            try:
                total = textpage.count_chars()
                return textpage.get_text_range(0, total) if total > 0 else ""
            finally:
                textpage.close()
        finally:
            page.close()


def compare_pdfs(path_a: Path, path_b: Path, settings: Settings, mode: str = "text",
                 ignore_case: bool = False, password_a: str | None = None,
                 password_b: str | None = None) -> tuple[CompareResult, list[bytes]]:
    """Compare two PDFs page by page; return the report and up to MAX_IMAGES PNG
    difference images (for the most changed pages, in visual modes)."""
    if mode not in MODES:
        raise PdfUserError(f"mode must be one of: {', '.join(MODES)}")
    clock = _Clock(COMPARE_TIME_LIMIT_S)
    data_a, total_a, heavy_a, late_a = _prepare(path_a, settings, password_a, mode, clock)
    data_b, total_b, heavy_b, late_b = _prepare(path_b, settings, password_b, mode, clock)
    skipped = [{"document": name, "page": i + 1, "reason": "too much content to read safely"}
               for name, heavy in (("a", heavy_a), ("b", heavy_b)) for i in sorted(heavy)]
    complete = not (late_a or late_b)
    doc_a, doc_b = _open(data_a), _open(data_b)
    try:
        pages: dict[str, list[_Page]] = {}
        for name, doc, total, heavy, late in (("a", doc_a, total_a, heavy_a, late_a),
                                              ("b", doc_b, total_b, heavy_b, late_b)):
            pages[name] = []
            for i in range(total):
                if i in heavy or i in late:
                    pages[name].append(_Page(None))
                elif mode == "visual":
                    pages[name].append(_Page(str(i)))   # matched by position only
                elif clock.out:
                    complete = False
                    pages[name].append(_Page(None))
                else:
                    pages[name].append(_read(_page_text(doc, i), ignore_case))
        a, b = pages["a"], pages["b"]
        if mode == "visual":
            pairs = _in_order(0, total_a, 0, total_b)
        else:
            pairs = _match(a, b, clock)

        counts = {"same": 0, "changed": 0, "added": 0, "removed": 0, "unknown": 0}
        differences, best, rendered, listed = [], [], 0, MAX_LISTED_CHARS
        truncated = False
        for i, j in pairs:
            entry = {"status": "", "page_a": None if i is None else i + 1,
                     "page_b": None if j is None else j + 1, "similarity": None,
                     "added": [], "removed": [], "lines_truncated": False,
                     "pixel_change": None}
            if j is None:
                entry["status"] = "removed"
            elif i is None:
                entry["status"] = "added"
            elif a[i].digest is None or b[j].digest is None:
                entry["status"] = "unknown"
            else:
                same_text = mode == "visual" or a[i].digest == b[j].digest
                text_known = True
                if not same_text:
                    if clock.out:
                        complete, text_known = False, False
                    else:
                        removed, added, alike = _line_changes(a[i], b[j])
                        entry["similarity"] = round(alike, 3)
                        cut = a[i].cut or b[j].cut or \
                            max(len(removed), len(added)) > MAX_DIFF_LINES
                        for key, lines in (("removed", removed), ("added", added)):
                            for line in lines[:MAX_DIFF_LINES]:
                                if len(line) > listed:
                                    cut = truncated = True
                                    break
                                listed -= len(line)
                                entry[key].append(line)
                        entry["lines_truncated"] = cut
                looks_same = True
                if mode != "text":
                    if rendered >= MAX_VISUAL_PAIRS or clock.out:
                        complete, looks_same = False, None
                    else:
                        rendered += 1
                        share, picture = _visual(_picture(doc_a, i), _picture(doc_b, j))
                        entry["pixel_change"] = round(share, 3)
                        looks_same = share == 0
                        if share > 0:  # keep only the most changed pictures
                            item = (share, -rendered, i, j, picture)
                            if len(best) < MAX_IMAGES:
                                heapq.heappush(best, item)
                            else:
                                heapq.heappushpop(best, item)
                if mode == "text" and a[i].empty and b[j].empty:
                    entry["status"] = "unknown"   # no text on either page (e.g. scans)
                elif not text_known or (looks_same is None and same_text):
                    entry["status"] = "unknown"
                else:
                    entry["status"] = "same" if same_text and looks_same else "changed"
            counts[entry["status"]] += 1
            if entry["status"] != "same":
                if len(differences) < MAX_DIFFERENCES:
                    differences.append(entry)
                else:
                    truncated = True
    finally:
        _close(doc_a)
        _close(doc_b)
    images, shown = [], []
    for share, _, i, j, picture in sorted(best, reverse=True):
        buf = io.BytesIO()
        picture.save(buf, "PNG")
        images.append(buf.getvalue())
        shown.append({"page_a": i + 1, "page_b": j + 1, "pixel_change": round(share, 3)})
    result = {
        "path_a": str(path_a), "path_b": str(path_b), "pages_a": total_a, "pages_b": total_b,
        "mode": mode, "identical": counts["same"] == len(pairs) and total_a == total_b,
        **counts, "differences": differences, "truncated": truncated,
        "complete": complete, "pages_skipped": skipped, "images": shown,
    }
    return result, images
