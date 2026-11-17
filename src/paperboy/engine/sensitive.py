"""Find personal data in a PDF's text, for the user to review and redact.

Each kind of data is a regular expression, run by redact_pdf's pattern worker (so a
slow match can't hang the server), and most are then checked the way the data itself
is: card numbers by their check digit, IBANs by theirs, social security numbers by the
ranges never issued. Text the assistant names (a person's name, an address) is found as
search_text finds it. Each finding keeps the areas of the page it covers, so only what
the user approves is redacted, by those areas.
"""
from __future__ import annotations

import hashlib
import io
import math
import re
import time
from pathlib import Path

from PIL import ImageDraw

from ..config import Settings
from ..errors import PdfUserError
from ..pagespec import parse_pages
from ..results import SensitiveDataResult
from .base import open_pdf, require_copy_permission
from .redact import MAX_TEXTS, REDACT_TIME_LIMIT_S, _crop, _find, _flattened, _plain
from .render import _PDFIUM_LOCK, _close, _open
from .search import MAX_PAGE_CONTENT, _content_size, _query

MAX_FINDINGS = 300         # findings one review may hold
SNIPPET_SCALE = 1.5        # pixels per point in the pictures shown for review
SNIPPET_MARGIN = 24        # points of the page shown above and below a finding
SNIPPET_PIXELS = 4_000_000  # a page is drawn smaller rather than bigger than this
SNIPPET_WIDTH = 900        # pixels, at most
SNIPPET_HEIGHT = 300       # pixels, at most (before scaling to the width)
SNIPPET_BYTES = 16 * 2**20  # pictures one review holds; later findings get none
LIMITS = ("Found by pattern: email addresses, phone numbers, payment card numbers, "
          "IBANs, US social security numbers, UK National Insurance numbers and dates "
          "of birth (a date after a label such as \"Date of birth\"). Names, postal "
          "addresses and other free text aren't recognised; pass them as texts. Numbers "
          "written unusually (a social security number without dashes, a date of birth "
          "on the line after its label) can be missed, so read the page too. Text in "
          "pictures (scans) isn't read: run ocr_pdf first.")

_MONTH = (r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|"
          r"aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)")
_DATE = (r"(?:\d{1,2}[/.-]\d{1,2}[/.-](?:\d{4}|\d{2})|\d{4}-\d{2}-\d{2}"
         rf"|\d{{1,2}}(?:st|nd|rd|th)? {_MONTH}\.?,? \d{{4}}"
         rf"|{_MONTH}\.? \d{{1,2}}(?:st|nd|rd|th)?,? \d{{4}})")
# Python's look-behind takes only fixed-width text, so each way of writing the label
# is a look-behind of its own: only the date is found, not its label.
_LABELS = ("date of birth", "birth date", "birthdate", "dob", "d.o.b.", "d.o.b", "born",
           "born on")
_DOB = "(?:" + "|".join(f"(?<={re.escape(label + sep)})" for label in _LABELS
                        for sep in (": ", ":", " ", " - ", ", ")) + ")" + _DATE

# kind: (what the review page calls it, pattern)
KINDS: dict[str, tuple[str, str]] = {
    "email": ("Email address", r"(?<![\w.+-])[\w.+-]+@[\w-]+(?:\.[\w-]+)+"),
    "card_number": ("Card number", r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)"),
    # In groups of four (as written, or run together), so a word after it isn't taken in.
    "iban": ("IBAN", r"(?<![a-z0-9])[a-z]{2}\d{2}(?: ?[a-z0-9]{4}){2,7}(?: ?[a-z0-9]{1,3})?"
                     r"(?![a-z0-9])"),
    "us_ssn": ("Social security number", r"(?<![\d-])\d{3}-\d{2}-\d{4}(?![\d-])"),
    "uk_ni_number": ("National Insurance number",
                     r"(?<![a-z0-9])[a-z]{2} ?\d{2} ?\d{2} ?\d{2} ?[a-d](?![a-z0-9])"),
    "date_of_birth": ("Date of birth", _DOB),
    "phone": ("Phone number",
              r"(?<![\w+])(?:\+\d{1,3}[ .-]?)?(?:\(\d{1,5}\)[ .-]?)?\d{2,5}"
              r"(?:[ .-]\d{2,6}){1,5}(?![\w])|(?<![\w+])(?:\+\d{9,15}|0\d{9,10})(?!\w)"),
}
# When two findings cover the same text, the more specific one stays.
_PRIORITY = ["custom", "card_number", "iban", "us_ssn", "uk_ni_number", "date_of_birth",
             "email", "phone"]
_NI_PREFIXES_BANNED = {"BG", "GB", "KN", "NK", "NT", "TN", "ZZ"}


def _digits(text: str) -> str:
    return "".join(c for c in text if c.isdigit())


def _luhn(number: str) -> bool:
    total = 0
    for i, c in enumerate(reversed(number)):
        d = int(c) * (2 if i % 2 else 1)
        total += d - 9 if d > 9 else d
    return total % 10 == 0


