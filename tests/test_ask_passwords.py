"""Passwords typed by the user on a local page, never given to the model.

The local page (password_page), the checks made before anyone is asked, and the whole
flow over real stdio: through URL-mode elicitation where the SDK has it (mcp >= 1.23),
otherwise by the tool replying with the link and taking "ask:<ref>" next time.
"""
from __future__ import annotations

import dataclasses
import http.client
import json
import os
import re
import urllib.parse
from types import SimpleNamespace

import anyio
import pikepdf
import pytest
from mcp import ClientSession, types
from test_server_stdio import _is_error, _payload, _session

import paperboy.server as S
from paperboy import password_page as P

from conftest import _make_pdf


@pytest.fixture
def server_env(tmp_path):
    env = dict(os.environ, PAPERBOY_ROOT=str(tmp_path))
    src = os.path.join(os.path.dirname(os.path.dirname(__file__)), "src")
    env["PYTHONPATH"] = src + os.pathsep + env.get("PYTHONPATH", "")
    return env


def _http(url: str, method="GET", fields=None, host=None):
    """A browser on this computer: (status, body)."""
    parts = urllib.parse.urlsplit(url)
    conn = http.client.HTTPConnection(parts.hostname, parts.port, timeout=10)
    body = urllib.parse.urlencode(fields).encode() if fields is not None else None
    headers = {"Host": host or parts.netloc}
    if body is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    conn.request(method, parts.path, body=body, headers=headers)
    response = conn.getresponse()
    status, text = response.status, response.read().decode()
    conn.close()
    return status, text, dict(response.getheaders())


def _locked(path, user="secret", owner="boss"):
    plain = _make_pdf(path.with_name("plain-" + path.name), 2)
    with pikepdf.open(plain) as pdf:
        pdf.save(path, encryption=pikepdf.Encryption(user=user, owner=owner, R=6))
    return path


# --- the page ----------------------------------------------------------------------------------


@pytest.fixture
def page():
    return P.PasswordPage()


def test_typing_a_password_on_the_page(page):
    entry = page.request("opening “a.pdf”", new=False)
    url = page.url(entry)
    assert url.startswith("http://127.0.0.1:") and entry.token in url
    status, text, headers = _http(url)
    assert status == 200 and "a.pdf" in text and 'type="password"' in text
    assert headers["Cache-Control"] == "no-store" and headers["X-Frame-Options"] == "DENY"
    assert "default-src 'none'" in headers["Content-Security-Policy"]
    status, text, _ = _http(url, "POST", {"password": "s3cret"})
    assert status == 200 and "close this tab" in text
    assert page.wait(entry, 1) == "s3cret"
    assert _http(url)[0] == 404                              # one use


def test_the_page_answers_only_its_own_address(page):
    entry = page.request("x", new=False)
    url = page.url(entry)
    assert _http(url, host="evil.example:80")[0] == 404      # DNS rebinding
    assert _http(url.replace("/p/", "/q/"))[0] == 404
    assert _http(url + "wrong")[0] == 404
    assert _http(url, "POST", {"password": "x"}, host="evil.example")[0] == 404
    assert not entry.done.is_set()


def test_new_passwords_are_typed_twice(page):
    entry = page.request("a new password", new=True)
    url = page.url(entry)
    assert 'name="confirm"' in _http(url)[1]
    status, text, _ = _http(url, "POST", {"password": "a", "confirm": "b"})
    assert status == 200 and "didn&#x27;t match" in text and not entry.done.is_set()
    _http(url, "POST", {"password": "a", "confirm": "a"})
    assert page.wait(entry, 1) == "a"


@pytest.mark.parametrize("fields", [{"password": ""}, {"other": "x"},
                                    {"password": "x" * 1025}])
def test_empty_or_oversized_passwords_are_refused(page, fields):
    entry = page.request("x", new=False)
    status, text, _ = _http(page.url(entry), "POST", fields)
    assert status in (200, 404) and not entry.done.is_set()


