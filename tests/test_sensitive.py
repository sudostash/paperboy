"""Personal data: finding it, the user's review of each item, and redacting what they
approve (find_sensitive_data, redact_sensitive_data, review_page)."""
from __future__ import annotations

import http.client
import io
import re
import time
import urllib.parse
from types import SimpleNamespace

import anyio
import pikepdf
import pytest
from PIL import Image
from reportlab.pdfgen import canvas

import paperboy.server as S
from paperboy import review_page as R
from paperboy.engine import sensitive as E
from paperboy.errors import PdfUserError

from conftest import page_labels

LINES = [
    "Name: Jo Bloggs   Email: jo.bloggs@example.com",
    "Phone: +44 20 7946 0958   Mobile: 07700 900123   US: 415-555-0132",
    "Card: 4111 1111 1111 1111   Bad card: 4111 1111 1111 1112",
    "IBAN: GB29 NWBK 6016 1331 9268 19   Ref: 2025-11-16",
    "SSN: 123-45-6789   bad SSN 000-12-3456   NI: AB 12 34 56 C",
    "Date of birth: 12/03/1980   Invoice date: 01/02/2024   Born 4 July 1975",
    "Amount 1 234 567 and order 2024 1234 5678 and zip 94105",
]


def _pdf(path, lines=LINES, pages=1):
    c = canvas.Canvas(str(path))
    for _ in range(pages):
        for i, text in enumerate(lines):
            c.drawString(40, 760 - 22 * i, text)
        c.showPage()
    c.save()
    return path


@pytest.fixture(autouse=True)
def sandbox(settings, monkeypatch):
    monkeypatch.setattr(S, "SETTINGS", settings)
    return settings


@pytest.fixture
def doc(tmp_path):
    return _pdf(tmp_path / "statement.pdf")


def _texts(found):
    return [(f["kind"], f["text"]) for f in found["findings"]]


# --- finding ---------------------------------------------------------------------------


def test_each_kind_is_found_and_look_alikes_are_not(doc, settings):
    found = E.detect(doc, settings, texts=["Jo Bloggs"])
    assert _texts(found) == [
        ("custom", "Jo Bloggs"), ("email", "jo.bloggs@example.com"),
        ("phone", "+44 20 7946 0958"), ("phone", "07700 900123"),
        ("phone", "415-555-0132"), ("card_number", "4111 1111 1111 1111"),
        ("iban", "GB29 NWBK 6016 1331 9268 19"), ("us_ssn", "123-45-6789"),
        ("uk_ni_number", "AB 12 34 56 C"), ("date_of_birth", "12/03/1980"),
        ("date_of_birth", "4 July 1975")]
    assert [f["id"] for f in found["findings"]] == list(range(1, 12))
    assert all(f["page"] == 1 and f["rects"] for f in found["findings"])
    assert found["pages_searched"] == 1 and found["pages_without_text"] == []
    assert len(found["digest"]) == 64 and "snippet" not in found["findings"][0]


def test_kinds_narrow_the_search(doc, settings):
    found = E.detect(doc, settings, kinds=["email", "us_ssn", "email"])
    assert _texts(found) == [("email", "jo.bloggs@example.com"),
                             ("us_ssn", "123-45-6789")]
    assert found["kinds"] == ["email", "us_ssn"]


@pytest.mark.parametrize("kwargs, message", [
    ({"kinds": []}, "non-empty list"),
    ({"kinds": "email"}, "non-empty list"),
    ({"kinds": ["passport"]}, "Unknown kind 'passport'"),
    ({"texts": "Jo"}, "texts must be a list"),
    ({"texts": [f"t{i}" for i in range(51)]}, "at most 50 texts"),
])
def test_bad_searches_are_refused(doc, settings, kwargs, message):
    with pytest.raises(PdfUserError, match=message):
        E.detect(doc, settings, **kwargs)


