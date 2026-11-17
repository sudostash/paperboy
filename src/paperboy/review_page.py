"""A local page where the user decides, item by item, what personal data is redacted.

redact_sensitive_data finds the data, then the user reviews it here, not in the chat:
each item is shown with a picture of where it is on its page, and only the items the
user leaves ticked are redacted. The assistant never decides, and the data itself
never passes through the client. The page is served like the password page (see
:mod:`.password_page`): on 127.0.0.1 only, the Host header checked, each link with an
unguessable token, used once, expiring; and the decision is bound to the file and the
findings it was asked about.
"""
from __future__ import annotations

import html
import secrets
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs

from .password_page import SOCKET_TIMEOUT_S, SWEEP_EVERY_S, _Server

LINK_LIFETIME_S = 1800    # a link, and the decision made on it, expire after this
MAX_PENDING = 10          # reviews waiting at once (each holds its pictures)
MAX_BODY = 16384          # bytes a decision may have


@dataclass
class Review:
    ref: str              # a short reference the model may see and pass back
    token: str            # the secret part of the link
    label: str            # the file, as shown on the page
    findings: list        # [{"id", "label", "page", "text", "snippet"}]
    purpose: tuple = ()   # the file, its digest and the search: what the decision is for
    waited: bool = False  # the call that asked waits for it; it can't be taken by ref
    created: float = field(default_factory=time.monotonic)
    decision: list[int] | None = None   # the ids approved; None: not decided, or cancelled
    cancelled: bool = False
    done: threading.Event = field(default_factory=threading.Event)

    @property
    def expired(self) -> bool:
        return time.monotonic() - self.created > LINK_LIFETIME_S