def test_oversized_submissions_are_refused(page):
    entry = page.request("x", new=False)
    assert _http(page.url(entry), "POST", {"password": "x" * 5000})[0] == 404


def test_links_expire(page, monkeypatch):
    entry, other = page.request("x", new=False), page.request("z", new=False)
    monkeypatch.setattr(P, "LINK_LIFETIME_S", -1)
    assert _http(page.url(entry))[0] == 404
    assert page.wait(entry, 0) is None
    page.request("y", new=False)                             # sweeping drops expired ones
    assert other.token not in page._entries


def test_a_typed_password_is_taken_once_by_reference_for_its_purpose(page):
    entry = page.request("x", new=False, purpose=("pdf_info", "password", "/a.pdf"))
    assert page.ready(entry.ref, entry.purpose) is None      # not typed yet
    _http(page.url(entry), "POST", {"password": "pw"})
    assert page.ready(entry.ref, ("encrypt_pdf", "user_password", "/a.pdf")) is None
    assert page.ready("unknown", entry.purpose) is None
    assert page.collect(page.ready(entry.ref, entry.purpose)) == "pw"
    assert page.ready(entry.ref, entry.purpose) is None


def test_a_password_a_call_waits_for_cant_be_taken_by_reference(page):
    entry = page.request("x", new=False, waited=True)
    _http(page.url(entry), "POST", {"password": "pw"})
    assert page.ready(entry.ref, entry.purpose) is None
    assert page.wait(entry, 1) == "pw"


def test_the_first_submission_wins(page):
    entry = page.request("x", new=False)
    url = page.url(entry)
    assert _http(url, "POST", {"password": "one"})[0] == 200
    assert _http(url, "POST", {"password": "two"})[0] == 404
    assert page.collect(entry) == "one"


def test_a_bad_length_is_refused(page):
    entry = page.request("x", new=False)
    parts = urllib.parse.urlsplit(page.url(entry))
    conn = http.client.HTTPConnection(parts.hostname, parts.port, timeout=10)
    conn.putrequest("POST", parts.path, skip_host=True)
    conn.putheader("Host", parts.netloc)
    conn.putheader("Content-Length", "zz")
    conn.endheaders()
    assert conn.getresponse().status == 404
    conn.close()
    assert not entry.done.is_set()


def test_the_server_takes_only_so_many_connections(page, monkeypatch):
    import socket
    import time
    monkeypatch.setattr(P, "MAX_CONNECTIONS", 2)
    entry = page.request("x", new=False)
    page._server._slots = __import__("threading").BoundedSemaphore(2)
    idle = [socket.create_connection(("127.0.0.1", page.port)) for _ in range(2)]
    time.sleep(0.3)
    extra = socket.create_connection(("127.0.0.1", page.port))
    extra.settimeout(5)
    assert extra.recv(1) == b""                              # closed straight away
    for s in (*idle, extra):
        s.close()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            if _http(page.url(entry))[0] == 200:
                break
        except OSError:
            pass
        time.sleep(0.1)
    else:
        raise AssertionError("the slots were never given back")


def test_expired_passwords_are_forgotten_without_another_call(monkeypatch):
    import time
    monkeypatch.setattr(P, "SWEEP_EVERY_S", 0.05)
    page = P.PasswordPage()
    entry = page.request("x", new=False)
    _http(page.url(entry), "POST", {"password": "pw"})
    monkeypatch.setattr(P, "LINK_LIFETIME_S", -1)
    deadline = time.monotonic() + 5
    while entry.value is not None and time.monotonic() < deadline:
        time.sleep(0.05)
    assert entry.value is None and entry.token not in page._entries


def test_waiting_gives_up(page):
    assert page.wait(page.request("x", new=False), 0.05) is None