@pytest.mark.parametrize("kind, text, real", [
    ("card_number", "4111 1111 1111 1111", True),
    ("card_number", "4111 1111 1111 1112", False),       # check digit
    ("card_number", "0000 0000 0000 0000", False),       # passes, but isn't a card
    ("card_number", "4111 1111 111", False),             # too short
    ("iban", "GB29 NWBK 6016 1331 9268 19", True),
    ("iban", "GB29 NWBK 6016 1331 9268 18", False),
    ("iban", "gb29 nwbk 6016 1331 9268 19", False),      # not written as an IBAN is
    ("us_ssn", "123-45-6789", True),
    ("us_ssn", "666-45-6789", False), ("us_ssn", "912-45-6789", False),
    ("us_ssn", "123-00-6789", False), ("us_ssn", "123-45-0000", False),
    ("uk_ni_number", "AB 12 34 56 C", True), ("uk_ni_number", "ab 12 34 56 c", False),
    ("uk_ni_number", "GB 12 34 56 C", False), ("uk_ni_number", "DA 12 34 56 C", False),
    ("uk_ni_number", "AO 12 34 56 C", False),
    ("phone", "+44 20 7946 0958", True), ("phone", "(020) 7946 0958", True),
    ("phone", "415-555-0132", True), ("phone", "12-345", False),
    ("phone", "2024 1234 5678", False), ("phone", "01/02/2024", False),
    ("phone", "0123-45-6789012345678", False),
    ("email", "a@b.co", True),
])
def test_matches_are_checked_as_the_data_itself_is(kind, text, real):
    assert E._real(kind, text) is real


@pytest.mark.parametrize("kind, text, shown", [
    ("email", "jo.bloggs@example.com", "j***@e***.com"),
    ("card_number", "4111 1111 1111 1111", "***1111"),
    ("iban", "GB29 NWBK 6016 1331 9268 19", "***6819"),
    ("phone", "+44 20 7946 0958", "***0958"),
    ("us_ssn", "123-45-6789", "1***"),
    ("custom", "J", "***"),
])
def test_the_assistant_sees_findings_masked(kind, text, shown):
    assert E.mask(kind, text) == shown


def test_text_in_form_fields_is_found(tmp_path, settings):
    path = tmp_path / "form.pdf"
    c = canvas.Canvas(str(path))
    c.acroForm.textfield(name="email", value="jo@example.com", x=72, y=700, width=200,
                         height=20)
    c.showPage()
    c.save()
    assert _texts(E.detect(path, settings)) == [("email", "jo@example.com")]


def test_a_scan_has_no_text_to_search(tmp_path, settings):
    picture = tmp_path / "scan.png"
    Image.new("RGB", (200, 100), "white").save(picture)
    path = tmp_path / "scan.pdf"
    c = canvas.Canvas(str(path))
    c.drawImage(str(picture), 0, 0)
    c.showPage()
    c.save()
    found = E.detect(path, settings)
    assert found["findings"] == [] and found["pages_without_text"] == [1]


def test_pages_limit_where_it_looks(tmp_path, settings):
    path = _pdf(tmp_path / "two.pdf", LINES[:1], pages=2)
    found = E.detect(path, settings, kinds=["email"], pages="2")
    assert [f["page"] for f in found["findings"]] == [2]
    assert found["pages_searched"] == 1


def test_a_pdf_that_forbids_copying_needs_its_owner_password(doc, tmp_path, settings):
    locked = tmp_path / "locked.pdf"
    with pikepdf.open(doc) as pdf:
        pdf.save(locked, encryption=pikepdf.Encryption(
            user="", owner="boss", R=6, allow=pikepdf.Permissions(extract=False)))
    with pytest.raises(PdfUserError, match="does not allow copying"):
        E.detect(locked, settings)
    assert len(E.detect(locked, settings, password="boss")["findings"]) == 10


def test_limits_on_content_and_findings(doc, settings, monkeypatch):
    monkeypatch.setattr(E, "MAX_FINDINGS", 3)
    with pytest.raises(PdfUserError, match="Over 3 items found"):
        E.detect(doc, settings)
    monkeypatch.setattr(E, "MAX_FINDINGS", 2)     # too many even to compare
    with pytest.raises(PdfUserError, match="Over 2 items found"):
        E.detect(doc, settings)
    monkeypatch.setattr(E, "MAX_PAGE_CONTENT", 10)
    with pytest.raises(PdfUserError, match="Pages 1 have too much content"):
        E.detect(doc, settings)


def test_snippets_show_each_finding_on_its_line(doc, settings):
    found = E.detect(doc, settings, kinds=["email", "us_ssn"], snippets=True)
    for f in found["findings"]:
        image = Image.open(io.BytesIO(f["snippet"]))
        assert image.format == "JPEG" and image.width <= E.SNIPPET_WIDTH
        assert 30 < image.height < 200          # its line and a little around it
        red = sum(n for n, p in image.convert("RGB").getcolors(1 << 24)
                  if p[0] > 180 and p[1] < 90 and p[2] < 90)
        assert red > 50                         # the finding is marked


