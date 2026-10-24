"""A local page where the user types a password, so it never passes through the model.

The MCP specification forbids asking for passwords in a client form (form-mode
elicitation): the answer goes through the client, which may log or show it. Instead
the server serves a small page on 127.0.0.1 and gives the user its link (through
URL-mode elicitation where the client supports it, otherwise in the tool's reply).
What the user types there goes straight to this process.

Each link carries an unguessable token, works once, and expires; the server listens
on the loopback interface only, checks the Host header (so a web page can't reach it
through DNS rebinding), and keeps passwords in memory only until they are used or
expire. A password is bound to what it was asked for (the tool, the argument and the
file), so a reference to it can't be spent on anything else.
"""
from __future__ import annotations

import html
import secrets
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

LINK_LIFETIME_S = 600     # a link, and a password typed on it, expire after this
MAX_PENDING = 20          # links waiting at once
MAX_BODY = 4096           # bytes a form submission may have
MAX_PASSWORD = 1024       # characters
MAX_CONNECTIONS = 16      # requests the page server handles at once
SOCKET_TIMEOUT_S = 10     # an idle connection is dropped after this
SWEEP_EVERY_S = 30        # how often expired links and passwords are forgotten


@dataclass
class Entry:
    ref: str              # a short reference the model may see and pass back
    token: str            # the secret part of the link
    label: str            # what the password is for, as shown on the page
    new: bool             # choosing a new password: typed twice
    purpose: tuple = ()   # what the password may be used for
    waited: bool = False  # the call that asked waits for it; it can't be taken by ref
    created: float = field(default_factory=time.monotonic)
    value: str | None = None
    done: threading.Event = field(default_factory=threading.Event)

    @property
    def expired(self) -> bool:
        return time.monotonic() - self.created > LINK_LIFETIME_S