def test_only_so_many_links_wait_at_once(page, monkeypatch):
    monkeypatch.setattr(P, "MAX_PENDING", 2)
    page.request("a", False)
    page.request("b", False)
    with pytest.raises(RuntimeError, match="too many"):
        page.request("c", False)


# --- checks before anyone is asked (in this process) --------------------------------------------


class _Ctx:
    """The MCP request context: whether the client can open links, and what the user
    does when it does."""

    def __init__(self, url_mode=True, action="accept", typed=None):
        self.opened, self.completed = [], []
        self.action, self.typed = action, list(typed or [])
        outer = self

        class Session:
            client_params = SimpleNamespace(capabilities=SimpleNamespace(
                elicitation=SimpleNamespace(form=object(),
                                            url=object() if url_mode else None)))

            async def send_elicit_complete(self, ref):
                outer.completed.append(ref)
        self.session = Session()

    async def elicit_url(self, message, url, elicitation_id):
        self.opened.append(message)
        if self.typed:
            reply = self.typed.pop(0)
            if isinstance(reply, Exception):
                raise reply
            await anyio.to_thread.run_sync(_http, url, "POST", reply)
        return type("Answer", (), {"action": self.action})()


@pytest.fixture
def sandbox(settings, monkeypatch, make_pdf):
    monkeypatch.setattr(S, "SETTINGS", settings)
    return settings


def _link(error) -> tuple[str, str]:
    message = str(error.value)
    return (re.search(r"http://127\.0\.0\.1:\d+/p/\S+", message).group(0),
            re.search(r'"ask:([^"]+)"', message).group(1))


async def test_a_password_typed_through_the_client(sandbox, tmp_path):
    locked = _locked(tmp_path / "taxes.pdf")
    ctx = _Ctx(typed=[{"password": "secret"}])
    kwargs = {"input_path": str(locked), "password": "ask"}
    await S._resolve_passwords(ctx, kwargs)
    assert kwargs["password"] == "secret" and len(ctx.completed) == 1
    assert "“taxes.pdf”" in ctx.opened[0] and str(tmp_path) not in ctx.opened[0]


async def test_declining_or_not_typing_does_nothing(sandbox, tmp_path, monkeypatch):
    locked = _locked(tmp_path / "l.pdf")
    with pytest.raises(S.ToolError, match="No password was given"):
        await S._resolve_passwords(_Ctx(action="decline"),
                                   {"input_path": str(locked), "password": "ask"})
    monkeypatch.setattr(S, "ASK_WAIT_S", 0.05)
    with pytest.raises(S.ToolError, match="No password was typed in time"):
        await S._resolve_passwords(_Ctx(), {"input_path": str(locked), "password": "ask"})
    with pytest.raises(S.ToolError, match="couldn't open the password page"):
        await S._resolve_passwords(_Ctx(typed=[RuntimeError("/home/x")]),
                                   {"input_path": str(locked), "password": "ask"})


async def test_without_url_mode_the_reply_carries_the_link(sandbox, tmp_path):
    locked = _locked(tmp_path / "l.pdf")
    kwargs = {"input_path": str(locked), "password": "ask"}
    with pytest.raises(S.ToolError) as caught:
        await S._resolve_passwords(_Ctx(url_mode=False), kwargs)
    url, ref = _link(caught)
    _http(url, "POST", {"password": "secret"})
    again = {"input_path": str(locked), "password": f"ask:{ref}"}
    await S._resolve_passwords(_Ctx(url_mode=False), again)
    assert again["password"] == "secret"
    with pytest.raises(S.ToolError, match="No password has been typed"):
        await S._resolve_passwords(_Ctx(url_mode=False),
                                   {"input_path": str(locked), "password": f"ask:{ref}"})