def test_snippets_of_a_wide_page_are_scaled_down(tmp_path, settings):
    path = tmp_path / "wide.pdf"
    c = canvas.Canvas(str(path), pagesize=(2000, 400))
    c.drawString(40, 200, "Email: jo@example.com")
    c.showPage()
    c.save()
    snippet = E.detect(path, settings, snippets=True)["findings"][0]["snippet"]
    assert Image.open(io.BytesIO(snippet)).width == E.SNIPPET_WIDTH


def test_preparing_snippets_is_time_limited(doc, settings):
    found = E.detect(doc, settings, kinds=["email"])
    doc_handle = E._open(doc.read_bytes())
    try:
        with pytest.raises(PdfUserError, match="Preparing the review took longer"):
            E._snippets(doc_handle, found["findings"], time.monotonic() - 1)
    finally:
        E._close(doc_handle)


def test_find_sensitive_data_reports_findings_masked(doc):
    res = S.find_sensitive_data(str(doc), texts=["Jo Bloggs"])
    assert res["count"] == 11 and res["path"] == str(doc)
    assert res["by_kind"] == {"custom": 1, "email": 1, "phone": 3, "card_number": 1,
                              "iban": 1, "us_ssn": 1, "uk_ni_number": 1,
                              "date_of_birth": 2}
    assert res["findings"][1] == {"id": 2, "kind": "email", "page": 1,
                                  "preview": "j***@e***.com"}
    shown = repr(res)
    for secret in ("jo.bloggs@example.com", "4111 1111 1111 1111", "123-45-6789"):
        assert secret not in shown
    assert "Names, postal addresses" in res["limits"]


# --- the review page ---------------------------------------------------------------------


def _request(url, method="GET", fields=None, host=None):
    """A browser on this computer: (status, body bytes, headers)."""
    parts = urllib.parse.urlsplit(url)
    conn = http.client.HTTPConnection(parts.hostname, parts.port, timeout=10)
    body = urllib.parse.urlencode(fields, doseq=True).encode() if fields is not None \
        else None
    headers = {"Host": host or parts.netloc}
    if body is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    conn.request(method, parts.path, body=body, headers=headers)
    response = conn.getresponse()
    result = response.status, response.read(), dict(response.getheaders())
    conn.close()
    return result


FINDINGS = [{"id": 1, "label": "Email address", "page": 1, "text": "a@<b>.com",
             "snippet": b"\xff\xd8jpeg"},
            {"id": 2, "label": "Card number", "page": 2, "text": "4111", "snippet": None}]


def _review(**kw):
    return R.PAGE.request("“x.pdf”", [dict(f) for f in FINDINGS], ("p",), **kw)


def test_the_page_lists_each_item_with_where_it_is():
    review = _review()
    url = R.PAGE.url(review)
    status, body, headers = _request(url)
    page = body.decode()
    assert status == 200 and "found 2 items" in page and "a@&lt;b&gt;.com" in page
    assert page.count('type="checkbox"') == 2 and page.count(" checked>") == 2
    assert f'src="/r/{review.token}/i/1"' in page and "/i/2" not in page
    assert "img-src 'self'" in headers["Content-Security-Policy"]
    assert headers["Cache-Control"] == "no-store"
    status, body, headers = _request(url + "/i/1")
    assert (status, body, headers["Content-Type"]) == (200, b"\xff\xd8jpeg", "image/jpeg")
    for bad in ("/i/2", "/i/9", "/i/x", "/other"):
        assert _request(url + bad)[0] == 404
    assert _request(url, host="evil.example")[0] == 404
    assert _request(url.replace(review.token, "nope"))[0] == 404
    assert _request(url.replace("/r/", "/p/"))[0] == 404


def test_a_decision_is_taken_once_and_bound_to_its_purpose():
    review = _review()
    url = R.PAGE.url(review)
    assert R.PAGE.ready(review.ref, ("p",)) is None              # not decided yet
    status, body, _ = _request(url, "POST", {"action": "redact", "f": ["2", "7", "x"]})
    assert status == 200 and b"redact the 1 ticked item " in body
    assert _request(url, "POST", {"action": "cancel"})[0] == 404  # the first one counts
    assert _request(url)[0] == 404
    assert R.PAGE.ready(review.ref, ("other",)) is None
    assert R.PAGE.ready(review.ref, ("p",)) is review
    assert R.PAGE.collect(review) == (True, [2])
    assert R.PAGE.ready(review.ref, ("p",)) is None              # used