_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Paperboy password</title><style>
body{{font:16px system-ui,sans-serif;background:#f4f6f8;color:#1f2933;margin:0;
display:flex;min-height:100vh;align-items:center;justify-content:center}}
main{{background:#fff;padding:28px 32px;border-radius:12px;max-width:420px;width:90%;
box-shadow:0 2px 12px #0001}}h1{{font-size:18px;margin:0 0 6px}}p{{color:#52606d;margin:0 0 18px}}
label{{display:block;font-size:14px;margin:12px 0 4px}}
input{{width:100%;box-sizing:border-box;padding:10px;border:1px solid #cbd2d9;border-radius:8px;
font-size:16px}}button{{margin-top:18px;width:100%;padding:11px;border:0;border-radius:8px;
background:#2f6fde;color:#fff;font-size:16px}}.err{{color:#c0392b}}</style></head>
<body><main>{body}</main></body></html>"""


def _form(entry: Entry, error: str = "") -> str:
    second = ('<label for="c">Type it again</label>'
              '<input id="c" name="confirm" type="password" required '
              'autocomplete="new-password">') if entry.new else ""
    title = "Choose a password" if entry.new else "Enter the password"
    return _PAGE.format(body=(
        f"<h1>{title}</h1><p>for {html.escape(entry.label)}. It goes to Paperboy on this "
        "computer, not to the assistant.</p>"
        + (f'<p class="err">{html.escape(error)}</p>' if error else "")
        + '<form method="post"><label for="p">Password</label>'
        f'<input id="p" name="password" type="password" required autofocus '
        f'autocomplete="{"new-password" if entry.new else "current-password"}">'
        f"{second}<button>Continue</button></form>"))


def _message(title: str, text: str) -> str:
    return _PAGE.format(body=f"<h1>{html.escape(title)}</h1><p>{html.escape(text)}</p>")


class PasswordPage:
    """The pending links, and the local server that answers them."""

    def __init__(self) -> None:
        self._entries: dict[str, Entry] = {}      # by token
        self._lock = threading.Lock()
        self._server: ThreadingHTTPServer | None = None

    # --- links ------------------------------------------------------------------------

    def request(self, label: str, new: bool, purpose: tuple = (),
                waited: bool = False) -> Entry:
        """A new one-time link for a password, to be used only for *purpose*."""
        self._start()
        with self._lock:
            self._sweep()
            if len(self._entries) >= MAX_PENDING:
                raise RuntimeError("too many password pages are waiting")
            entry = Entry(ref=secrets.token_urlsafe(9), token=secrets.token_urlsafe(32),
                          label=label, new=new, purpose=purpose, waited=waited)
            self._entries[entry.token] = entry
        return entry

    def url(self, entry: Entry) -> str:
        return f"http://127.0.0.1:{self.port}/p/{entry.token}"

    def wait(self, entry: Entry, timeout: float) -> str | None:
        """The password typed on *entry*'s page, waiting up to *timeout* seconds."""
        entry.done.wait(timeout)
        return self.collect(entry)

    def collect(self, entry: Entry) -> str | None:
        """The password typed on *entry*'s page, if it has been; the link is gone after."""
        with self._lock:
            self._entries.pop(entry.token, None)
            value, entry.value = entry.value, None
        return None if entry.expired else value

    def discard(self, entry: Entry) -> None:
        self.collect(entry)

    def ready(self, ref: str, purpose: tuple) -> Entry | None:
        """The entry with reference *ref*, if its password has been typed and was asked
        for *purpose*. Entries a call is waiting for can't be taken this way."""
        with self._lock:
            self._sweep()
            entry = next((e for e in self._entries.values() if e.ref == ref), None)
            if (entry is None or entry.waited or entry.value is None
                    or not secrets.compare_digest(repr(entry.purpose), repr(purpose))):
                return None
            return entry

    def _sweep(self) -> None:
        for token in [t for t, e in self._entries.items() if e.expired]:
            self._entries.pop(token).value = None

    def _sweeper(self) -> None:
        while True:
            time.sleep(SWEEP_EVERY_S)
            with self._lock:
                self._sweep()

    # --- the server -------------------------------------------------------------------

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

                def _send(self, status: int, body: str) -> None:
                    data = body.encode()
                    self.send_response(status)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("X-Frame-Options", "DENY")
                    self.send_header("Referrer-Policy", "no-referrer")
                    self.send_header("Content-Security-Policy",
                                     "default-src 'none'; style-src 'unsafe-inline'; "
                                     "form-action 'self'; frame-ancestors 'none'")
                    self.end_headers()
                    self.wfile.write(data)

                def _entry(self) -> Entry | None:
                    # Only this machine's own address: a page elsewhere can't reach the
                    # server through a DNS name that points here.
                    if self.headers.get("Host") != f"127.0.0.1:{page.port}":
                        return None
                    if not self.path.startswith("/p/"):
                        return None
                    with page._lock:
                        entry = page._entries.get(self.path[3:])
                    if entry is None or entry.expired or entry.done.is_set():
                        return None
                    return entry

                def _invalid(self) -> None:
                    self._send(404, _message("Link not valid",
                                             "This link was used, has expired, or is "
                                             "wrong. Ask for a new one."))

                def do_GET(self):  # noqa: N802 - the http.server API
                    entry = self._entry()
                    if entry is None:
                        self._invalid()
                        return
                    self._send(200, _form(entry))

                def do_POST(self):  # noqa: N802 - the http.server API
                    entry = self._entry()
                    try:
                        length = int(self.headers.get("Content-Length") or 0)
                    except ValueError:
                        length = -1
                    if entry is None or not 0 < length <= MAX_BODY:
                        self._invalid()
                        return
                    fields = parse_qs(self.rfile.read(length).decode("utf-8", "replace"))
                    password = (fields.get("password") or [""])[0]
                    if not password or len(password) > MAX_PASSWORD:
                        self._send(200, _form(entry, "Type a password."))
                        return
                    if entry.new and (fields.get("confirm") or [""])[0] != password:
                        self._send(200, _form(entry, "The two passwords didn't match."))
                        return
                    with page._lock:   # the first submission wins
                        taken = entry.done.is_set() or entry.token not in page._entries
                        if not taken:
                            entry.value = password
                            entry.done.set()
                    if taken:
                        self._invalid()
                        return
                    self._send(200, _message("Done", "Paperboy has the password. You can "
                                                     "close this tab."))

            self._server = _Server(("127.0.0.1", 0), Handler)
            threading.Thread(target=self._server.serve_forever, daemon=True,
                             name="paperboy-password-page").start()
            threading.Thread(target=self._sweeper, daemon=True,
                             name="paperboy-password-sweeper").start()


class _Server(ThreadingHTTPServer):
    """At most MAX_CONNECTIONS requests at once; more are closed straight away."""

    daemon_threads = True

    def __init__(self, *args) -> None:
        super().__init__(*args)
        self._slots = threading.BoundedSemaphore(MAX_CONNECTIONS)

    def process_request(self, request, client_address) -> None:
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


PAGE = PasswordPage()