async def test_a_typed_password_serves_only_what_it_was_asked_for(sandbox, tmp_path):
    locked, junk = _locked(tmp_path / "secret.pdf"), _make_pdf(tmp_path / "junk.pdf", 1)
    with pytest.raises(S.ToolError) as caught:
        await S._resolve_passwords(_Ctx(url_mode=False),
                                   {"input_path": str(locked), "password": "ask"}, "pdf_info")
    url, ref = _link(caught)
    _http(url, "POST", {"password": "Bank$ecret1"})
    for tool, kwargs in [
        ("encrypt_pdf", {"input_path": str(junk), "user_password": f"ask:{ref}"}),
        ("pdf_info", {"input_path": str(junk), "password": f"ask:{ref}"}),
        ("decrypt_pdf", {"input_path": str(locked), "password": f"ask:{ref}"}),
    ]:
        with pytest.raises(S.ToolError, match="for something else"):
            await S._resolve_passwords(_Ctx(url_mode=False), kwargs, tool)
    kwargs = {"input_path": str(locked), "password": f"ask:{ref}"}
    await S._resolve_passwords(_Ctx(url_mode=False), kwargs, "pdf_info")
    assert kwargs["password"] == "Bank$ecret1"


@pytest.mark.parametrize("algorithm", ["AES-128", "RC4-128", "RC4-40"])
async def test_an_owner_password_the_model_knows_cant_unlock_a_typed_one(sandbox, tmp_path,
                                                                        algorithm):
    doc = str(_make_pdf(tmp_path / "doc.pdf", 1))
    for kwargs in ({"input_path": doc, "user_password": "ask", "owner_password": "known",
                    "algorithm": algorithm},
                   {"input_path": doc, "steps": [{"tool": "encrypt_pdf", "args": {
                       "user_password": "ask", "owner_password": "known",
                       "algorithm": algorithm}}]}):
        ctx = _Ctx(typed=[{"password": "x", "confirm": "x"}])
        with pytest.raises(S.ToolError, match="owner password must be typed too"):
            await S._resolve_passwords(ctx, kwargs, "encrypt_pdf")
        assert ctx.opened == []
    for owner, alg in (("known", "AES-256"), (None, algorithm), ("ask", algorithm)):
        ctx = _Ctx(typed=[{"password": "n", "confirm": "n"}, {"password": "o", "confirm": "o"}])
        kwargs = {"input_path": doc, "user_password": "ask", "owner_password": owner,
                  "algorithm": alg}
        await S._resolve_passwords(ctx, kwargs, "encrypt_pdf")
        assert kwargs["user_password"] == "n"


async def test_a_reference_is_kept_when_the_call_stops_short(sandbox, tmp_path):
    a, b = _locked(tmp_path / "a.pdf"), _locked(tmp_path / "b.pdf")
    with pytest.raises(S.ToolError) as caught:
        await S._resolve_passwords(_Ctx(url_mode=False),
                                   {"path_a": str(a), "password_a": "ask"}, "compare_pdfs")
    url, ref = _link(caught)
    _http(url, "POST", {"password": "secret"})
    kwargs = {"path_a": str(a), "password_a": f"ask:{ref}", "path_b": str(b),
              "password_b": "ask"}
    with pytest.raises(S.ToolError, match="Open the link"):   # asks for b first
        await S._resolve_passwords(_Ctx(url_mode=False), dict(kwargs), "compare_pdfs")
    kwargs["password_b"] = "secret"                         # the reference still works
    await S._resolve_passwords(_Ctx(url_mode=False), kwargs, "compare_pdfs")
    assert kwargs["password_a"] == "secret"