def test_cancelling_or_ticking_nothing():
    review = _review()
    body = _request(R.PAGE.url(review), "POST", {"action": "cancel", "f": "1"})[1]
    assert b"Nothing will be redacted" in body
    assert R.PAGE.collect(review) == (True, None)
    review = _review()
    body = _request(R.PAGE.url(review), "POST", {"action": "redact"})[1]
    assert b"Nothing was ticked" in body
    assert R.PAGE.collect(review) == (True, [])


def test_bad_submissions_change_nothing(monkeypatch):
    review = _review()
    url = R.PAGE.url(review)
    assert b"Review what to redact" in _request(url, "POST", {"action": "other"})[1]
    assert _request(url, "POST", {"action": "redact", "x": "y" * R.MAX_BODY})[0] == 404
    assert _request(url + "/i/1", "POST", {"action": "redact"})[0] == 404
    parts = urllib.parse.urlsplit(url)
    conn = http.client.HTTPConnection(parts.hostname, parts.port, timeout=10)
    conn.request("POST", parts.path, headers={"Host": parts.netloc,
                                              "Content-Length": "abc"})
    assert conn.getresponse().status == 404
    conn.close()
    assert not review.done.is_set()


def test_a_review_waited_for_cant_be_taken_by_reference():
    review = _review(waited=True)
    _request(R.PAGE.url(review), "POST", {"action": "redact", "f": "1"})
    assert R.PAGE.ready(review.ref, ("p",)) is None
    assert R.PAGE.collect(review) == (True, [1])


def test_reviews_expire_and_are_limited(monkeypatch):
    review = _review()
    _request(R.PAGE.url(review), "POST", {"action": "redact", "f": "1"})
    monkeypatch.setattr(R, "LINK_LIFETIME_S", -1)
    assert R.PAGE.collect(review) == (False, None)           # decided too late
    stale = _review()
    assert _request(R.PAGE.url(stale))[0] == 404
    assert R.PAGE.ready(stale.ref, ("p",)) is None           # swept
    assert stale.findings == []
    monkeypatch.setattr(R, "LINK_LIFETIME_S", 1800)
    monkeypatch.setattr(R, "MAX_PENDING", 0)
    with pytest.raises(RuntimeError, match="too many reviews"):
        _review()


def test_the_sweeper_forgets_expired_reviews(monkeypatch):
    review = _review()
    monkeypatch.setattr(R, "LINK_LIFETIME_S", -1)
    calls = []

    def sleep(seconds):
        if calls:
            raise SystemExit
        calls.append(seconds)
    monkeypatch.setattr(R.time, "sleep", sleep)
    with pytest.raises(SystemExit):
        R.PAGE._sweeper()
    assert review.token not in R.PAGE._reviews


# --- the whole flow ---------------------------------------------------------------------


class _Ctx:
    """The client: whether it can open links, and what the user does on the page."""

    def __init__(self, url_mode=True, action="accept", decide=None):
        self.opened, self.completed = [], []
        self.action, self.decide = action, decide
        outer = self

        class Session:
            client_params = SimpleNamespace(capabilities=SimpleNamespace(
                elicitation=SimpleNamespace(form=object(),
                                            url=object() if url_mode else None)))

            async def send_elicit_complete(self, ref):
                outer.completed.append(ref)
        self.session = Session()

    async def elicit_url(self, message, url, elicitation_id):
        self.opened.append((message, url))
        if isinstance(self.decide, Exception):
            raise self.decide
        if callable(self.decide):
            await anyio.to_thread.run_sync(self.decide, url)
        return SimpleNamespace(action=self.action)


def _ticks(*labels, action="redact"):
    """The user ticks only the items whose text is in *labels*."""
    def decide(url):
        page = _request(url)[1].decode()
        rows = re.findall(r'value="(\d+)".*?<code>(.*?)</code>', page)
        _request(url, "POST", {"action": action,
                               "f": [i for i, text in rows if text in labels]})
    return decide


async def _run(ctx, **kwargs):
    return await S._asking(S.redact_sensitive_data)(paperboy_context=ctx, **kwargs)


