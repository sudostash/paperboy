"""scan_pdf: a risk report. It never calls a file safe, only says what it found."""
from __future__ import annotations

import base64
import dataclasses
import zlib
from pathlib import Path

import pikepdf
import pytest
from test_engine_sanitize import _action, _annot, _plain

from paperboy import engine as E
from paperboy.errors import PdfUserError


def _codes(result) -> dict[str, dict]:
    return {i["code"]: i for i in result["indicators"]}


def _attach(pdf, name, data, compress=True):
    stream = pikepdf.Stream(pdf, zlib.compress(data) if compress else data)
    if compress:
        stream.Filter = pikepdf.Name.FlateDecode
    stream.Params = pikepdf.Dictionary(Size=len(data))
    spec = pdf.make_indirect(pikepdf.Dictionary(
        Type=pikepdf.Name.Filespec, F=pikepdf.String(name), UF=pikepdf.String(name),
        EF=pikepdf.Dictionary(F=pdf.make_indirect(stream))))
    names = pdf.Root.get("/Names") or pikepdf.Dictionary()
    tree = names.get("/EmbeddedFiles") or pikepdf.Dictionary(Names=pikepdf.Array())
    tree.Names.extend([pikepdf.String(name), spec])
    names.EmbeddedFiles = tree
    pdf.Root.Names = names


def _edit(path, change, **save):
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        change(pdf)
        pdf.save(path, **save)
    return path


# Keep filters exactly as written (pikepdf would otherwise recompress Flate streams).
RAW = {"compress_streams": False, "stream_decode_level": pikepdf.StreamDecodeLevel.none}


@pytest.fixture
def plain(tmp_path):
    return _plain(tmp_path / "plain.pdf")


@pytest.fixture
def no_av(settings):
    return dataclasses.replace(settings, clamscan_path=None)


# --- verdicts ----------------------------------------------------------------------------


def test_plain_pdf_has_no_active_content(plain, no_av):
    res = E.scan_pdf(plain, no_av)
    assert res["verdict"] == "no_active_content_found"
    assert [i for i in res["indicators"] if i["severity"] != "info"] == []
    assert res["embedded_files"] == []
    assert "can't prove" in res["limits"]


def test_never_says_safe(plain, no_av):
    res = E.scan_pdf(plain, no_av)
    assert "safe" not in res["verdict"]


def test_javascript_is_active_content(plain, no_av):
    _edit(plain, lambda pdf: pdf.pages[0].obj.__setitem__("/Annots", pdf.make_indirect(
        pikepdf.Array([_annot(pdf, Subtype=pikepdf.Name.Link, A=_action(
            pdf, "JavaScript", JS=pikepdf.String("x()")))]))))
    res = E.scan_pdf(plain, no_av)
    assert res["verdict"] == "active_content"
    assert _codes(res)["javascript"]["count"] == 1
    assert _codes(res)["javascript"]["severity"] == "warning"


def test_script_that_runs_on_open_is_suspicious(plain, no_av):
    _edit(plain, lambda pdf: setattr(pdf.Root, "OpenAction",
                                     _action(pdf, "JavaScript", JS=pikepdf.String("x()"))))
    res = E.scan_pdf(plain, no_av)
    assert res["verdict"] == "suspicious"
    assert _codes(res)["javascript_on_open"]["severity"] == "high"


def test_script_triggered_by_page_events_counts_as_on_open(plain, no_av):
    _edit(plain, lambda pdf: setattr(pdf.pages[0].obj, "AA", pikepdf.Dictionary(
        O=_action(pdf, "GoTo", D=pikepdf.Array([pdf.pages[0].obj, pikepdf.Name.Fit]),
                  Next=_action(pdf, "JavaScript", JS=pikepdf.String("x()"))))))
    assert _codes(E.scan_pdf(plain, no_av))["javascript_on_open"]["count"] == 1


def test_launch_actions_are_suspicious(plain, no_av):
    _edit(plain, lambda pdf: setattr(pdf.Root, "OpenAction",
                                     _action(pdf, "Launch", F=pikepdf.String("cmd.exe"))))
    res = E.scan_pdf(plain, no_av)
    assert res["verdict"] == "suspicious"
    assert _codes(res)["launch_actions"]["severity"] == "high"


def test_submit_and_remote_actions_are_warnings(plain, no_av):
    _edit(plain, lambda pdf: setattr(pdf.Root, "Extra", pikepdf.Array([
        _action(pdf, "SubmitForm", F=pikepdf.String("https://x.example/collect")),
        _action(pdf, "GoToR", F=pikepdf.String("other.pdf"))])))
    res = E.scan_pdf(plain, no_av)
    assert _codes(res)["risky_actions"]["count"] == 2
    assert _codes(res)["risky_actions"]["severity"] == "warning"
    assert res["verdict"] == "active_content"