async def test_nothing_is_asked_for_a_call_that_would_fail(sandbox, tmp_path, outside):
    ctx = _Ctx(typed=[{"password": "x"}] * 5)
    for kwargs, message in [
        ({"input_path": str(tmp_path / "missing.pdf"), "password": "ask"}, "not found"),
        ({"input_path": str(outside / "secret.pdf"), "password": "ask"}, "outside"),
        ({"input_paths": [str(_make_pdf(tmp_path / "a.pdf", 1))],
          "passwords": {str(tmp_path / "Bank PIN.pdf"): "ask"}}, "isn't being merged"),
        ({"input_path": str(_make_pdf(tmp_path / "b.pdf", 1)),
          "steps": [{"tool": "rotate_pages", "args": {"degrees": 90, "user_password": "ask"}}]},
         "Extra inputs"),
        ({"input_path": str(tmp_path / "missing.pdf"), "user_password": "ask"}, "not found"),
    ]:
        with pytest.raises(S.ToolError, match=message):
            await S._resolve_passwords(ctx, kwargs)
    assert ctx.opened == []


async def test_a_call_asks_for_only_so_many_passwords(sandbox, tmp_path, monkeypatch):
    monkeypatch.setattr(S, "MAX_ASKS", 1)
    a, b = _locked(tmp_path / "a.pdf"), _locked(tmp_path / "b.pdf")
    with pytest.raises(S.ToolError, match="at most 1 passwords"):
        await S._resolve_passwords(_Ctx(), {"path_a": str(a), "path_b": str(b),
                                            "password_a": "ask", "password_b": "ask"})


async def test_file_names_are_shown_safely(sandbox, tmp_path):
    odd = tmp_path / ("x‮" + "y" * 200 + ".pdf")
    _locked(odd)
    ctx = _Ctx(typed=[{"password": "secret"}])
    await S._resolve_passwords(ctx, {"input_path": str(odd), "password": "ask"})
    shown = re.search("“(.*)”", ctx.opened[0]).group(1)
    assert len(shown) <= 80 and "‮" not in shown


async def test_every_kind_of_password_can_be_asked(sandbox, tmp_path):
    a, b = _locked(tmp_path / "a.pdf"), _locked(tmp_path / "b.pdf")
    doc = _make_pdf(tmp_path / "doc.pdf", 2)
    ctx = _Ctx(typed=[{"password": "p1"}, {"password": "n", "confirm": "n"},
                      {"password": "o", "confirm": "o"}, {"password": "m1"},
                      {"password": "s", "confirm": "s"}])
    kwargs = {"input_path": str(doc), "current_password": "ask", "user_password": "ask",
              "owner_password": "ask", "input_paths": [str(a), str(b)],
              "passwords": {str(a): "ask", str(b): "plain"},
              "steps": [{"tool": "encrypt_pdf", "args": {"user_password": "ask"}}]}
    await S._resolve_passwords(ctx, kwargs)
    assert (kwargs["current_password"], kwargs["user_password"], kwargs["owner_password"]) \
        == ("p1", "n", "o")
    assert kwargs["passwords"] == {str(a): "m1", str(b): "plain"}
    assert kwargs["steps"][0]["args"]["user_password"] == "s"


async def test_plain_passwords_can_be_turned_off(sandbox, tmp_path, monkeypatch):
    monkeypatch.setattr(S, "SETTINGS", dataclasses.replace(sandbox,
                                                           require_password_references=True))
    locked = _locked(tmp_path / "l.pdf")
    for kwargs in ({"input_path": str(locked), "password": "secret"},
                   {"input_paths": [str(locked)], "passwords": {str(locked): "secret"}},
                   {"input_path": str(locked),
                    "steps": [{"tool": "encrypt_pdf", "args": {"user_password": "x"}}]}):
        with pytest.raises(S.ToolError, match="can't be given in tool arguments"):
            await S._resolve_passwords(_Ctx(), kwargs)
    kwargs = {"password": None, "user_password": ""}
    await S._resolve_passwords(_Ctx(), kwargs)               # nothing given: nothing refused
    assert kwargs == {"password": None, "user_password": ""}