async def test_only_what_the_user_ticks_is_redacted(doc):
    ctx = _Ctx(decide=_ticks("jo.bloggs@example.com", "4111 1111 1111 1111"))
    res = await _run(ctx, input_path=str(doc))
    assert res["status"] == "redacted" and (res["found"], res["skipped"]) == (10, 8)
    assert [a["preview"] for a in res["approved"]] == ["j***@e***.com", "***1111"]
    out = doc.with_name("statement_redacted.pdf")
    assert res["output"] == str(out) and res["redaction"]["output"] == str(out)
    text = page_labels(out)[0]
    assert "jo.bloggs@example.com" not in text and "4111 1111 1111 1111" not in text
    assert "123-45-6789" in text and "Jo Bloggs" in text     # left, as the user chose
    assert "jo.bloggs@example.com" in page_labels(doc)[0]    # the input is unchanged
    message, url = ctx.opened[0]
    assert "found 10 items of personal data in “statement.pdf”" in message
    assert str(doc.parent) not in message and len(ctx.completed) == 1
    assert _request(url)[0] == 404                            # the link is gone


async def test_texts_the_assistant_names_are_reviewed_too(doc):
    ctx = _Ctx(decide=_ticks("Jo Bloggs"))
    res = await _run(ctx, input_path=str(doc), texts=["Jo Bloggs"], kinds=["email"],
                     output_path=str(doc.with_name("out.pdf")), fill="white")
    assert [a["kind"] for a in res["approved"]] == ["custom"] and res["skipped"] == 1
    assert "Jo Bloggs" not in page_labels(doc.with_name("out.pdf"))[0]


async def test_cancelling_or_ticking_nothing_writes_nothing(doc):
    for decide, status in ((_ticks(action="cancel"), "cancelled"),
                           (_ticks(), "none_approved")):
        res = await _run(_Ctx(decide=decide), input_path=str(doc))
        assert (res["status"], res["output"], res["redaction"]) == (status, None, None)
        assert res["approved"] == [] and res["skipped"] == 10
    assert not doc.with_name("statement_redacted.pdf").exists()


async def test_nothing_found_asks_nothing(tmp_path):
    plain = _pdf(tmp_path / "plain.pdf", ["Nothing personal here."])
    ctx = _Ctx()
    res = await _run(ctx, input_path=str(plain))
    assert res["status"] == "nothing_found" and res["found"] == 0
    assert ctx.opened == [] and not tmp_path.joinpath("plain_redacted.pdf").exists()


@pytest.fixture
def browser(monkeypatch):
    """The browser on this computer: the pages Paperboy opened in it."""
    opened = []

    def open_page(url):
        opened.append(url)
        return True
    monkeypatch.setattr(S, "_open_in_browser", open_page)
    return opened


async def test_without_links_in_the_client_paperboy_opens_the_page_itself(doc, browser):
    args = {"input_path": str(doc), "kinds": ["email", "us_ssn"]}
    with pytest.raises(S.ToolError) as caught:
        await _run(_Ctx(url_mode=False), **args)
    message = str(caught.value)
    assert "Paperboy opened a review page in the browser on this computer" in message
    assert "listing the 2 items found in \u201cstatement.pdf\u201d" in message
    assert "127.0.0.1" not in message and "/r/" not in message   # the model never has it
    url = browser[0]
    assert R.PAGE.url(next(iter(R.PAGE._reviews.values()))) == url
    ref = re.search(r'review="review:([^"]+)"', message).group(1)
    for bad in ("review:nope", f"ask:{ref}"):
        with pytest.raises(S.ToolError):
            await _run(_Ctx(url_mode=False), **args, review=bad)
    with pytest.raises(S.ToolError, match="hasn't finished the review yet"):
        await _run(_Ctx(url_mode=False), **args, review=f"review:{ref}")
    await anyio.to_thread.run_sync(_ticks("123-45-6789"), url)
    with pytest.raises(S.ToolError, match="was for something else"):  # another search
        await _run(_Ctx(url_mode=False), input_path=str(doc), kinds=["us_ssn"],
                   review=f"review:{ref}")
    res = await _run(_Ctx(url_mode=False), **args, review=f"review:{ref}")
    assert res["status"] == "redacted" and [a["kind"] for a in res["approved"]] == ["us_ssn"]
    assert "123-45-6789" not in page_labels(doc.with_name("statement_redacted.pdf"))[0]
    with pytest.raises(S.ToolError, match="already used"):       # a decision is used once
        await _run(_Ctx(url_mode=False), **args, review=f"review:{ref}", overwrite=True)