# --- disguise and hiding ---------------------------------------------------------------------


def test_obfuscated_names_are_suspicious(plain, no_av):
    # "/J#61vaScript" is "/JavaScript" written with a hex escape, a classic disguise.
    with pikepdf.open(plain, allow_overwriting_input=True) as pdf:
        pdf.Root.OpenAction = _action(pdf, "GoTo",
                                      D=pikepdf.Array([pdf.pages[0].obj, pikepdf.Name.Fit]))
        pdf.save(plain, object_stream_mode=pikepdf.ObjectStreamMode.disable,
                 compress_streams=False)
    data = plain.read_bytes().replace(b"/OpenAction", b"/Op#65nAction", 1)
    plain.write_bytes(data)
    res = E.scan_pdf(plain, no_av)
    assert res["verdict"] == "suspicious"
    hit = _codes(res)["obfuscated_names"]
    assert hit["severity"] == "high" and "OpenAction" in hit["detail"]


def test_ordinary_hex_escapes_are_not_flagged(plain, no_av):
    _edit(plain, lambda pdf: setattr(pdf.Root, "Extra", pikepdf.Name("/My#20Font")))
    assert "obfuscated_names" not in _codes(E.scan_pdf(plain, no_av))


def test_data_after_the_end_of_file(plain, no_av):
    plain.write_bytes(plain.read_bytes() + b"MZ" + b"\x00" * 4000)
    hit = _codes(E.scan_pdf(plain, no_av))["data_after_end"]
    assert hit["severity"] == "warning" and hit["count"] >= 4000


def test_small_trailing_whitespace_is_ignored(plain, no_av):
    plain.write_bytes(plain.read_bytes() + b"\n\r\n  ")
    assert "data_after_end" not in _codes(E.scan_pdf(plain, no_av))


def test_data_before_the_header(plain, no_av):
    plain.write_bytes(b"GIF89a" + b"x" * 200 + plain.read_bytes())  # a GIF/PDF polyglot
    hit = _codes(E.scan_pdf(plain, no_av))["data_before_header"]
    assert hit["severity"] == "warning" and hit["count"] == 206


def test_damaged_structure_is_reported(plain, no_av):
    plain.write_bytes(plain.read_bytes().replace(b"xref", b"xxxx", 1))
    res = E.scan_pdf(plain, no_av)
    hit = _codes(res)["damaged_structure"]
    assert hit["severity"] == "info"    # damage isn't active content on its own
    assert "BytesIO" not in hit["detail"] and "0x" not in hit["detail"]
    assert res["verdict"] == "no_active_content_found"


def test_unusual_filter_chains(plain, no_av):
    def chain(pdf):
        s = pikepdf.Stream(pdf, b"")
        s.write(zlib.compress(zlib.compress(b"hidden")),
                filter=pikepdf.Array([pikepdf.Name.FlateDecode, pikepdf.Name.FlateDecode]))
        pdf.Root.Extra = pdf.make_indirect(s)
    _edit(plain, chain, **RAW)
    assert _codes(E.scan_pdf(plain, no_av))["unusual_filter_chains"]["count"] == 1


def test_exploit_prone_content_types(plain, no_av):
    def add(pdf):
        jbig2 = pikepdf.Stream(pdf, b"\x00")
        jbig2.Filter = pikepdf.Name.JBIG2Decode
        pdf.Root.Extra = pdf.make_indirect(jbig2)
        pdf.pages[0].obj.Annots = pdf.make_indirect(pikepdf.Array([
            _annot(pdf, Subtype=pikepdf.Name.RichMedia),
            _annot(pdf, Subtype=pikepdf.Name("/3D"))]))
        pdf.Root.AcroForm = pikepdf.Dictionary(Fields=pikepdf.Array(),
                                               XFA=pdf.make_indirect(pikepdf.Stream(pdf, b"x")))
    _edit(plain, add)
    codes = _codes(E.scan_pdf(plain, no_av))
    assert codes["jbig2_images"]["severity"] == "info"     # common in scans
    assert codes["rich_media"]["count"] == 2 and codes["rich_media"]["severity"] == "warning"
    assert codes["xfa_form"]["severity"] == "warning"


def test_external_links_are_listed(plain, no_av):
    def links(pdf):
        pdf.pages[0].obj.Annots = pdf.make_indirect(pikepdf.Array([
            _annot(pdf, Subtype=pikepdf.Name.Link, A=_action(
                pdf, "URI", URI=pikepdf.String(url)))
            for url in ("https://login.example-bank.com/x", "http://evil.example/a",
                        "https://login.example-bank.com/y")]))
    _edit(plain, links)
    res = E.scan_pdf(plain, no_av)
    hit = _codes(res)["external_links"]
    assert hit["count"] == 3 and hit["severity"] == "info"
    assert "evil.example" in hit["detail"] and "login.example-bank.com" in hit["detail"]
    assert res["verdict"] == "no_active_content_found"


