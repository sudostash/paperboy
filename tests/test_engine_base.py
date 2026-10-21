"""Shared engine helpers: open_pdf, is_encrypted, copy_docinfo."""
from __future__ import annotations

import dataclasses
import subprocess

import pikepdf
import pytest

from paperboy.engine.base import copy_docinfo, is_encrypted, open_pdf, run_qpdf
from paperboy.errors import PdfUserError

from conftest import HAS_QPDF


def _spy_close(monkeypatch) -> list:
    closed = []
    real = pikepdf.Pdf.close
    monkeypatch.setattr(pikepdf.Pdf, "close", lambda self: (closed.append(self), real(self)))
    return closed


def test_open_pdf_yields_document_and_closes_it(doc, settings, monkeypatch):
    closed = _spy_close(monkeypatch)
    with open_pdf(doc, settings) as pdf:
        assert len(pdf.pages) == 5
        assert closed == []
    assert closed == [pdf]


def test_open_pdf_closes_even_when_the_body_fails(doc, settings, monkeypatch):
    closed = _spy_close(monkeypatch)
    with pytest.raises(RuntimeError):
        with open_pdf(doc, settings):
            raise RuntimeError("boom")
    assert len(closed) == 1


def test_open_pdf_closes_when_over_the_page_cap(doc, settings, monkeypatch):
    closed = _spy_close(monkeypatch)
    with pytest.raises(PdfUserError):
        with open_pdf(doc, dataclasses.replace(settings, max_pages=1)):
            pass
    assert len(closed) == 1


def test_open_pdf_with_correct_password(make_encrypted, settings):
    with open_pdf(make_encrypted(password="s3cret"), settings, "s3cret") as pdf:
        assert len(pdf.pages) == 3


def test_open_pdf_without_password_asks_for_one(make_encrypted, settings):
    with pytest.raises(PdfUserError, match="supply its password"):
        with open_pdf(make_encrypted(), settings):
            pass


def test_open_pdf_with_wrong_password(make_encrypted, settings):
    with pytest.raises(PdfUserError, match="Incorrect password"):
        with open_pdf(make_encrypted(password="right"), settings, "wrong"):
            pass


def test_open_pdf_rejects_non_pdf(tmp_path, settings):
    junk = tmp_path / "junk.pdf"
    junk.write_bytes(b"this is not a pdf at all")
    with pytest.raises(PdfUserError, match="Could not open PDF"):
        with open_pdf(junk, settings):
            pass


def test_open_pdf_enforces_page_cap(doc, settings):
    capped = dataclasses.replace(settings, max_pages=4)
    with pytest.raises(PdfUserError, match="5 pages, over the 4 page limit"):
        with open_pdf(doc, capped):
            pass


def test_open_pdf_allows_exactly_the_cap(doc, settings):
    with open_pdf(doc, dataclasses.replace(settings, max_pages=5)) as pdf:
        assert len(pdf.pages) == 5


def test_is_encrypted(doc, make_encrypted):
    assert is_encrypted(doc) is False
    assert is_encrypted(make_encrypted()) is True


def test_is_encrypted_with_owner_password_only(make_pdf, tmp_path):
    # Empty user password: opens without a password but is still encrypted.
    src = make_pdf("p.pdf", 1)
    out = tmp_path / "owner-only.pdf"
    with pikepdf.open(src) as pdf:
        pdf.save(out, encryption=pikepdf.Encryption(user="", owner="boss", R=6))
    assert is_encrypted(out) is True


def test_copy_docinfo_carries_metadata(doc, tmp_path):
    with pikepdf.open(doc) as src:
        with src.open_metadata() as m:
            m["dc:title"] = "Carried"
        src.save(tmp_path / "titled.pdf")
    with pikepdf.open(tmp_path / "titled.pdf") as src, pikepdf.new() as dst:
        copy_docinfo(src, dst)
        with dst.open_metadata() as m:
            assert m["dc:title"] == "Carried"


def test_copy_docinfo_never_raises(doc):
    class Broken:
        def open_metadata(self, *a, **k):
            raise RuntimeError("boom")

    with pikepdf.open(doc) as src:
        copy_docinfo(src, Broken())  # best-effort: swallowed


# --- run_qpdf ------------------------------------------------------------------


def _capture_run(monkeypatch) -> list[dict]:
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append({"cmd": cmd, **kwargs})
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    return calls


def test_run_qpdf_sends_password_on_stdin_not_argv(settings, monkeypatch):
    calls = _capture_run(monkeypatch)
    run_qpdf(dataclasses.replace(settings, qpdf_path="qpdf"), ["--check", "f.pdf"], "s3cret")
    (call,) = calls
    assert call["cmd"] == ["qpdf", "--password-file=-", "--check", "f.pdf"]
    assert call["input"] == b"s3cret"
    assert not any("s3cret" in part for part in call["cmd"])
    assert call["timeout"] == 120


def test_run_qpdf_without_password(settings, monkeypatch):
    calls = _capture_run(monkeypatch)
    run_qpdf(dataclasses.replace(settings, qpdf_path="qpdf"), ["--check", "f.pdf"])
    assert calls[0]["cmd"] == ["qpdf", "--check", "f.pdf"]


def test_run_qpdf_timeout_is_a_user_error(settings, monkeypatch):
    def hang(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])

    monkeypatch.setattr(subprocess, "run", hang)
    with pytest.raises(PdfUserError, match="qpdf timed out after 5s"):
        run_qpdf(dataclasses.replace(settings, qpdf_path="qpdf"), ["x"], timeout=5)


@pytest.mark.skipif(not HAS_QPDF, reason="qpdf CLI not installed")
def test_run_qpdf_password_works_with_real_qpdf(make_encrypted, settings):
    locked = make_encrypted(password="pw")
    assert run_qpdf(settings, ["--check", str(locked)], "pw").returncode == 0
    assert run_qpdf(settings, ["--check", str(locked)], "bad").returncode == 2


@pytest.mark.parametrize("password", ["a\nb", "a\rb"])
def test_run_qpdf_rejects_multiline_passwords(settings, monkeypatch, password):
    calls = _capture_run(monkeypatch)
    with pytest.raises(PdfUserError, match="line break"):
        run_qpdf(dataclasses.replace(settings, qpdf_path="qpdf"), ["x"], password)
    assert calls == []


def test_run_qpdf_decodes_output_safely(settings, monkeypatch):
    def run(cmd, **kwargs):
        assert kwargs["input"] == "pässword".encode()
        return subprocess.CompletedProcess(cmd, 0, b"caf\xe9\r\n", b"oops \xff")

    monkeypatch.setattr(subprocess, "run", run)
    proc = run_qpdf(dataclasses.replace(settings, qpdf_path="qpdf"), ["x"], "pässword")
    assert proc.stdout == "caf�\r\n"  # raw bytes decoded, \r kept as-is
    assert proc.stderr == "oops �"