async def test_a_review_that_expires_after_it_was_taken_does_nothing(doc, browser,
                                                                     monkeypatch):
    args = {"input_path": str(doc), "kinds": ["us_ssn"]}
    with pytest.raises(S.ToolError) as caught:
        await _run(_Ctx(url_mode=False), **args)
    ref = re.search(r'review="review:([^"]+)"', str(caught.value)).group(1)
    await anyio.to_thread.run_sync(_ticks("123-45-6789"), browser[0])
    real = R.PAGE.ready

    def late(*a):
        review = real(*a)
        monkeypatch.setattr(R, "LINK_LIFETIME_S", -1)   # it expires just after
        return review
    monkeypatch.setattr(R.PAGE, "ready", late)
    with pytest.raises(S.ToolError, match="That review expired"):
        await _run(_Ctx(url_mode=False), **args, review=f"review:{ref}")


async def test_without_a_browser_the_link_is_never_given_to_the_assistant(doc, monkeypatch):
    monkeypatch.setattr(S, "_open_in_browser", lambda url: False)
    with pytest.raises(S.ToolError, match="couldn't open a browser") as caught:
        await _run(_Ctx(url_mode=False), input_path=str(doc))
    assert "127.0.0.1" not in str(caught.value)
    assert all(r.waited for r in R.PAGE._reviews.values())  # its review was dropped


def test_the_page_opens_with_the_systems_own_opener_detached(monkeypatch):
    launched = []

    class Popen:
        def __init__(self, args, **kwargs):
            launched.append((args, kwargs))
    monkeypatch.setattr(S.subprocess, "Popen", Popen)
    monkeypatch.setattr(S.shutil, "which", lambda name: f"/usr/bin/{name}")
    for platform, opener in (("darwin", "open"), ("linux", "xdg-open")):
        monkeypatch.setattr(S.sys, "platform", platform)
        assert S._open_in_browser("http://127.0.0.1:1/r/t") is True
        args, kwargs = launched.pop()
        assert args == [f"/usr/bin/{opener}", "http://127.0.0.1:1/r/t"]
        # Never on the server's own stdin and stdout: they carry the protocol.
        assert kwargs["stdin"] == kwargs["stdout"] == kwargs["stderr"] == S.subprocess.DEVNULL
        assert kwargs["start_new_session"] is True
    monkeypatch.setattr(S.shutil, "which", lambda name: None)       # no desktop
    assert S._open_in_browser("http://127.0.0.1:1/r/t") is False

    def fails(*args, **kwargs):
        raise OSError("cannot run")
    monkeypatch.setattr(S.shutil, "which", lambda name: "/usr/bin/x")
    monkeypatch.setattr(S.subprocess, "Popen", fails)
    assert S._open_in_browser("http://127.0.0.1:1/r/t") is False
    started = []
    monkeypatch.setattr(S.os, "name", "nt")
    monkeypatch.setattr(S.os, "startfile", started.append, raising=False)
    assert S._open_in_browser("http://127.0.0.1:1/r/t") is True
    assert started == ["http://127.0.0.1:1/r/t"]


async def test_a_file_changed_after_its_review_is_not_redacted(doc):
    def decide(url):
        _ticks("123-45-6789")(url)
        _pdf(doc, LINES[:5])                       # replaced while the user reviewed
    with pytest.raises(S.ToolError, match="changed after it was reviewed"):
        await _run(_Ctx(decide=decide), input_path=str(doc))
    assert not doc.with_name("statement_redacted.pdf").exists()


async def test_a_review_refused_failed_or_not_finished_does_nothing(doc, monkeypatch):
    with pytest.raises(S.ToolError, match="wasn't opened"):
        await _run(_Ctx(action="decline"), input_path=str(doc))
    with pytest.raises(S.ToolError, match="couldn't open the review page"):
        await _run(_Ctx(decide=RuntimeError("no browser")), input_path=str(doc))
    monkeypatch.setattr(S, "REVIEW_WAIT_S", 0.3)
    with pytest.raises(S.ToolError, match="wasn't finished in time"):
        await _run(_Ctx(), input_path=str(doc))
    assert not doc.with_name("statement_redacted.pdf").exists()


async def test_the_call_is_checked_before_anyone_reviews(doc):
    doc.with_name("statement_redacted.pdf").write_bytes(b"x")
    ctx = _Ctx()
    with pytest.raises(S.ToolError, match="already exists"):
        await _run(ctx, input_path=str(doc))
    with pytest.raises(S.ToolError, match="Unknown kind|not found|outside"):
        await _run(ctx, input_path=str(doc.with_name("missing.pdf")))
    assert ctx.opened == []