# --- attachments ---------------------------------------------------------------------------------


@pytest.mark.parametrize(("name", "data", "kind", "runs", "mismatch"), [
    ("notes.txt", b"just text", "text", False, False),
    ("report.pdf", b"%PDF-1.7 ...", "PDF document", False, False),
    ("invoice.pdf.exe", b"MZ\x90\x00" + b"\x00" * 60, "Windows program", True, True),
    ("readme.txt", b"MZ\x90\x00" + b"\x00" * 60, "Windows program", True, True),
    ("photo.jpg", b"\x7fELF\x02\x01", "Linux program", True, True),
    ("tool", b"\xcf\xfa\xed\xfe\x07", "macOS program", True, False),
    ("run.sh", b"#!/bin/sh\nrm -rf /", "script", True, False),
    ("budget.xls", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", "Office document (may contain macros)",
     False, False),
    ("archive.zip", b"PK\x03\x04", "ZIP archive (or modern Office file)", False, False),
    ("shortcut.lnk", b"L\x00\x00\x00\x01\x14\x02\x00", "Windows shortcut", True, False),
])
def test_attachment_types_are_detected_from_their_bytes(plain, no_av, name, data, kind,
                                                        runs, mismatch):
    _edit(plain, lambda pdf: _attach(pdf, name, data))
    res = E.scan_pdf(plain, no_av)
    (f,) = res["embedded_files"]
    assert (f["name"], f["size"], f["detected_type"]) == (name, len(data), kind)
    assert f["executable"] is runs and f["extension_mismatch"] is mismatch
    codes = _codes(res)
    if runs:
        assert codes["executable_attachments"]["severity"] == "high"
        assert res["verdict"] == "suspicious"
    if mismatch:
        assert codes["disguised_attachments"]["severity"] == "high"
    if not runs:
        assert res["verdict"] == "active_content"  # an attachment is still active content


def test_double_extensions_count_as_disguise(plain, no_av):
    _edit(plain, lambda pdf: _attach(pdf, "invoice.pdf.js", b"app.alert(1)"))
    res = E.scan_pdf(plain, no_av)
    assert res["embedded_files"][0]["executable"] is True     # .js runs on Windows
    assert res["embedded_files"][0]["extension_mismatch"] is True
    assert res["verdict"] == "suspicious"


def test_compressed_attachment_bombs_are_not_inflated(plain, no_av, monkeypatch):
    from paperboy.engine import scan
    monkeypatch.setattr(scan, "MAGIC_BYTES", 64)
    bomb = b"MZ" + b"\x00" * 50_000_000          # inflates hugely, compresses tiny
    _edit(plain, lambda pdf: _attach(pdf, "big.bin", bomb))
    (f,) = E.scan_pdf(plain, no_av)["embedded_files"]
    assert f["detected_type"] == "Windows program"   # from the first bytes only


def test_attachment_with_an_unusual_filter_is_unknown(plain, no_av):
    def add(pdf):
        _attach(pdf, "data.bin", b"x", compress=False)
        spec = pdf.Root.Names.EmbeddedFiles.Names[1]
        spec.EF.F.write(b"not really LZW", filter=pikepdf.Name.LZWDecode)
    _edit(plain, add, **RAW)
    (f,) = E.scan_pdf(plain, no_av)["embedded_files"]
    assert f["detected_type"] == "unknown"


# --- antivirus (ClamAV), with a stand-in clamscan --------------------------------------------


CLEAN_AV = """\
case "$1" in --version) echo "ClamAV 1.4.1/27450/Mon Oct 20 08:00:00 2026"; exit 0;; esac
for f in "$@"; do :; done
echo "$@" > "$(dirname "$0")/args.txt"
echo "$f: OK"; exit 0
"""
INFECTED_AV = """\
case "$1" in --version) echo "ClamAV 1.4.1/27450/Mon Oct 20 08:00:00 2026"; exit 0;; esac
for f in "$@"; do :; done
echo "$f: Pdf.Exploit.CVE_2009_0927-1 FOUND"; echo "$f: Win.Trojan.Agent-2 FOUND"; exit 1
"""
BROKEN_AV = """\
case "$1" in --version) echo "ClamAV 1.4.1/27450/Mon Oct 20 08:00:00 2026"; exit 0;; esac
echo "LibClamAV Error: cli_loaddb(): No supported database files found" >&2; exit 2
"""