_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Paperboy review</title><style>
body{{font:16px system-ui,sans-serif;background:#f4f6f8;color:#1f2933;margin:0;padding:24px 12px}}
main{{background:#fff;padding:24px 28px;border-radius:12px;max-width:960px;margin:0 auto;
box-shadow:0 2px 12px #0001}}h1{{font-size:20px;margin:0 0 6px}}p{{color:#52606d;margin:0 0 16px}}
.item{{display:block;border:1px solid #e4e7eb;border-radius:10px;padding:12px 14px;margin:0 0 12px;
cursor:pointer}}.item:has(input:checked){{border-color:#d62828;background:#fff7f7}}
.head{{display:flex;gap:12px;align-items:center;flex-wrap:wrap}}
.head input{{width:20px;height:20px;margin:0}}.kind{{font-weight:600}}
.page{{color:#52606d;font-size:14px}}code{{font-size:15px;background:#f4f6f8;padding:2px 6px;
border-radius:4px;word-break:break-all}}img{{display:block;max-width:100%;margin-top:10px;
border:1px solid #e4e7eb;border-radius:6px}}.bar{{position:sticky;bottom:0;background:#fff;
padding:14px 0 0;display:flex;gap:12px;flex-wrap:wrap}}button{{padding:11px 18px;border:0;
border-radius:8px;font-size:16px;cursor:pointer}}.go{{background:#d62828;color:#fff}}
.stop{{background:#e4e7eb;color:#1f2933}}</style></head>
<body><main>{body}</main></body></html>"""


def _form(review: Review, base: str) -> str:
    items = "".join(
        f'<label class="item" data-id="{f["id"]}" data-kind="{html.escape(f["label"])}" '
        f'data-page="{f["page"]}"><span class="head">'
        f'<input type="checkbox" name="f" value="{f["id"]}" checked>'
        f'<span class="kind">{html.escape(f["label"])}</span>'
        f'<span class="page">page {f["page"]}</span>'
        f'<code>{html.escape(f["text"])}</code></span>'
        + (f'<img src="{base}/i/{f["id"]}" alt="Where it is on page {f["page"]}">'
           if f.get("snippet") else "")
        + "</label>" for f in review.findings)
    n = len(review.findings)
    return _PAGE.format(body=(
        f"<h1>Review what to redact</h1><p>Paperboy found {n} item{'s' if n != 1 else ''} "
        f"of personal data in {html.escape(review.label)}. Ticked items will be blacked "
        "out for good in a new copy; untick anything that should stay. Nothing is "
        "changed until you choose, and this list goes to Paperboy on this computer, not "
        "to the assistant.</p>"
        f'<form method="post" action="{base}">{items}<div class="bar">'
        '<button class="go" name="action" value="redact">Redact the ticked items</button>'
        '<button class="stop" name="action" value="cancel" formnovalidate>'
        "Cancel, redact nothing</button></div></form>"))


def _message(title: str, text: str) -> str:
    return _PAGE.format(body=f"<h1>{html.escape(title)}</h1><p>{html.escape(text)}</p>")


class ReviewPage:
    """The reviews waiting for the user, and the local server that shows them."""

    def __init__(self) -> None:
        self._reviews: dict[str, Review] = {}     # by token
        self._lock = threading.Lock()
        self._server: _Server | None = None

    def request(self, label: str, findings: list, purpose: tuple = (),
                waited: bool = False) -> Review:
        """A new one-time link to review *findings*, its decision only for *purpose*."""
        self._start()
        with self._lock:
            self._sweep()
            if len(self._reviews) >= MAX_PENDING:
                raise RuntimeError("too many reviews are waiting")
            review = Review(ref=secrets.token_urlsafe(9), token=secrets.token_urlsafe(32),
                            label=label, findings=findings, purpose=purpose, waited=waited)
            self._reviews[review.token] = review
        return review

    def url(self, review: Review) -> str:
        return f"http://127.0.0.1:{self.port}/r/{review.token}"

    def collect(self, review: Review) -> tuple[bool, list[int] | None]:
        """(decided, the ids approved or None if cancelled); the link is gone after."""
        with self._lock:
            self._reviews.pop(review.token, None)
            decided = review.done.is_set() and not review.expired
            approved = None if review.cancelled else review.decision
            review.findings = []
        return decided, approved if decided else None

    def waiting(self, ref: str, purpose: tuple) -> bool:
        """Whether the review with reference *ref*, for *purpose*, is still open."""
        with self._lock:
            self._sweep()
            review = next((r for r in self._reviews.values() if r.ref == ref), None)
            return (review is not None and not review.waited and not review.done.is_set()
                    and secrets.compare_digest(repr(review.purpose), repr(purpose)))

    def ready(self, ref: str, purpose: tuple) -> Review | None:
        """The review with reference *ref*, if the user has decided and it was asked
        for *purpose*. Reviews a call is waiting for can't be taken this way."""
        with self._lock:
            self._sweep()
            review = next((r for r in self._reviews.values() if r.ref == ref), None)
            if (review is None or review.waited or not review.done.is_set()
                    or not secrets.compare_digest(repr(review.purpose), repr(purpose))):
                return None
            self._reviews.pop(review.token)  # taken: a second call can't use it too
            return review

    def _sweep(self) -> None:
        for token in [t for t, r in self._reviews.items() if r.expired]:
            self._reviews.pop(token).findings = []

    def _sweeper(self) -> None:
        while True:
            time.sleep(SWEEP_EVERY_S)
            with self._lock:
                self._sweep()

    @property
    def port(self) -> int:
        assert self._server is not None
        return self._server.server_address[1]

    def _start(self) -> None:
        with self._lock:
            if self._server is not None:
                return
            page = self

            class Handler(BaseHTTPRequestHandler):
                timeout = SOCKET_TIMEOUT_S

                def log_message(self, *args):  # never log requests (they hold tokens)
                    pass

                def _send(self, status: int, body: bytes, kind: str) -> None:
                    self.send_response(status)
                    self.send_header("Content-Type", kind)
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("X-Frame-Options", "DENY")
                    self.send_header("X-Content-Type-Options", "nosniff")
                    self.send_header("Referrer-Policy", "no-referrer")
                    self.send_header("Content-Security-Policy",
                                     "default-src 'none'; style-src 'unsafe-inline'; "
                                     "img-src 'self'; form-action 'self'; "
                                     "frame-ancestors 'none'")
                    self.end_headers()
                    self.wfile.write(body)

                def _html(self, status: int, text: str) -> None:
                    self._send(status, text.encode(), "text/html; charset=utf-8")

                def _review(self) -> tuple[Review | None, str]:
                    """The review the path names, and the rest of the path."""
                    # Only this machine's own address: a page elsewhere can't reach the
                    # server through a DNS name that points here.
                    if self.headers.get("Host") != f"127.0.0.1:{page.port}":
                        return None, ""
                    if not self.path.startswith("/r/"):
                        return None, ""
                    token, _, rest = self.path[3:].partition("/")
                    with page._lock:
                        review = page._reviews.get(token)
                    if review is None or review.expired or review.done.is_set():
                        return None, ""
                    return review, rest

                def _invalid(self) -> None:
                    self._html(404, _message("Link not valid",
                                             "This review was finished, has expired, or "
                                             "the link is wrong. Ask for a new one."))

                def do_GET(self):  # noqa: N802 - the http.server API
                    review, rest = self._review()
                    if review is None:
                        self._invalid()
                    elif rest == "":
                        self._html(200, _form(review, f"/r/{review.token}"))
                    elif rest.startswith("i/") and rest[2:].isdigit():
                        found = next((f for f in review.findings
                                      if f["id"] == int(rest[2:]) and f.get("snippet")), None)
                        if found is None:
                            self._invalid()
                        else:
                            self._send(200, found["snippet"], "image/jpeg")
                    else:
                        self._invalid()

                def do_POST(self):  # noqa: N802 - the http.server API
                    review, rest = self._review()
                    try:
                        length = int(self.headers.get("Content-Length") or 0)
                    except ValueError:
                        length = -1
                    # A browser says where a form came from: only this page may submit it.
                    origin = self.headers.get("Origin")
                    if (review is None or rest or not 0 < length <= MAX_BODY
                            or origin not in (None, f"http://127.0.0.1:{page.port}")):
                        self._invalid()
                        return
                    fields = parse_qs(self.rfile.read(length).decode("utf-8", "replace"))
                    action = (fields.get("action") or [""])[0]
                    ids = {f["id"] for f in review.findings}
                    approved = sorted({int(v) for v in fields.get("f", [])
                                       if v.isascii() and v.isdigit() and int(v) in ids})
                    if action not in ("redact", "cancel"):
                        self._html(200, _form(review, f"/r/{review.token}"))
                        return
                    with page._lock:   # the first decision wins
                        taken = review.done.is_set() or review.token not in page._reviews
                        if not taken:
                            review.cancelled = action == "cancel"
                            review.decision = approved
                            review.done.set()
                    if taken:
                        self._invalid()
                    elif action == "cancel":
                        self._html(200, _message("Cancelled", "Nothing will be redacted. "
                                                              "You can close this tab."))
                    else:
                        n = len(approved)
                        self._html(200, _message("Done", (
                            f"Paperboy will redact the {n} ticked item"
                            f"{'s' if n != 1 else ''} in a new copy. You can close this "
                            "tab." if n else "Nothing was ticked, so nothing will be "
                            "redacted. You can close this tab.")))

            self._server = _Server(("127.0.0.1", 0), Handler)
            threading.Thread(target=self._server.serve_forever, daemon=True,
                             name="paperboy-review-page").start()
            threading.Thread(target=self._sweeper, daemon=True,
                             name="paperboy-review-sweeper").start()


PAGE = ReviewPage()