async def test_the_review_page_must_be_available(doc, monkeypatch):
    def full(*args, **kwargs):
        raise RuntimeError("too many")
    monkeypatch.setattr(R.PAGE, "request", full)
    with pytest.raises(S.ToolError, match="Too many reviews are waiting"):
        await _run(_Ctx(), input_path=str(doc))

    def broken(*args, **kwargs):
        raise OSError("no port")
    monkeypatch.setattr(R.PAGE, "request", broken)
    with pytest.raises(S.ToolError, match="couldn't start"):
        await _run(_Ctx(), input_path=str(doc))


def test_the_tool_never_runs_without_a_review(doc):
    with pytest.raises(S.ToolError, match="needs the user's review"):
        S.redact_sensitive_data(str(doc))


async def test_a_locked_pdf_is_reviewed_and_stays_locked(doc, tmp_path):
    locked = tmp_path / "locked.pdf"
    with pikepdf.open(doc) as pdf:
        pdf.save(locked, encryption=pikepdf.Encryption(user="pw", owner="pw", R=6))
    res = await _run(_Ctx(decide=_ticks("123-45-6789")), input_path=str(locked),
                     password="pw")
    assert res["status"] == "redacted" and res["redaction"]["password_protected"] is True
    assert "123-45-6789" not in page_labels(tmp_path / "locked_redacted.pdf", "pw")[0]


def test_a_finding_the_assistant_named_isnt_listed_twice(doc, settings):
    found = E.detect(doc, settings, kinds=["email"], texts=["jo.bloggs@example.com"])
    assert _texts(found) == [("custom", "jo.bloggs@example.com")]


def test_a_decision_racing_another_is_refused(monkeypatch):
    review = _review()
    real = R.parse_qs

    def meanwhile(body):
        R.PAGE.collect(review)                 # e.g. the call gave up just now
        return real(body)
    monkeypatch.setattr(R, "parse_qs", meanwhile)
    assert _request(R.PAGE.url(review), "POST", {"action": "redact", "f": "1"})[0] == 404
    assert not review.done.is_set()


async def test_a_client_that_cant_take_the_completion_notice(doc):
    ctx = _Ctx(decide=_ticks("123-45-6789"))

    async def refuse(ref):
        raise RuntimeError("not supported")
    ctx.session.send_elicit_complete = refuse
    res = await _run(ctx, input_path=str(doc))
    assert res["status"] == "redacted"


async def test_over_stdio_the_user_reviews_and_the_result_keeps_its_contract(tmp_path):
    import json
    import os
    import sys

    import jsonschema
    from mcp import ClientSession, StdioServerParameters, types
    from mcp.client.stdio import stdio_client

    doc = _pdf(tmp_path / "statement.pdf")
    opened = tmp_path / "opened.txt"   # the browser Paperboy opens, for an older SDK
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / ("open" if sys.platform == "darwin" else "xdg-open")
    script.write_text(f'#!/bin/sh\necho "$1" >> {opened}\n')
    script.chmod(0o755)
    env = dict(os.environ, PAPERBOY_ROOT=str(tmp_path),
               PATH=str(bin_dir) + os.pathsep + os.environ.get("PATH", ""))
    src = os.path.join(os.path.dirname(os.path.dirname(__file__)), "src")
    env["PYTHONPATH"] = src + os.pathsep + env.get("PYTHONPATH", "")
    decide = _ticks("123-45-6789")

    async def user(context, params):          # the client opens the page for the user
        await anyio.to_thread.run_sync(decide, params.url)
        return types.ElicitResult(action="accept")
    params = StdioServerParameters(command=sys.executable, args=["-m", "paperboy"], env=env)
    args = {"input_path": str(doc), "kinds": ["us_ssn", "email"]}
    async with stdio_client(params) as (r, w), \
            ClientSession(r, w, elicitation_callback=user) as s:
        await s.initialize()
        tool = next(t for t in (await s.list_tools()).tools
                    if t.name == "redact_sensitive_data")
        result = await s.call_tool("redact_sensitive_data", args)
        text = " ".join(getattr(c, "text", "") for c in result.content)
        ref = re.search(r'review="(review:[^"]+)"', text)
        if ref:  # an SDK without URL-mode elicitation: Paperboy opened the page itself
            assert "127.0.0.1" not in text
            for _ in range(50):                 # the opener runs detached
                if opened.exists():
                    break
                await anyio.sleep(0.1)
            await anyio.to_thread.run_sync(decide, opened.read_text().split()[0])
            result = await s.call_tool("redact_sensitive_data",
                                       {**args, "review": ref.group(1)})
    structured = getattr(result, "structured_content", None) or result.structuredContent
    jsonschema.validate(structured, getattr(tool, "output_schema", None) or tool.outputSchema)
    assert json.loads(result.content[0].text) == structured
    assert structured["status"] == "redacted" and structured["skipped"] == 1
    assert "123-45-6789" not in page_labels(tmp_path / "statement_redacted.pdf")[0]