def test_antivirus_clean(plain, settings, fake_binary):
    av = fake_binary("clamscan", CLEAN_AV)
    res = E.scan_pdf(plain, dataclasses.replace(settings, clamscan_path=av))
    assert res["antivirus"] == {"available": True, "engine": "ClamAV 1.4.1",
                                "database": "27450 (Mon Oct 20 08:00:00 2026)",
                                "infected": False, "findings": [], "error": None}
    assert res["verdict"] == "no_active_content_found"


def test_antivirus_detection_makes_it_suspicious(plain, settings, fake_binary):
    av = fake_binary("clamscan", INFECTED_AV)
    res = E.scan_pdf(plain, dataclasses.replace(settings, clamscan_path=av))
    assert res["antivirus"]["infected"] is True
    assert res["antivirus"]["findings"] == ["Pdf.Exploit.CVE_2009_0927-1",
                                            "Win.Trojan.Agent-2"]
    assert res["verdict"] == "suspicious"
    assert _codes(res)["antivirus_detection"]["severity"] == "high"


def test_antivirus_errors_are_reported_not_hidden(plain, settings, fake_binary):
    av = fake_binary("clamscan", BROKEN_AV)
    res = E.scan_pdf(plain, dataclasses.replace(settings, clamscan_path=av))
    assert res["antivirus"]["available"] is True
    assert res["antivirus"]["infected"] is None
    assert "No supported database" in res["antivirus"]["error"]


def test_antivirus_timeout_is_reported(plain, settings, fake_binary):
    av = fake_binary("clamscan", 'case "$1" in --version) echo "ClamAV 1.4.1/1/x"; '
                                 'exit 0;; esac\nsleep 5\n')
    res = E.scan_pdf(plain, dataclasses.replace(settings, clamscan_path=av,
                                                clamscan_timeout=1))
    assert res["antivirus"]["infected"] is None
    assert "timed out" in res["antivirus"]["error"]


def test_without_clamav_the_report_says_so(plain, no_av):
    res = E.scan_pdf(plain, no_av)
    assert res["antivirus"] == {"available": False, "engine": None, "database": None,
                                "infected": None, "findings": [], "error": None}


def test_antivirus_can_be_skipped(plain, settings, fake_binary):
    av = fake_binary("clamscan", INFECTED_AV)
    res = E.scan_pdf(plain, dataclasses.replace(settings, clamscan_path=av),
                     antivirus=False)
    assert res["antivirus"]["available"] is False


def test_encrypted_pdf_is_also_scanned_decrypted(plain, tmp_path, settings, fake_binary):
    locked = tmp_path / "locked.pdf"
    with pikepdf.open(plain) as pdf:
        pdf.save(locked, encryption=pikepdf.Encryption(user="pw", owner="pw", R=6))
    av = fake_binary("clamscan", CLEAN_AV)
    calls = tmp_path / "bin" / "calls.txt"
    av_script = (tmp_path / "bin" / "clamscan")
    av_script.write_text(av_script.read_text().replace(
        'echo "$@" > "$(dirname "$0")/args.txt"',
        f'echo "$f" >> "{calls}"; cp "$f" "{tmp_path}/seen-$(basename "$f")"'))
    res = E.scan_pdf(locked, dataclasses.replace(settings, clamscan_path=av), password="pw")
    scanned = [Path(p) for p in calls.read_text().split()]
    # The bytes that were checked, then a decrypted copy, both in a temporary folder
    # that is gone afterwards.
    assert [p.name for p in scanned] == ["input.pdf", "decrypted.pdf"]
    assert not any(p.exists() or p.is_relative_to(tmp_path) for p in scanned)
    assert (tmp_path / "seen-input.pdf").read_bytes() == locked.read_bytes()
    with pikepdf.open(tmp_path / "seen-decrypted.pdf") as copy:
        assert not copy.is_encrypted
    assert res["antivirus"]["infected"] is False


def test_password_is_required(tmp_path, no_av, make_encrypted):
    with pytest.raises(PdfUserError, match="supply its password"):
        E.scan_pdf(make_encrypted(), no_av)


def test_scan_writes_nothing(plain, no_av):
    before = sorted(p.name for p in plain.parent.iterdir())
    E.scan_pdf(plain, no_av)
    assert sorted(p.name for p in plain.parent.iterdir()) == before


# --- edge paths -------------------------------------------------------------------------------