def _real(kind: str, text: str) -> bool:
    """Whether a pattern's match is the real thing, as far as its own checks tell."""
    if kind not in ("email", "custom") and not text.isascii():
        return False  # "[a-z]" ignoring case also takes the Kelvin sign, dotless i, ...
    if kind == "card_number":
        digits = _digits(text)
        return 13 <= len(digits) <= 19 and _luhn(digits) and len(set(digits)) > 1
    if kind == "iban":
        code = text.replace(" ", "")
        if not (code[:2].isalpha() and code[:2].isupper() and 15 <= len(code) <= 34):
            return False
        moved = (code[4:] + code[:4]).upper()
        return int("".join(str(int(c, 36)) for c in moved)) % 97 == 1
    if kind == "us_ssn":
        area, group, serial = text.split("-")
        return area not in ("000", "666") and area[0] != "9" and group != "00" \
            and serial != "0000"
    if kind == "uk_ni_number":
        code = text.replace(" ", "")
        return code.isupper() and code[:2] not in _NI_PREFIXES_BANNED \
            and code[0] not in "DFIQUV" and code[1] not in "DFIOQUV"
    if kind == "phone":
        digits = _digits(text)
        if not 9 <= len(digits) <= 15:
            return False
        if re.fullmatch(r"\d{1,4}([/.-])\d{1,2}\1\d{1,4}", text):  # a date
            return False
        # A grouped number without a leading +, 0 or bracket is too often an amount, a
        # reference or a range; a plain run of digits is a phone number only after +.
        return text[0] in "+0(" or bool(re.fullmatch(r"\d{3}[ .-]\d{3}[ .-]\d{4}", text))
    return True


def mask(kind: str, text: str) -> str:
    """A finding as the assistant sees it: enough to tell findings apart, not the data."""
    if kind == "email" and "@" in text:
        user, _, domain = text.partition("@")
        return f"{user[:1]}***@{domain[:1]}***{domain[domain.rfind('.'):]}"
    if kind in ("card_number", "iban", "phone"):
        tail = text.replace(" ", "")[-4:]
        return f"***{tail}"
    return text[:1] + "***" if len(text) > 1 else "***"


def _inside(f: dict, k: dict) -> bool:
    """Whether every area of finding *f* lies within an area of finding *k*."""
    return f["page"] == k["page"] and all(
        any(b[0] - 0.5 <= a[0] and a[2] <= b[2] + 0.5 and b[1] - 0.5 <= a[1]
            and a[3] <= b[3] + 0.5 for b in k["rects"]) for a in f["rects"])


def _check(kinds, texts) -> tuple[list[str], list[str]]:
    if kinds is not None:
        if not isinstance(kinds, list) or not kinds:
            raise PdfUserError("kinds must be a non-empty list")
        unknown = [k for k in kinds if k not in KINDS]
        if unknown:
            raise PdfUserError(f"Unknown kind {unknown[0]!r}; use any of: "
                               f"{', '.join(KINDS)}")
    kinds = list(dict.fromkeys(kinds)) if kinds is not None else list(KINDS)
    if texts is not None and not isinstance(texts, list):
        raise PdfUserError("texts must be a list")
    texts = list(dict.fromkeys(_query(t) for t in texts or []))
    if len(texts) > MAX_TEXTS:
        raise PdfUserError(f"Give at most {MAX_TEXTS} texts")
    return kinds, texts