def test_a_name_inside_an_email_doesnt_hide_the_email(tmp_path, settings):
    path = _pdf(tmp_path / "ada.pdf", ["Ada, ada@example.com, card 4111 1111 1111 1111"])
    found = E.detect(path, settings, texts=["Ada"])
    assert _texts(found) == [("custom", "Ada"), ("email", "ada@example.com"),
                             ("card_number", "4111 1111 1111 1111")]


def test_an_iban_holding_a_card_like_run_is_one_iban(tmp_path, settings):
    assert E._real("card_number", "43 1188 6305 9319 2243")       # the run alone passes
    path = _pdf(tmp_path / "de.pdf", ["Pay to DE43 1188 6305 9319 2243 26 today"])
    assert _texts(E.detect(path, settings)) == [("iban", "DE43 1188 6305 9319 2243 26")]


def test_look_alike_letters_are_not_numbers(tmp_path, settings):
    # "[a-z]" ignoring case takes the Kelvin sign: it must not reach the check digits.
    assert not E._real("iban", "KZ12 3456 7890 1234 56")
    assert not E._real("uk_ni_number", "AK 12 34 56 C")
    assert E._real("email", "jörg@example.com")
    path = _pdf(tmp_path / "k.pdf", ["KZ12 3456 7890 1234 56"])
    assert E.detect(path, settings)["findings"] == []


async def test_text_outside_the_page_is_redacted_too(tmp_path):
    path = _pdf(tmp_path / "hidden.pdf", ["Contact: ada@example.com"])
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        pdf.pages[0].MediaBox = [0, 0, 140, 800]    # the address runs off its edge
        pdf.save(path)
    res = await _run(_Ctx(decide=_ticks("ada@example.com")), input_path=str(path))
    assert res["status"] == "redacted"
    out = tmp_path / "hidden_redacted.pdf"
    import pypdfium2
    doc = pypdfium2.PdfDocument(str(out))
    text = doc[0].get_textpage().get_text_range()
    doc.close()
    assert "example" not in text and "ada" not in text


def test_snippets_are_bounded(tmp_path, settings, monkeypatch):
    path = tmp_path / "tall.pdf"
    c = canvas.Canvas(str(path), pagesize=(300, 3000))
    c.drawString(20, 2900, "Mail: ada@example.com")
    c.drawString(20, -50, "Off the page: bob@example.com")  # outside the media box
    c.showPage()
    c.save()
    found = E.detect(path, settings, snippets=True)
    on, off = found["findings"]
    assert "snippet" in on and "snippet" not in off            # nothing to show for it
    monkeypatch.setattr(E, "SNIPPET_HEIGHT", 20)
    strip = Image.open(io.BytesIO(E.detect(path, settings, snippets=True)
                                  ["findings"][0]["snippet"]))
    assert strip.height <= 20 * E.SNIPPET_WIDTH / strip.width + 1
    monkeypatch.setattr(E, "SNIPPET_BYTES", 0)
    assert "snippet" not in E.detect(path, settings, snippets=True)["findings"][0]


def test_a_form_from_another_site_is_refused():
    review = _review()
    url = R.PAGE.url(review)
    parts = urllib.parse.urlsplit(url)
    for origin, status in (("https://evil.example", 404), ("null", 404),
                           (f"http://{parts.netloc}", 200)):
        conn = http.client.HTTPConnection(parts.hostname, parts.port, timeout=10)
        conn.request("POST", parts.path, "action=cancel",
                     {"Host": parts.netloc, "Origin": origin,
                      "Content-Type": "application/x-www-form-urlencoded"})
        assert conn.getresponse().status == status
        conn.close()
    assert R.PAGE.collect(review) == (True, None)


def test_ids_must_be_plain_digits():
    review = _review()
    _request(R.PAGE.url(review), "POST", {"action": "redact", "f": ["²", "1"]})
    assert R.PAGE.collect(review) == (True, [1])