def test_uncompressed_corrupt_and_binary_attachments(plain, no_av):
    def add(pdf):
        _attach(pdf, "plain.txt", b"hello there", compress=False)
        _attach(pdf, "broken.bin", b"x", compress=False)
        _attach(pdf, "blob.bin", b"\x00\x9f\x92\x96\xff", compress=False)
        pdf.Root.Names.EmbeddedFiles.Names[3].EF.F.write(b"not zlib",
                                                         filter=pikepdf.Name.FlateDecode)
        pdf.Root.Names.EmbeddedFiles.Names.extend([  # an entry with no stream at all
            pikepdf.String("empty"), pdf.make_indirect(pikepdf.Dictionary(
                Type=pikepdf.Name.Filespec, EF=pikepdf.Dictionary()))])
    _edit(plain, add, **RAW)
    files = {f["name"]: f["detected_type"] for f in E.scan_pdf(plain, no_av)["embedded_files"]}
    assert files == {"plain.txt": "text", "broken.bin": "unknown", "blob.bin": "binary data"}


def test_disguised_names_inside_object_streams_are_found():
    from paperboy.engine.scan import _escaped_names
    pdf = pikepdf.new()
    objstm = pikepdf.Stream(pdf, b"<< /S /J#61vaScript >>")
    objstm.Type = pikepdf.Name.ObjStm
    pdf.make_indirect(objstm)
    broken = pikepdf.Stream(pdf, b"")
    broken.write(b"garbage", filter=pikepdf.Name.FlateDecode)
    broken.Type = pikepdf.Name.ObjStm
    pdf.make_indirect(broken)
    from paperboy.engine.scan import _object_streams
    assert _escaped_names([b"", *_object_streams(pdf)]) == ["JavaScript"]


def test_script_hidden_in_a_next_array_or_loop_still_runs_on_open(plain, no_av):
    def add(pdf):
        goto = _action(pdf, "GoTo", D=pikepdf.Array([pdf.pages[0].obj, pikepdf.Name.Fit]))
        goto.Next = goto  # a loop with no script
        pdf.pages[0].obj.AA = pikepdf.Dictionary(
            C=goto,
            O=_action(pdf, "GoTo", D=pikepdf.Array([pdf.pages[0].obj, pikepdf.Name.Fit]),
                      Next=pikepdf.Array([_action(pdf, "JavaScript",
                                                  JS=pikepdf.String("x()"))])))
    _edit(plain, add)
    assert _codes(E.scan_pdf(plain, no_av))["javascript_on_open"]["count"] == 1


def test_antivirus_that_cannot_start_is_reported(plain, settings, tmp_path):
    res = E.scan_pdf(plain, dataclasses.replace(settings,
                                                clamscan_path=str(tmp_path / "missing")))
    assert res["antivirus"]["infected"] is None
    assert "could not start" in res["antivirus"]["error"]


# --- review findings: evasions and limits -----------------------------------------------------


def _js(pdf, code="x()"):
    return _action(pdf, "JavaScript", JS=pikepdf.String(code))


def test_document_level_scripts_run_on_open(plain, no_av):
    # The /Names /JavaScript tree runs when the file opens: the usual malware route.
    def add(pdf):
        leaf = pdf.make_indirect(pikepdf.Dictionary(
            Names=pikepdf.Array([pikepdf.String("b"), _js(pdf)])))
        pdf.Root.Names = pikepdf.Dictionary(JavaScript=pikepdf.Dictionary(
            Names=pikepdf.Array([pikepdf.String("a"), _js(pdf)]),
            Kids=pikepdf.Array([leaf, leaf])))     # a repeated kid is visited once
    _edit(plain, add)
    res = E.scan_pdf(plain, no_av)
    assert _codes(res)["javascript_on_open"]["count"] == 2
    assert res["verdict"] == "suspicious"


def test_script_inside_a_rendition_action_is_seen(plain, no_av):
    def add(pdf):
        pdf.pages[0].obj.Annots = pdf.make_indirect(pikepdf.Array([_annot(
            pdf, Subtype=pikepdf.Name.Screen, AA=pikepdf.Dictionary(PO=_action(
                pdf, "Rendition", OP=0, JS=pikepdf.String("x()"))))]))
    _edit(plain, add)
    codes = _codes(E.scan_pdf(plain, no_av))
    assert codes["javascript"]["count"] == 1
    assert codes["javascript_on_open"]["count"] == 1


@pytest.mark.parametrize("where", ["open", "page"])
def test_actions_that_run_without_a_click_are_active(plain, no_av, where):
    def add(pdf):
        uri = _action(pdf, "URI", URI=pikepdf.String("https://tracker.example/p"))
        if where == "open":
            pdf.Root.OpenAction = uri
        else:
            pdf.pages[0].obj.AA = pikepdf.Dictionary(O=uri)
    _edit(plain, add)
    res = E.scan_pdf(plain, no_av)
    hit = _codes(res)["automatic_actions"]
    assert hit["severity"] == "warning" and "URI" in hit["detail"]
    assert res["verdict"] == "active_content"