def detect(path: Path, settings: Settings, kinds: list[str] | None = None,
           texts: list[str] | None = None, pages: str | None = None,
           password: str | None = None, snippets: bool = False) -> dict:
    """The personal data found in a PDF: its findings (id, kind, page, text, areas and,
    with *snippets*, a JPEG of where each is on its page), the pages searched and
    those without text, and a digest of the file's bytes, which a review is bound to."""
    kinds, texts = _check(kinds, texts)
    data = path.read_bytes()
    deadline = time.monotonic() + REDACT_TIME_LIMIT_S
    with open_pdf(data, settings, password) as pdf:
        require_copy_permission(pdf, "finding personal data")
        total = len(pdf.pages)
        targets = parse_pages(pages, total, dedupe=True) if pages else list(range(total))
        sizes: dict = {}
        heavy = [i + 1 for i in targets
                 if (size := _content_size(pdf.pages[i], MAX_PAGE_CONTENT, sizes)) is None
                 or size > MAX_PAGE_CONTENT]
        if heavy:
            raise PdfUserError(f"Pages {', '.join(map(str, heavy[:20]))} have too much "
                               "content to search safely; leave them out with pages")
        plain = _plain(pdf)
    patterns = [KINDS[k][1] for k in kinds]
    doc = _open(_flattened(plain, deadline))  # text in form fields and comments counts too
    try:
        matches, without = _find(doc, targets, texts, patterns, False, False, deadline)
        found = []
        for index, source, text, rects in matches:
            kind = "custom" if source in texts else kinds[patterns.index(source)]
            if rects and _real(kind, text):
                found.append({"kind": kind, "page": index + 1, "text": text,
                              "rects": [list(r) for r in rects]})
        if len(found) > 4 * MAX_FINDINGS:  # before comparing them all with each other
            raise PdfUserError(f"Over {MAX_FINDINGS} items found; choose fewer pages or "
                               "kinds at a time")
        found.sort(key=lambda f: _PRIORITY.index(f["kind"]))
        # A finding wholly inside another is the same text (a card-like run in an IBAN, a
        # name in an email address): only the larger is listed, as redacting it covers
        # both. Findings that merely overlap are both kept, so neither is left half done.
        kept = [f for n, f in enumerate(found)
                if not any(m != n and _inside(f, k) and (not _inside(k, f) or m < n)
                           for m, k in enumerate(found))]
        kept.sort(key=lambda f: (f["page"], -max(r[3] for r in f["rects"]),
                                 min(r[0] for r in f["rects"])))
        if len(kept) > MAX_FINDINGS:
            raise PdfUserError(f"Over {MAX_FINDINGS} items found; choose fewer pages or "
                               "kinds at a time")
        for n, f in enumerate(kept, 1):
            f["id"] = n
        if snippets:
            _snippets(doc, kept, deadline)
    finally:
        _close(doc)
    return {"findings": kept, "kinds": kinds, "pages_searched": len(targets),
            "pages_without_text": without, "digest": hashlib.sha256(data).hexdigest()}


def _snippets(doc, findings: list[dict], deadline: float) -> None:
    """Give each finding a JPEG of its line or lines on the page, the finding marked;
    none for text off the visible page, or once the review's pictures are too big."""
    budget = SNIPPET_BYTES
    for page in sorted({f["page"] for f in findings}):
        if time.monotonic() > deadline:
            raise PdfUserError(f"Preparing the review took longer than "
                               f"{REDACT_TIME_LIMIT_S} s; choose fewer pages")
        with _PDFIUM_LOCK:
            handle = doc[page - 1]
            try:
                left, bottom, right, top = _crop(handle)
                width, height = right - left, top - bottom
                scale = min(SNIPPET_SCALE, math.sqrt(SNIPPET_PIXELS / max(width * height, 1)))
                image = handle.render(scale=scale,
                                      rotation=(360 - handle.get_rotation()) % 360,
                                      may_draw_forms=False, draw_annots=False).to_pil()
                image = image.convert("RGB")
            finally:
                handle.close()
        sx, sy = image.width / width, image.height / height
        for f in (f for f in findings if f["page"] == page):
            boxes = [(math.floor((r[0] - left) * sx), math.floor((top - r[3]) * sy),
                      math.ceil((r[2] - left) * sx), math.ceil((top - r[1]) * sy))
                     for r in f["rects"]]
            upper = max(0, min(b[1] for b in boxes) - round(SNIPPET_MARGIN * sy))
            lower = min(image.height, max(b[3] for b in boxes) + round(SNIPPET_MARGIN * sy))
            if lower - upper > SNIPPET_HEIGHT:  # spread down the page: show where it starts
                lower = upper + SNIPPET_HEIGHT
            if budget <= 0 or upper >= image.height or lower <= upper:
                continue
            strip = image.crop((0, upper, image.width, lower))
            draw = ImageDraw.Draw(strip)
            for b in boxes:
                draw.rectangle((b[0] - 3, b[1] - upper - 3, b[2] + 3, b[3] - upper + 3),
                               outline=(214, 40, 40), width=3)
            if strip.width > SNIPPET_WIDTH:
                strip = strip.resize((SNIPPET_WIDTH, max(1, round(
                    strip.height * SNIPPET_WIDTH / strip.width))))
            out = io.BytesIO()
            strip.save(out, "JPEG", quality=85)
            f["snippet"] = out.getvalue()
            budget -= len(f["snippet"])


def find_sensitive_data(path: Path, settings: Settings, kinds: list[str] | None = None,
                        texts: list[str] | None = None, pages: str | None = None,
                        password: str | None = None) -> SensitiveDataResult:
    """What personal data a PDF holds, each item masked: for the assistant to report."""
    found = detect(path, settings, kinds, texts, pages, password)
    findings = [{"id": f["id"], "kind": f["kind"], "page": f["page"],
                 "preview": mask(f["kind"], f["text"])} for f in found["findings"]]
    by_kind: dict[str, int] = {}
    for f in findings:
        by_kind[f["kind"]] = by_kind.get(f["kind"], 0) + 1
    return {"path": str(path), "kinds_searched": found["kinds"], "count": len(findings),
            "by_kind": by_kind, "findings": findings,
            "pages_searched": found["pages_searched"],
            "pages_without_text": found["pages_without_text"], "limits": LIMITS}