async def test_the_registered_tool_runs_in_a_worker_thread():
    import inspect
    import threading
    seen = []

    def tool(input_path: str, password: str | None = None) -> dict:
        seen.append(threading.current_thread() is threading.main_thread())
        return {"ok": True}
    wrapped = S._asking(tool)
    assert "paperboy_context" in str(inspect.signature(wrapped))
    assert await wrapped(input_path="x.pdf", password=None,
                         paperboy_context=_Ctx()) == {"ok": True}
    assert seen == [False]
    assert S._asking(S.list_pdfs) is S.list_pdfs


@pytest.fixture
def outside(tmp_path_factory):
    d = tmp_path_factory.mktemp("outside")
    _locked(d / "secret.pdf")
    return d


# --- over stdio ----------------------------------------------------------------------------------


def _user(*replies):
    """A user who, when the client opens a page, types the next reply on it."""
    queue = list(replies)

    async def callback(context, params):
        url = getattr(params, "url", None)
        if url is None:            # older clients only know forms: Paperboy never asks so
            return types.ElicitResult(action="decline")
        await anyio.to_thread.run_sync(_http, url, "POST", queue.pop(0))
        return types.ElicitResult(action="accept")
    return callback


async def _call(env, tool, args, *replies):
    """Call a tool as a user would: through the client's page if it can open one, or by
    opening the links the tool replies with and running it again."""
    async with await _session(env) as (r, w):
        async with ClientSession(r, w, elicitation_callback=_user(*replies)) as s:
            await s.initialize()
            result = await s.call_tool(tool, args)
            text = " ".join(getattr(c, "text", "") for c in result.content)
            links = re.findall(r'(http://127\.0\.0\.1:\d+/p/\S+) \(then pass "ask:([^"]+)" as '
                               r'([^)]+)\)', text)
            if not links:
                return result
            args = json.loads(json.dumps(args))
            for (url, ref, name), reply in zip(links, replies, strict=False):
                _http(url, "POST", reply)
                _replace(args, name, f"ask:{ref}")
            return await s.call_tool(tool, args)


def _replace(args: dict, name: str, value: str) -> None:
    if args.get(name) == "ask":
        args[name] = value
    elif name in (args.get("passwords") or {}):
        args["passwords"][name] = value
    else:
        for step in args.get("steps") or []:
            if (step.get("args") or {}).get(name) == "ask":
                step["args"][name] = value
                return


async def test_over_stdio_open_and_new_passwords(server_env, tmp_path):
    locked = _locked(tmp_path / "taxes.pdf")
    res = await _call(server_env, "pdf_info", {"input_path": str(locked), "password": "ask"},
                      {"password": "secret"})
    assert not _is_error(res) and _payload(res)["pages"] == 2
    assert "secret" not in json.dumps(_payload(res))
    doc = _make_pdf(tmp_path / "doc.pdf", 2)
    res = await _call(server_env, "encrypt_pdf", {"input_path": str(doc),
                                                  "user_password": "ask"},
                      {"password": "n3w", "confirm": "n3w"})
    assert not _is_error(res)
    with pikepdf.open(tmp_path / "doc_encrypted.pdf", password="n3w") as pdf:
        assert len(pdf.pages) == 2


async def test_over_stdio_a_pipeline_and_a_merge(server_env, tmp_path):
    doc = _make_pdf(tmp_path / "doc.pdf", 3)
    res = await _call(server_env, "run_pipeline",
                      {"input_path": str(doc),
                       "steps": [{"tool": "delete_pages", "args": {"pages": "1"}},
                                 {"tool": "encrypt_pdf", "args": {"user_password": "ask"}}]},
                      {"password": "piped", "confirm": "piped"})
    assert not _is_error(res)
    with pikepdf.open(tmp_path / "doc_processed.pdf", password="piped") as pdf:
        assert len(pdf.pages) == 2
    a, b = _locked(tmp_path / "a.pdf"), _locked(tmp_path / "b.pdf")
    res = await _call(server_env, "merge_pdfs",
                      {"input_paths": [str(a), str(b)], "output_path": str(tmp_path / "m.pdf"),
                       "passwords": {str(a): "ask", str(b): "ask"}},
                      {"password": "secret"}, {"password": "secret"})
    assert not _is_error(res) and _payload(res)["pages"] == 4