def test_clicks_and_page_jumps_are_not_automatic(plain, no_av):
    def add(pdf):
        pdf.Root.OpenAction = _action(pdf, "GoTo", D=pikepdf.Array([pdf.pages[1].obj,
                                                                    pikepdf.Name.Fit]))
        pdf.pages[0].obj.Annots = pdf.make_indirect(pikepdf.Array([_annot(
            pdf, Subtype=pikepdf.Name.Link, AA=pikepdf.Dictionary(
                U=_action(pdf, "URI", URI=pikepdf.String("https://example.com"))))]))
    _edit(plain, add)
    assert "automatic_actions" not in _codes(E.scan_pdf(plain, no_av))


def test_long_shared_action_chains_hit_one_budget(plain, no_av, monkeypatch):
    from paperboy.engine import scan
    monkeypatch.setattr(scan, "MAX_ACTION_STEPS", 50)
    def add(pdf):
        chain = None
        for _ in range(20):
            chain = _action(pdf, "GoTo", D=pikepdf.Array([pdf.pages[0].obj, pikepdf.Name.Fit]),
                            **({"Next": chain} if chain is not None else {}))
        pdf.pages[0].obj.AA = pikepdf.Dictionary(
            **{k: chain for k in ("O", "C", "PO", "PC", "PV", "PI")})
    _edit(plain, add)
    res = E.scan_pdf(plain, no_av)
    assert _codes(res)["action_chains_too_long"]["severity"] == "high"
    assert res["verdict"] == "suspicious"


def test_object_stream_bombs_are_decoded_within_a_budget(monkeypatch):
    from paperboy.engine import scan
    monkeypatch.setattr(scan, "OBJSTM_BUDGET", 100_000)
    pdf = pikepdf.new()
    for _ in range(5):
        bomb = pikepdf.Stream(pdf, b"")
        bomb.write(zlib.compress(zlib.compress(b"\0" * 20_000_000)),
                   filter=pikepdf.Array([pikepdf.Name.FlateDecode, pikepdf.Name.FlateDecode]))
        bomb.Type = pikepdf.Name.ObjStm
        pdf.make_indirect(bomb)
    assert sum(map(len, scan._object_streams(pdf))) <= 100_000


@pytest.mark.parametrize(("encode", "filters"), [
    (lambda d: base64.b16encode(d) + b">", ["/ASCIIHexDecode"]),
    (lambda d: base64.a85encode(d) + b"~>", ["/ASCII85Decode"]),
    (lambda d: zlib.compress(zlib.compress(d)), ["/FlateDecode", "/FlateDecode"]),
])
def test_attachments_in_other_encodings_are_identified(plain, no_av, encode, filters):
    exe = b"MZ\x90\x00" + b"\x00" * 60
    def add(pdf):
        _attach(pdf, "invoice.pdf", b"", compress=False)
        stream = pdf.Root.Names.EmbeddedFiles.Names[1].EF.F
        stream.write(encode(exe), filter=pikepdf.Array([pikepdf.Name(f) for f in filters]))
    _edit(plain, add, **RAW)
    (f,) = E.scan_pdf(plain, no_av)["embedded_files"]
    assert f["detected_type"] == "Windows program" and f["extension_mismatch"] is True


def test_attachments_with_predictors_or_damage_are_unknown(plain, no_av):
    def add(pdf):
        _attach(pdf, "a.bin", b"MZ", compress=False)
        _attach(pdf, "b.bin", b"MZ", compress=False)
        names = pdf.Root.Names.EmbeddedFiles.Names
        names[1].EF.F.write(zlib.compress(b"MZ"), filter=pikepdf.Name.FlateDecode,
                            decode_parms=pikepdf.Dictionary(Predictor=12, Columns=1))
        names[3].EF.F.write(b"\x7f\x7f~>", filter=pikepdf.Name.ASCII85Decode)
    _edit(plain, add, **RAW)
    assert [f["detected_type"] for f in E.scan_pdf(plain, no_av)["embedded_files"]] == \
        ["unknown", "unknown"]


@pytest.mark.parametrize("name", ["invoice.exe.", "invoice.exe ", r"C:\tmp\setup.iso",
                                  "budget.xlsm", "page.html"])
def test_names_that_run_on_windows(plain, no_av, name):
    _edit(plain, lambda pdf: _attach(pdf, name, b"data"))
    assert E.scan_pdf(plain, no_av)["embedded_files"][0]["executable"] is True


def test_attachment_under_a_platform_key_is_found(plain, no_av):
    def add(pdf):
        _attach(pdf, "x", b"MZ\x00\x00", compress=False)
        spec = pdf.Root.Names.EmbeddedFiles.Names[1]
        del spec.UF, spec.F
        spec.DOS = pikepdf.String("setup.exe")
        spec.EF = pikepdf.Dictionary(DOS=spec.EF.F)
    _edit(plain, add)
    (f,) = E.scan_pdf(plain, no_av)["embedded_files"]
    assert (f["name"], f["detected_type"]) == ("setup.exe", "Windows program")


def _pdf_bytes(tmp_path, change=None) -> bytes:
    inner = _plain(tmp_path / "inner.pdf")
    if change:
        _edit(inner, change)
    return inner.read_bytes()


def test_attached_pdfs_are_scanned_too(plain, no_av, tmp_path):
    inner = _pdf_bytes(tmp_path, lambda pdf: setattr(pdf.Root, "OpenAction", _js(pdf)))
    _edit(plain, lambda pdf: _attach(pdf, "statement.pdf", inner))
    res = E.scan_pdf(plain, no_av)
    hit = _codes(res)["attachment_active_content"]
    assert hit["severity"] == "high" and "javascript_on_open" in hit["detail"]
    assert res["verdict"] == "suspicious"


def test_clean_attached_pdf_adds_nothing(plain, no_av, tmp_path):
    _edit(plain, lambda pdf: _attach(pdf, "clean.pdf", _pdf_bytes(tmp_path)))
    codes = _codes(E.scan_pdf(plain, no_av))
    assert "attachment_active_content" not in codes and "attachment_not_scanned" not in codes


def test_attached_pdf_that_cannot_be_opened_is_reported(plain, no_av, make_encrypted):
    def add(pdf):
        _attach(pdf, "broken.pdf", b"%PDF-1.7 nothing else")
        _attach(pdf, "locked.pdf", make_encrypted().read_bytes())
    _edit(plain, add)
    assert _codes(E.scan_pdf(plain, no_av))["attachment_not_scanned"]["count"] == 2


def test_oversized_attached_pdf_is_not_scanned(plain, no_av, tmp_path):
    _edit(plain, lambda pdf: _attach(pdf, "big.pdf", _pdf_bytes(tmp_path)))
    tiny = dataclasses.replace(no_av, max_file_bytes=100)
    assert "attachment_not_scanned" in _codes(E.scan_pdf(plain, tiny))


def test_nesting_stops_at_a_depth(plain, no_av, tmp_path, monkeypatch):
    from paperboy.engine import scan
    monkeypatch.setattr(scan, "MAX_NESTED_DEPTH", 1)
    deepest = _pdf_bytes(tmp_path, lambda pdf: setattr(pdf.Root, "OpenAction", _js(pdf)))
    middle = _pdf_bytes(tmp_path, lambda pdf: _attach(pdf, "deep.pdf", deepest))
    _edit(plain, lambda pdf: _attach(pdf, "middle.pdf", middle))
    hit = _codes(E.scan_pdf(plain, no_av))["attachment_active_content"]
    assert "attachment_active_content" not in hit["detail"]   # the third level wasn't opened
    assert hit["severity"] == "warning"                       # middle's own attachment


def test_data_slipped_in_before_the_last_startxref(plain, no_av):
    data = plain.read_bytes()
    xref = data[data.rfind(b"startxref"):]
    plain.write_bytes(data + b"MZ" + b"\x90" * 500 + b"\n" + xref)
    hit = _codes(E.scan_pdf(plain, no_av))["data_after_end"]
    assert hit["count"] >= 500


def test_linearized_files_are_not_mistaken_for_hidden_data(plain, no_av):
    with pikepdf.open(plain, allow_overwriting_input=True) as pdf:
        pdf.save(plain, linearize=True)
    assert "data_after_end" not in _codes(E.scan_pdf(plain, no_av))


def test_scripts_the_document_does_not_reach_are_reported(plain, no_av):
    data = plain.read_bytes()
    plain.write_bytes(data + b"\n99 0 obj\n<< /S /JavaScript /JS (x()) >>\nendobj\n"
                      b"98 0 obj\n<< /S /Launch /F (cmd.exe) >>\nendobj\n")
    res = E.scan_pdf(plain, no_av)
    hit = _codes(res)["unreferenced_active_content"]
    assert hit["count"] == 2 and res["verdict"] == "active_content"


def test_scripts_the_document_uses_are_not_double_reported(plain, no_av):
    _edit(plain, lambda pdf: setattr(pdf.Root, "OpenAction", _js(pdf)))
    assert "unreferenced_active_content" not in _codes(E.scan_pdf(plain, no_av))