async def test_over_stdio_plain_passwords_can_be_turned_off(server_env, tmp_path):
    locked = _locked(tmp_path / "l.pdf")
    env = dict(server_env, PAPERBOY_REQUIRE_PASSWORD_REFERENCES="true")
    res = await _call(env, "pdf_info", {"input_path": str(locked), "password": "secret"})
    assert _is_error(res)
    res = await _call(env, "pdf_info", {"input_path": str(locked), "password": "ask"},
                      {"password": "secret"})
    assert not _is_error(res)


async def test_tool_descriptions_mention_ask(server_env):
    async with await _session(server_env) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            tools = {t.name: t for t in (await s.list_tools()).tools}
    assert '"ask"' in tools["decrypt_pdf"].description
    assert '"ask"' not in tools["list_pdfs"].description


def test_url_mode_needs_the_sdk_and_the_client():
    assert S._url_mode(_Ctx(url_mode=True)) is True
    assert S._url_mode(_Ctx(url_mode=False)) is False        # a client with forms only
    no_sdk = _Ctx()
    no_sdk.elicit_url = None
    del no_sdk.elicit_url
    assert S._url_mode(SimpleNamespace(session=no_sdk.session)) is False
    assert S._url_mode(SimpleNamespace(elicit_url=1, session=SimpleNamespace())) is False


async def test_a_form_only_client_gets_the_link(sandbox, tmp_path):
    ctx = _Ctx(url_mode=False, typed=[{"password": "secret"}])
    with pytest.raises(S.ToolError, match="Open the link"):
        await S._resolve_passwords(ctx, {"input_path": str(_locked(tmp_path / "l.pdf")),
                                         "password": "ask"})
    assert ctx.opened == []


async def test_cancelling_the_call_stops_the_wait(sandbox, tmp_path, monkeypatch):
    import time
    monkeypatch.setattr(S, "ASK_WAIT_S", 60)
    before = set(P.PAGE._entries)
    start = time.monotonic()
    with anyio.move_on_after(0.3):
        await S._resolve_passwords(_Ctx(), {"input_path": str(_locked(tmp_path / "l.pdf")),
                                            "password": "ask"})
    assert time.monotonic() - start < 5
    assert set(P.PAGE._entries) <= before                    # the link is gone


async def test_a_declined_page_is_gone(sandbox, tmp_path):
    before = set(P.PAGE._entries)
    with pytest.raises(S.ToolError, match="No password was given"):
        await S._resolve_passwords(_Ctx(action="decline"),
                                   {"input_path": str(_locked(tmp_path / "l.pdf")),
                                    "password": "ask"})
    assert set(P.PAGE._entries) <= before


async def test_page_trouble_is_explained(sandbox, tmp_path, monkeypatch):
    kwargs = {"input_path": str(_locked(tmp_path / "l.pdf")), "password": "ask"}
    for error, message in ((RuntimeError("full"), "Too many password pages"),
                           (OSError("no port"), "couldn't start")):
        def fail(*args, error=error):
            raise error
        monkeypatch.setattr(P.PAGE, "request", fail)
        with pytest.raises(S.ToolError, match=message):
            await S._resolve_passwords(_Ctx(), dict(kwargs))


async def test_a_client_that_fails_to_hear_back_still_gets_the_result(sandbox, tmp_path):
    locked = _locked(tmp_path / "l.pdf")
    ctx = _Ctx(typed=[{"password": "secret"}])

    async def refuse(ref):
        raise RuntimeError("gone")
    ctx.session.send_elicit_complete = refuse
    kwargs = {"input_path": str(locked), "password": "ask"}
    await S._resolve_passwords(ctx, kwargs)
    assert kwargs["password"] == "secret"