def test_xfa_with_scripts_is_suspicious(plain, no_av):
    def add(pdf):
        template = pikepdf.Stream(pdf, b"<template><script>app.alert(1)</script></template>")
        pdf.Root.AcroForm = pikepdf.Dictionary(Fields=pikepdf.Array(), XFA=pikepdf.Array([
            pikepdf.String("preamble"), pdf.make_indirect(pikepdf.Stream(pdf, b"<xdp>")),
            pikepdf.String("template"), pdf.make_indirect(template)]))
    _edit(plain, add)
    assert _codes(E.scan_pdf(plain, no_av))["xfa_form"]["severity"] == "high"


def test_links_without_a_host_are_summarised_not_listed(plain, no_av):
    def links(pdf):
        pdf.pages[0].obj.Annots = pdf.make_indirect(pikepdf.Array([
            _annot(pdf, Subtype=pikepdf.Name.Link, A=_action(
                pdf, "URI", URI=pikepdf.String(url)))
            for url in ("mailto:jane@example.com", "http://[broken", "wwwhttp")]))
    _edit(plain, links)
    detail = _codes(E.scan_pdf(plain, no_av))["external_links"]["detail"]
    assert "jane@example.com" not in detail
    assert "mailto: links" in detail and "an unreadable link" in detail


LIMITS_AV = """\
case "$1" in --version) echo "ClamAV 1.4.1/27450/x"; exit 0;; esac
for f in "$@"; do :; done
echo "$@" > "$(dirname "$0")/args.txt"
echo "$f: Heuristics.Limits.Exceeded.MaxFileSize FOUND"; exit 1
"""


def test_antivirus_limits_are_raised_and_overruns_are_not_infections(plain, settings,
                                                                     fake_binary, tmp_path):
    av = fake_binary("clamscan", LIMITS_AV)
    res = E.scan_pdf(plain, dataclasses.replace(settings, clamscan_path=av))
    args = (tmp_path / "bin" / "args.txt").read_text()
    assert "--max-filesize=201M" in args and "--alert-exceeds-max=yes" in args
    assert res["antivirus"]["infected"] is None and res["antivirus"]["findings"] == []
    assert "too large" in res["antivirus"]["error"]


def test_antivirus_errors_do_not_reveal_temporary_paths(plain, settings, fake_binary):
    av = fake_binary("clamscan", 'case "$1" in --version) echo "ClamAV 1/1/x"; exit 0;; esac\n'
                                 'for f in "$@"; do :; done\necho "$f: Can\'t open" >&2; exit 2\n')
    error = E.scan_pdf(plain, dataclasses.replace(settings, clamscan_path=av))["antivirus"]["error"]
    assert error == "<temp>/input.pdf: Can't open"


def test_a_failed_decrypted_copy_is_reported(plain, tmp_path, settings, fake_binary,
                                             monkeypatch):
    locked = tmp_path / "locked.pdf"
    with pikepdf.open(plain) as pdf:
        pdf.save(locked, encryption=pikepdf.Encryption(user="pw", owner="pw", R=6))
    def fail(*args, **kwargs):
        raise OSError("disk full")
    monkeypatch.setattr(pikepdf.Pdf, "save", fail)
    av = fake_binary("clamscan", CLEAN_AV)
    res = E.scan_pdf(locked, dataclasses.replace(settings, clamscan_path=av), password="pw")
    assert res["antivirus"]["infected"] is None
    assert "only the encrypted file was scanned" in res["antivirus"]["error"]


def test_decoder_gives_up_on_filters_it_does_not_handle():
    from paperboy.engine.scan import _decode
    pdf = pikepdf.new()
    s = pikepdf.Stream(pdf, b"")
    s.write(b"x", filter=pikepdf.Name.RunLengthDecode)
    assert _decode(s, 10) is None
    s.write(b"x", filter=pikepdf.Array([pikepdf.Name.FlateDecode] * 5))
    assert _decode(s, 10) is None
    s.write(b"abc")
    assert _decode(s, 2) == b"ab"


def test_only_so_many_attached_pdfs_are_opened(plain, no_av, tmp_path, monkeypatch):
    from paperboy.engine import scan
    monkeypatch.setattr(scan, "MAX_NESTED", 1)
    inner = _pdf_bytes(tmp_path)
    _edit(plain, lambda pdf: [_attach(pdf, f"{n}.pdf", inner) for n in "ab"])
    assert _codes(E.scan_pdf(plain, no_av))["attachment_not_scanned"]["count"] == 1


def test_data_hidden_behind_a_second_end_marker(plain, no_av):
    plain.write_bytes(plain.read_bytes() + b"MZ" + b"\x90" * 500 + b"\n%%EOF\n")
    assert _codes(E.scan_pdf(plain, no_av))["data_after_end"]["count"] >= 500
