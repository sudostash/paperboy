"""Hidden data: inspect_hidden_data reports it, sanitize_pdf removes it."""
from __future__ import annotations

import dataclasses
import re

import pikepdf
import pytest
from reportlab.pdfgen import canvas

from paperboy import engine as E
from paperboy.errors import PdfUserError

from conftest import page_labels

CLEAN = {"metadata_fields": [], "xmp_metadata": False, "comment_authors": 0,
         "image_metadata": 0, "javascript": 0, "risky_actions": 0, "embedded_files": 0,
         "comments": 0, "private_app_data": 0, "thumbnails": 0, "previous_revisions": 0,
         "reader_usage_rights": False}


def _plain(path, pages=2):
    c = canvas.Canvas(str(path))
    c.setTitle("")
    c.setAuthor("")
    c.setSubject("")
    c.setCreator("")
    c.setProducer("")
    for n in range(pages):
        c.drawString(72, 720, f"PAGE-{n + 1}")
        c.showPage()
    c.save()
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        for key in list(pdf.trailer.Info.keys()):  # truly no metadata
            del pdf.trailer.Info[key]
        pdf.save(path)
    return path


def _action(pdf, kind, **extra):
    return pdf.make_indirect(pikepdf.Dictionary(Type=pikepdf.Name.Action,
                                                S=pikepdf.Name("/" + kind), **extra))


def _dirty(path):
    """One of every kind of hidden data, plus things that must survive."""
    _plain(path)
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        root, page = pdf.Root, pdf.pages[0]
        pdf.trailer.Info.Author = pikepdf.String("Jane Secret")
        pdf.trailer.Info.Producer = pikepdf.String("Secret Tool 9")
        with pdf.open_metadata(set_pikepdf_as_editor=False, update_docinfo=False) as meta:
            meta["dc:title"] = "Internal draft"
        js = _action(pdf, "JavaScript", JS=pikepdf.String("app.alert('hi')"))
        root.Names = pikepdf.Dictionary(
            JavaScript=pikepdf.Dictionary(Names=pikepdf.Array([pikepdf.String("init"), js])),
            EmbeddedFiles=pikepdf.Dictionary(Names=pikepdf.Array([
                pikepdf.String("budget.xlsx"),
                pdf.make_indirect(pikepdf.Dictionary(
                    Type=pikepdf.Name.Filespec, F=pikepdf.String("budget.xlsx"),
                    EF=pikepdf.Dictionary(F=pdf.make_indirect(pikepdf.Stream(pdf, b"x")))))])),
            Dests=pikepdf.Dictionary(Names=pikepdf.Array([
                pikepdf.String("intro"), pikepdf.Array([page.obj, pikepdf.Name.Fit])])))
        root.OpenAction = _action(pdf, "JavaScript", JS=pikepdf.String("this.print()"))
        page.obj.AA = pikepdf.Dictionary(O=_action(pdf, "Launch",
                                                   F=pikepdf.String("calc.exe")))
        page.obj.PieceInfo = pikepdf.Dictionary(Illustrator=pikepdf.Dictionary(
            Private=pikepdf.String("layer data")))
        page.obj.Thumb = pdf.make_indirect(pikepdf.Stream(pdf, b"thumb"))
        rect = pikepdf.Array([72, 72, 200, 100])
        annots = [
            # A web link: must survive.
            pikepdf.Dictionary(Type=pikepdf.Name.Annot, Subtype=pikepdf.Name.Link, Rect=rect,
                               A=_action(pdf, "URI", URI=pikepdf.String("https://e.com"))),
            # A link that launches a program: its action goes.
            pikepdf.Dictionary(Type=pikepdf.Name.Annot, Subtype=pikepdf.Name.Link, Rect=rect,
                               A=_action(pdf, "Launch", F=pikepdf.String("cmd.exe"))),
            # A link whose GoTo hides a JavaScript in its /Next chain.
            pikepdf.Dictionary(Type=pikepdf.Name.Annot, Subtype=pikepdf.Name.Link, Rect=rect,
                               A=_action(pdf, "GoTo", D=pikepdf.Array([page.obj, pikepdf.Name.Fit]),
                                         Next=_action(pdf, "JavaScript",
                                                      JS=pikepdf.String("x()")))),
            # A sticky-note comment with its author's name.
            pikepdf.Dictionary(Type=pikepdf.Name.Annot, Subtype=pikepdf.Name.Text, Rect=rect,
                               T=pikepdf.String("Jane Secret"),
                               Contents=pikepdf.String("Cut this before sending")),
            # An attached file.
            pikepdf.Dictionary(Type=pikepdf.Name.Annot, Subtype=pikepdf.Name.FileAttachment,
                               Rect=rect, FS=pdf.make_indirect(pikepdf.Dictionary(
                                   Type=pikepdf.Name.Filespec, F=pikepdf.String("notes.txt"),
                                   EF=pikepdf.Dictionary(F=pdf.make_indirect(
                                       pikepdf.Stream(pdf, b"notes")))))),
        ]
        page.obj.Annots = pdf.make_indirect(pikepdf.Array(
            [pdf.make_indirect(a) for a in annots]))
        root.Outlines = pdf.make_indirect(pikepdf.Dictionary(Type=pikepdf.Name.Outlines,
                                                             Count=0))
        pdf.save(path)
    return path


def _append_revision(path, old_title="Old secret title", new_title="Final title"):
    """Make *old_title* an earlier saved version: the file's metadata says *old_title*,
    then an incremental update (appended, as editors do) changes it to *new_title*."""
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        pdf.trailer.Info = pdf.make_indirect(pikepdf.Dictionary(
            Title=pikepdf.String(old_title)))
        pdf.save(path, object_stream_mode=pikepdf.ObjectStreamMode.disable,
                 compress_streams=False)
    data = path.read_bytes()
    with pikepdf.open(path) as pdf:
        root, size = pdf.Root.objgen[0], int(pdf.trailer.Size)
    prev = int(re.findall(rb"startxref\s+(\d+)", data)[-1])
    body = b"\n%d 0 obj\n<< /Title (%s) >>\nendobj\n" % (size, new_title.encode())
    xref_at = len(data) + len(body)
    path.write_bytes(data + body + (
        b"xref\n%d 1\n%010d 00000 n \ntrailer\n<< /Size %d /Root %d 0 R /Info %d 0 R "
        b"/Prev %d >>\nstartxref\n%d\n%%%%EOF\n" % (
            size, len(data) + 1, size + 1, root, size, prev, xref_at)))
    return path


@pytest.fixture
def dirty(tmp_path):
    return _dirty(tmp_path / "dirty.pdf")


# --- inspect_hidden_data ---------------------------------------------------------------------


def test_clean_pdf_reports_nothing(tmp_path, settings):
    res = E.inspect_hidden_data(_plain(tmp_path / "plain.pdf"), settings)
    assert res["found"] == CLEAN and res["clean"] is True


def test_reports_every_kind_of_hidden_data(dirty, settings):
    res = E.inspect_hidden_data(dirty, settings)
    found = res["found"]
    assert res["clean"] is False
    assert {"author", "producer", "title"} <= set(found["metadata_fields"])
    assert found["xmp_metadata"] is True
    assert found["javascript"] == 3          # named script, open action, chained script
    assert found["risky_actions"] == 2       # two Launch actions
    assert found["embedded_files"] == 2      # attached in the name tree and on the page
    assert found["comments"] == 1
    assert found["private_app_data"] == 1
    assert found["thumbnails"] == 1
    assert found["previous_revisions"] == 0


def test_reports_earlier_saved_versions(tmp_path, settings):
    path = _append_revision(_plain(tmp_path / "p.pdf"))
    assert E.inspect_hidden_data(path, settings)["found"]["previous_revisions"] == 1


def test_linearized_files_are_not_mistaken_for_revisions(tmp_path, settings):
    path = _plain(tmp_path / "p.pdf")
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        pdf.save(path, linearize=True)
    assert E.inspect_hidden_data(path, settings)["found"]["previous_revisions"] == 0


def test_inspect_writes_nothing(dirty, settings):
    before = dirty.read_bytes()
    E.inspect_hidden_data(dirty, settings)
    assert dirty.read_bytes() == before


# --- sanitize_pdf -----------------------------------------------------------------------------


def test_sanitize_removes_hidden_data_and_keeps_content(dirty, tmp_path, settings):
    out = tmp_path / "clean.pdf"
    res = E.sanitize_pdf(dirty, out, settings)
    assert res["output"] == str(out)
    assert res["removed"]["javascript"] == 3 and res["removed"]["risky_actions"] == 2
    assert res["removed"]["comments"] == 0        # comments are kept unless asked
    assert res["document_id_reset"] is True
    after = E.inspect_hidden_data(out, settings)["found"]
    assert after == {**CLEAN, "comments": 1}
    assert page_labels(out) == ["PAGE-1", "PAGE-2"]


def test_sanitize_keeps_links_destinations_and_outline(dirty, tmp_path, settings):
    out = tmp_path / "clean.pdf"
    E.sanitize_pdf(dirty, out, settings)
    with pikepdf.open(out) as pdf:
        subtypes = [str(a.Subtype) for a in pdf.pages[0].Annots]
        assert subtypes.count("/Link") == 3
        uris = [a.A.URI for a in pdf.pages[0].Annots if "/A" in a and "/URI" in a.A]
        assert uris == ["https://e.com"]
        goto = [a for a in pdf.pages[0].Annots if "/A" in a and a.A.S == "/GoTo"]
        assert goto and "/Next" not in goto[0].A      # the chained script is cut off
        assert "/Dests" in pdf.Root.Names and "/Outlines" in pdf.Root
        assert "/JavaScript" not in pdf.Root.Names
        assert "/EmbeddedFiles" not in pdf.Root.Names
        assert "/OpenAction" not in pdf.Root


def test_sanitize_can_remove_comments_too(dirty, tmp_path, settings):
    out = tmp_path / "clean.pdf"
    res = E.sanitize_pdf(dirty, out, settings, remove_comments=True)
    assert res["removed"]["comments"] == 1
    assert E.inspect_hidden_data(out, settings)["clean"] is True
    assert b"Jane Secret" not in out.read_bytes()


def test_options_keep_what_was_not_asked_for(dirty, tmp_path, settings):
    out = tmp_path / "partial.pdf"
    res = E.sanitize_pdf(dirty, out, settings, remove_metadata=False,
                         remove_scripts=False, remove_attachments=False)
    found = E.inspect_hidden_data(out, settings)["found"]
    assert "author" in found["metadata_fields"] and found["javascript"] == 3
    assert found["embedded_files"] == 2
    assert res["removed"]["metadata_fields"] == [] and res["removed"]["javascript"] == 0
    assert found["private_app_data"] == 0 and found["thumbnails"] == 0  # always removed


def test_earlier_versions_are_dropped(tmp_path, settings):
    path = _append_revision(_plain(tmp_path / "p.pdf"))
    assert b"Old secret title" in path.read_bytes()
    assert E.get_metadata(path, settings)["metadata"]["title"] == "Final title"
    out = tmp_path / "clean.pdf"
    # Metadata is kept, so the old title can only go if the earlier version does.
    res = E.sanitize_pdf(path, out, settings, remove_metadata=False)
    assert res["removed"]["previous_revisions"] == 1
    data = out.read_bytes()
    assert b"Old secret title" not in data
    assert E.get_metadata(out, settings)["metadata"]["title"] == "Final title"


def test_document_id_is_replaced(dirty, tmp_path, settings):
    with pikepdf.open(dirty) as pdf:
        old = bytes(pdf.trailer.ID[0])
    out = tmp_path / "clean.pdf"
    E.sanitize_pdf(dirty, out, settings)
    with pikepdf.open(out) as pdf:
        assert bytes(pdf.trailer.ID[0]) != old


def test_unused_resources_are_dropped(tmp_path, settings):
    path = _plain(tmp_path / "p.pdf", pages=1)
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        hidden = pdf.make_indirect(pikepdf.Stream(pdf, b"secret image bytes"))
        pdf.pages[0].obj.Resources.XObject = pikepdf.Dictionary(Unused=hidden)
        pdf.save(path)
    out = tmp_path / "clean.pdf"
    E.sanitize_pdf(path, out, settings)
    assert b"secret image bytes" not in out.read_bytes()


def test_sanitize_writes_a_new_file_and_leaves_the_input(dirty, tmp_path, settings):
    before = dirty.read_bytes()
    E.sanitize_pdf(dirty, tmp_path / "clean.pdf", settings)
    assert dirty.read_bytes() == before


# --- hostile structures ------------------------------------------------------------------------


def test_action_chain_cycles_are_survived(tmp_path, settings):
    path = _plain(tmp_path / "p.pdf", pages=1)
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        a = _action(pdf, "GoTo", D=pikepdf.Array([pdf.pages[0].obj, pikepdf.Name.Fit]))
        b = _action(pdf, "JavaScript", JS=pikepdf.String("x"), Next=a)
        a.Next = b
        pdf.Root.OpenAction = a
        pdf.save(path)
    assert E.inspect_hidden_data(path, settings)["found"]["javascript"] == 1
    E.sanitize_pdf(path, tmp_path / "clean.pdf", settings)
    assert E.inspect_hidden_data(tmp_path / "clean.pdf", settings)["found"]["javascript"] == 0


def test_object_graph_cycles_and_deep_nesting(tmp_path, settings):
    path = _plain(tmp_path / "p.pdf", pages=1)
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        loop = pdf.make_indirect(pikepdf.Dictionary())
        loop.Self = loop
        node = pikepdf.Dictionary(Loop=loop)
        for _ in range(450):  # qpdf refuses nesting deeper than 500
            node = pikepdf.Dictionary(Inner=node)
        pdf.Root.Deep = node
        pdf.save(path)
    assert E.inspect_hidden_data(path, settings)["clean"] is True


# --- policies -----------------------------------------------------------------------------------


def test_signed_documents_are_refused(dirty, tmp_path, settings):
    with pikepdf.open(dirty, allow_overwriting_input=True) as pdf:
        pdf.Root.Sig = pdf.make_indirect(pikepdf.Dictionary(
            Type=pikepdf.Name.Sig, ByteRange=pikepdf.Array([0, 1, 2, 3])))
        pdf.save(dirty)
    with pytest.raises(PdfUserError, match="digitally signed"):
        E.sanitize_pdf(dirty, tmp_path / "clean.pdf", settings)


def test_reader_usage_rights_are_reported_and_removed(dirty, tmp_path, settings):
    with pikepdf.open(dirty, allow_overwriting_input=True) as pdf:
        pdf.Root.Perms = pikepdf.Dictionary(UR3=pdf.make_indirect(pikepdf.Dictionary(
            Type=pikepdf.Name.Sig, ByteRange=pikepdf.Array([0, 1, 2, 3]))))
        pdf.save(dirty)
    assert E.inspect_hidden_data(dirty, settings)["found"]["reader_usage_rights"] is True
    res = E.sanitize_pdf(dirty, tmp_path / "clean.pdf", settings)
    assert res["removed"]["reader_usage_rights"] is True
    with pikepdf.open(tmp_path / "clean.pdf") as pdf:
        assert "/Perms" not in pdf.Root


def _protect(src, tmp_path, allow=None):
    out = tmp_path / "locked.pdf"
    with pikepdf.open(src) as pdf:
        pdf.save(out, encryption=pikepdf.Encryption(user="u", owner="o", R=6,
                                                    allow=allow or pikepdf.Permissions()))
    return out


def test_protected_input_stays_protected(dirty, tmp_path, settings):
    out = tmp_path / "clean.pdf"
    res = E.sanitize_pdf(_protect(dirty, tmp_path), out, settings, password="u")
    assert res["password_protected"] is True
    with pytest.raises(pikepdf.PasswordError):
        pikepdf.open(out)


def test_changing_the_file_needs_permission(dirty, tmp_path, settings):
    locked = _protect(dirty, tmp_path, pikepdf.Permissions(modify_other=False))
    assert E.inspect_hidden_data(locked, settings, password="u")["clean"] is False
    with pytest.raises(PdfUserError, match="does not allow changing.*owner"):
        E.sanitize_pdf(locked, tmp_path / "clean.pdf", settings, password="u")
    E.sanitize_pdf(locked, tmp_path / "clean.pdf", settings, password="o")


def test_page_cap(dirty, settings):
    with pytest.raises(PdfUserError, match="page limit"):
        E.inspect_hidden_data(dirty, dataclasses.replace(settings, max_pages=1))


# --- action chains, associated files, XFA, budgets ------------------------------------------


def _with_open_action(tmp_path, build):
    path = _plain(tmp_path / "p.pdf", pages=1)
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        pdf.Root.OpenAction = build(pdf)
        pdf.save(path)
    return path


def _goto(pdf, **extra):
    return _action(pdf, "GoTo", D=pikepdf.Array([pdf.pages[0].obj, pikepdf.Name.Fit]), **extra)


def test_next_arrays_keep_only_safe_actions(tmp_path, settings):
    path = _with_open_action(tmp_path, lambda pdf: _goto(pdf, Next=pikepdf.Array([
        _action(pdf, "JavaScript", JS=pikepdf.String("a()")),
        _goto(pdf, Next=_action(pdf, "Launch", F=pikepdf.String("x.exe"))),
        _action(pdf, "URI", URI=pikepdf.String("https://e.com"))])))
    out = tmp_path / "clean.pdf"
    E.sanitize_pdf(path, out, settings)
    with pikepdf.open(out) as pdf:
        chain = pdf.Root.OpenAction.Next
        assert [str(a.S) for a in chain] == ["/GoTo", "/URI"]
        assert "/Next" not in chain[0]                 # its Launch was cut off too
    assert E.inspect_hidden_data(out, settings)["clean"] is True


def test_next_array_of_only_scripts_is_removed(tmp_path, settings):
    path = _with_open_action(tmp_path, lambda pdf: _goto(pdf, Next=pikepdf.Array([
        _action(pdf, "JavaScript", JS=pikepdf.String("a()"))])))
    E.sanitize_pdf(path, tmp_path / "clean.pdf", settings)
    with pikepdf.open(tmp_path / "clean.pdf") as pdf:
        assert "/Next" not in pdf.Root.OpenAction


def test_long_safe_chains_and_safe_loops_are_left_alone(tmp_path, settings):
    def build(pdf):
        first = _goto(pdf)
        second = _goto(pdf)
        third = _goto(pdf, Next=first)              # loops back, nothing dangerous
        first.Next, second.Next = second, third
        return first
    path = _with_open_action(tmp_path, build)
    E.sanitize_pdf(path, tmp_path / "clean.pdf", settings)
    with pikepdf.open(tmp_path / "clean.pdf") as pdf:
        assert pdf.Root.OpenAction.Next.Next.Next.S == "/GoTo"


def test_associated_files_and_xfa_scripts_are_removed(tmp_path, settings):
    path = _plain(tmp_path / "p.pdf", pages=1)
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        spec = pdf.make_indirect(pikepdf.Dictionary(
            Type=pikepdf.Name.Filespec, F=pikepdf.String("data.xml"),
            EF=pikepdf.Dictionary(F=pdf.make_indirect(pikepdf.Stream(pdf, b"<x/>")))))
        pdf.Root.AF = pikepdf.Array([spec])
        pdf.Root.AcroForm = pikepdf.Dictionary(Fields=pikepdf.Array(),
                                               XFA=pdf.make_indirect(pikepdf.Stream(pdf, b"x")))
        pdf.save(path)
    found = E.inspect_hidden_data(path, settings)["found"]
    assert found["embedded_files"] == 1 and found["javascript"] == 1  # XFA counts
    E.sanitize_pdf(path, tmp_path / "clean.pdf", settings)
    with pikepdf.open(tmp_path / "clean.pdf") as pdf:
        assert "/AF" not in pdf.Root and "/XFA" not in pdf.Root.AcroForm
    assert E.inspect_hidden_data(tmp_path / "clean.pdf", settings)["clean"] is True


def test_pages_with_nothing_to_drop_keep_their_annotations(dirty, tmp_path, settings):
    out = tmp_path / "clean.pdf"
    E.sanitize_pdf(dirty, out, settings, remove_attachments=False, remove_comments=True)
    with pikepdf.open(out) as pdf:
        assert "/FileAttachment" in [str(a.Subtype) for a in pdf.pages[0].Annots]
        assert len(pdf.pages[1].obj.get("/Annots", [])) == 0


def test_object_budget_is_enforced(dirty, settings, monkeypatch):
    from paperboy.engine import sanitize
    monkeypatch.setattr(sanitize, "MAX_NODES", 10)
    with pytest.raises(PdfUserError, match="too many objects"):
        E.inspect_hidden_data(dirty, settings)


# --- review findings: verified in the output's bytes --------------------------------------


def _all_bytes(path, password=""):
    """Every byte of the file with all streams decompressed, as a single bytes object."""
    import io
    buf = io.BytesIO()
    with pikepdf.open(path, password=password) as pdf:
        pdf.save(buf, qdf=True, compress_streams=False,
                 stream_decode_level=pikepdf.StreamDecodeLevel.all,
                 object_stream_mode=pikepdf.ObjectStreamMode.disable)
    return buf.getvalue()


def _annot(pdf, **fields):
    return pdf.make_indirect(pikepdf.Dictionary(Type=pikepdf.Name.Annot,
                                                Rect=pikepdf.Array([72, 72, 90, 90]), **fields))


def _attachment(pdf, marker):
    return pdf.make_indirect(pikepdf.Dictionary(
        Type=pikepdf.Name.Filespec, F=pikepdf.String("f.bin"),
        EF=pikepdf.Dictionary(F=pdf.make_indirect(pikepdf.Stream(pdf, marker)))))


def test_dropped_annotations_do_not_survive_through_back_references(tmp_path, settings):
    path = _plain(tmp_path / "p.pdf", pages=1)
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        attach = _annot(pdf, Subtype=pikepdf.Name.FileAttachment,
                        FS=_attachment(pdf, b"SECRET_ATTACH_POPUP"), T=pikepdf.String("Jane"))
        popup = _annot(pdf, Subtype=pikepdf.Name.Popup, Parent=attach)
        attach.Popup = popup
        note = _annot(pdf, Subtype=pikepdf.Name.Text, T=pikepdf.String("Jane Author"),
                      Contents=pikepdf.String("SECRET_NOTE_BODY"))
        pdf.pages[0].obj.Annots = pdf.make_indirect(pikepdf.Array([attach, popup, note]))
        # A tagged PDF's structure tree pointing at the comment (OBJR).
        objr = pikepdf.Dictionary(Type=pikepdf.Name.OBJR, Obj=note, Pg=pdf.pages[0].obj)
        pdf.Root.StructTreeRoot = pdf.make_indirect(pikepdf.Dictionary(
            Type=pikepdf.Name.StructTreeRoot,
            K=pikepdf.Dictionary(S=pikepdf.Name.Annot, K=pikepdf.Array([objr]))))
        pdf.save(path)
    out = tmp_path / "clean.pdf"
    E.sanitize_pdf(path, out, settings, remove_comments=True)
    data = _all_bytes(out)
    assert b"SECRET_ATTACH_POPUP" not in data
    assert b"SECRET_NOTE_BODY" not in data and b"Jane Author" not in data
    with pikepdf.open(out) as pdf:
        assert "/StructTreeRoot" in pdf.Root  # the structure tree itself is kept


def test_embedded_files_anywhere_are_removed(tmp_path, settings):
    path = _plain(tmp_path / "p.pdf", pages=1)
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        screen = _annot(pdf, Subtype=pikepdf.Name.Screen, RichMediaContent=pikepdf.Dictionary(
            Assets=pikepdf.Dictionary(Names=pikepdf.Array([
                pikepdf.String("m.swf"), _attachment(pdf, b"SECRET_MEDIA_EF")]))))
        pdf.pages[0].obj.Annots = pdf.make_indirect(pikepdf.Array([screen]))
        pdf.save(path)
    assert E.inspect_hidden_data(path, settings)["found"]["embedded_files"] == 1
    out = tmp_path / "clean.pdf"
    res = E.sanitize_pdf(path, out, settings)
    assert res["removed"]["embedded_files"] == 1
    assert b"SECRET_MEDIA_EF" not in _all_bytes(out)


def test_scripts_under_any_key_are_removed(tmp_path, settings):
    path = _plain(tmp_path / "p.pdf", pages=1)
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        link = _annot(pdf, Subtype=pikepdf.Name.Link,
                      PA=_action(pdf, "JavaScript", JS=pikepdf.String("JS_IN_PA()")))
        pdf.pages[0].obj.Annots = pdf.make_indirect(pikepdf.Array([link]))
        pdf.Root.Custom = pikepdf.Array([_action(pdf, "Launch",
                                                 F=pikepdf.String("LAUNCH_IN_ARRAY.exe"))])
        pdf.save(path)
    out = tmp_path / "clean.pdf"
    res = E.sanitize_pdf(path, out, settings)
    assert res["removed"]["javascript"] == 1 and res["removed"]["risky_actions"] == 1
    data = _all_bytes(out)
    assert b"JS_IN_PA" not in data and b"LAUNCH_IN_ARRAY" not in data
    assert E.inspect_hidden_data(out, settings)["clean"] is True


def test_what_is_reported_removed_is_measured_not_assumed(dirty, tmp_path, settings,
                                                          monkeypatch):
    # If something can't be removed, sanitize must refuse rather than claim success.
    from paperboy.engine import sanitize
    monkeypatch.setattr(sanitize, "_neutralise_actions", lambda dicts, blocked: None)
    monkeypatch.setattr(sanitize, "_remove_action_keys", lambda dicts, blocked: None)
    with pytest.raises(PdfUserError, match="could not remove.*javascript"):
        E.sanitize_pdf(dirty, tmp_path / "clean.pdf", settings)
    assert not (tmp_path / "clean.pdf").exists()


def test_new_document_ids_are_random(tmp_path, settings):
    a, b = _plain(tmp_path / "a.pdf"), _plain(tmp_path / "b.pdf")
    E.sanitize_pdf(a, tmp_path / "a2.pdf", settings)
    E.sanitize_pdf(b, tmp_path / "b2.pdf", settings)  # within the same second
    with pikepdf.open(tmp_path / "a2.pdf") as x, pikepdf.open(tmp_path / "b2.pdf") as y:
        assert bytes(x.trailer.ID[0]) != bytes(y.trailer.ID[0])


def test_xmp_on_images_and_pages_is_found(tmp_path, settings):
    path = _plain(tmp_path / "p.pdf", pages=1)
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        xmp = pdf.make_indirect(pikepdf.Stream(pdf, b"<x:xmpmeta>SECRET_IMAGE_XMP</x:xmpmeta>"))
        pdf.pages[0].obj.Metadata = xmp
        pdf.save(path)
    assert E.inspect_hidden_data(path, settings)["found"]["xmp_metadata"] is True
    out = tmp_path / "clean.pdf"
    assert E.sanitize_pdf(path, out, settings)["removed"]["xmp_metadata"] is True
    assert b"SECRET_IMAGE_XMP" not in _all_bytes(out)


def test_large_ordinary_files_are_not_refused(tmp_path, settings, monkeypatch):
    # Numbers (e.g. font widths) must not count against the object budget: only
    # dictionaries, streams and arrays do.
    from paperboy.engine import sanitize
    monkeypatch.setattr(sanitize, "MAX_NODES", 200)
    path = _plain(tmp_path / "p.pdf", pages=1)
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        pdf.Root.Widths = pikepdf.Array(list(range(5000)))
        pdf.save(path)
    assert E.inspect_hidden_data(path, settings)["clean"] is True


def _jpeg_with_exif(path):
    from PIL import Image
    image = Image.new("RGB", (40, 30), (200, 50, 50))
    exif = Image.Exif()
    exif[0x013B] = "EXIF_ARTIST_NAME"      # Artist
    exif[0x010F] = "EXIF_CAMERA_MAKE"      # Make
    image.save(path, "JPEG", exif=exif.tobytes())
    return path


def test_camera_metadata_inside_jpeg_images_is_found_and_stripped(tmp_path, settings):
    path = _plain(tmp_path / "p.pdf", pages=1)
    jpeg = _jpeg_with_exif(tmp_path / "photo.jpg").read_bytes()
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        image = pikepdf.Stream(pdf, jpeg)
        image.Type, image.Subtype = pikepdf.Name.XObject, pikepdf.Name.Image
        image.Width, image.Height, image.BitsPerComponent = 40, 30, 8
        image.ColorSpace, image.Filter = pikepdf.Name.DeviceRGB, pikepdf.Name.DCTDecode
        pdf.pages[0].obj.Resources.XObject = pikepdf.Dictionary(Im1=pdf.make_indirect(image))
        pdf.pages[0].contents_add(pikepdf.Stream(pdf, b"q 40 0 0 30 72 72 cm /Im1 Do Q"))
        pdf.save(path)
    assert E.inspect_hidden_data(path, settings)["found"]["image_metadata"] == 1
    out = tmp_path / "clean.pdf"
    assert E.sanitize_pdf(path, out, settings)["removed"]["image_metadata"] == 1
    raw = out.read_bytes()
    assert b"EXIF_ARTIST_NAME" not in raw and b"EXIF_CAMERA_MAKE" not in raw
    with pikepdf.open(out) as pdf:  # the image still decodes
        import io

        from PIL import Image
        data = pdf.pages[0].Resources.XObject.Im1.read_raw_bytes()
        with Image.open(io.BytesIO(data)) as im:
            assert im.size == (40, 30) and im.convert("RGB").getpixel((20, 15))[0] > 150


def test_revisions_count_follows_the_xref_chain(tmp_path, settings):
    plain = _plain(tmp_path / "a.pdf")
    linear = _plain(tmp_path / "b.pdf")
    with pikepdf.open(linear, allow_overwriting_input=True) as pdf:
        pdf.save(linear, linearize=True)
    linear_updated = _append_revision(linear)
    eof_in_stream = _plain(tmp_path / "c.pdf")
    with pikepdf.open(eof_in_stream, allow_overwriting_input=True) as pdf:
        pdf.pages[0].contents_add(pikepdf.Stream(pdf, b"% %%EOF looks like a revision"))
        pdf.save(eof_in_stream, compress_streams=False)
    counts = {p.name: E.inspect_hidden_data(p, settings)["found"]["previous_revisions"]
              for p in (plain, linear_updated, eof_in_stream)}
    assert counts == {"a.pdf": 0, "b.pdf": 1, "c.pdf": 0}


def test_comment_authors_are_removed_with_metadata_even_when_comments_stay(dirty, tmp_path,
                                                                          settings):
    found = E.inspect_hidden_data(dirty, settings)["found"]
    assert found["comment_authors"] == 1
    out = tmp_path / "clean.pdf"
    res = E.sanitize_pdf(dirty, out, settings)  # comments kept by default
    assert res["removed"]["comment_authors"] == 1
    data = _all_bytes(out)
    assert b"Jane Secret" not in data and b"Cut this before sending" in data


def test_metadata_that_is_not_a_stream_is_not_an_internal_error(tmp_path, settings):
    path = _plain(tmp_path / "p.pdf", pages=1)
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        pdf.Root.Metadata = pikepdf.Dictionary(Bogus=1)
        pdf.save(path, fix_metadata_version=False)  # pikepdf would try to update it
    assert E.get_metadata(path, settings)["metadata"] == {}
    E.sanitize_pdf(path, tmp_path / "clean.pdf", settings)


def test_signatures_stored_inline_in_fields_are_detected(tmp_path, settings):
    path = _plain(tmp_path / "p.pdf", pages=1)
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        field = pdf.make_indirect(pikepdf.Dictionary(
            FT=pikepdf.Name.Sig, T=pikepdf.String("sig"),
            V=pikepdf.Dictionary(Type=pikepdf.Name.Sig, Contents=pikepdf.String("x"))))
        pdf.Root.AcroForm = pikepdf.Dictionary(Fields=pikepdf.Array([field]))
        pdf.save(path)
    with pytest.raises(PdfUserError, match="digitally signed"):
        E.sanitize_pdf(path, tmp_path / "clean.pdf", settings)


# --- re-review findings -----------------------------------------------------------------------


@pytest.mark.parametrize("target", ["root", "pages", "page", "struct"])
def test_dictionaries_that_merely_have_an_action_name_are_not_touched(tmp_path, settings,
                                                                     target):
    path = _plain(tmp_path / "p.pdf", pages=2)
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        if target == "struct":
            pdf.Root.StructTreeRoot = pdf.make_indirect(pikepdf.Dictionary(
                Type=pikepdf.Name.StructTreeRoot,
                K=pdf.make_indirect(pikepdf.Dictionary(
                    Type=pikepdf.Name.StructElem, S=pikepdf.Name.Launch,
                    K=pikepdf.Array([pikepdf.Dictionary(S=pikepdf.Name.P)])))))
        else:
            node = {"root": pdf.Root, "pages": pdf.Root.Pages, "page": pdf.pages[0].obj}[target]
            node.S = pikepdf.Name("/JavaScript" if target == "root" else "/Launch")
        pdf.save(path)
    out = tmp_path / "clean.pdf"
    E.sanitize_pdf(path, out, settings)
    assert page_labels(out) == ["PAGE-1", "PAGE-2"]  # opens, every page and its content
    if target == "struct":
        with pikepdf.open(out) as pdf:
            assert len(pdf.Root.StructTreeRoot.K.K) == 1


def test_jpeg_inside_another_compression_layer_is_stripped(tmp_path, settings):
    import zlib
    path = _plain(tmp_path / "p.pdf", pages=1)
    jpeg = _jpeg_with_exif(tmp_path / "photo.jpg").read_bytes()
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        image = pikepdf.Stream(pdf, zlib.compress(jpeg))
        image.Type, image.Subtype = pikepdf.Name.XObject, pikepdf.Name.Image
        image.Width, image.Height, image.BitsPerComponent = 40, 30, 8
        image.ColorSpace = pikepdf.Name.DeviceRGB
        image.Filter = pikepdf.Array([pikepdf.Name.FlateDecode, pikepdf.Name.DCTDecode])
        pdf.pages[0].obj.Resources.XObject = pikepdf.Dictionary(Im1=pdf.make_indirect(image))
        pdf.save(path)
    assert E.inspect_hidden_data(path, settings)["found"]["image_metadata"] == 1
    out = tmp_path / "clean.pdf"
    E.sanitize_pdf(path, out, settings)
    assert b"EXIF_ARTIST_NAME" not in _all_bytes(out)


def test_revisions_are_found_despite_trailing_junk(tmp_path, settings):
    path = _append_revision(_plain(tmp_path / "p.pdf"))
    path.write_bytes(path.read_bytes() + b"\n" + b"x" * 10_000)
    assert E.inspect_hidden_data(path, settings)["found"]["previous_revisions"] == 1


def test_popups_are_not_counted_as_comments(tmp_path, settings):
    path = _plain(tmp_path / "p.pdf", pages=1)
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        note = _annot(pdf, Subtype=pikepdf.Name.Text, Contents=pikepdf.String("hi"))
        popup = _annot(pdf, Subtype=pikepdf.Name.Popup, Parent=note)
        note.Popup = popup
        pdf.pages[0].obj.Annots = pdf.make_indirect(pikepdf.Array([note, popup]))
        pdf.save(path)
    assert E.inspect_hidden_data(path, settings)["found"]["comments"] == 1
    out = tmp_path / "clean.pdf"
    assert E.sanitize_pdf(path, out, settings, remove_comments=True)["removed"]["comments"] == 1
    with pikepdf.open(out) as pdf:
        assert len(pdf.pages[0].obj.get("/Annots", [])) == 0  # the popup went with it


# --- the JPEG segment parser on malformed input --------------------------------------------


@pytest.mark.parametrize(("data", "spans"), [
    (b"not a jpeg", []),
    (b"\xff\xd8\xff\xff\xe1\x00\x04ab\xff\xda", [(3, 9)]),         # fill byte before APP1
    (b"\xff\xd8\xff\xd0\xff\xe1\x00\x04ab\xff\xd9", [(4, 10)]),    # RST marker (no length)
    (b"\xff\xd8\x00", []),                                          # not a marker
    (b"\xff\xd8\xff\xe1\x00\x01", []),                              # impossible length
    (b"\xff\xd8\xff\xe1\x00\xff", []),                              # length past the end
])
def test_jpeg_metadata_parser(data, spans):
    from paperboy.engine.sanitize import _jpeg_metadata
    assert _jpeg_metadata(data) == spans


def test_revisions_through_cross_reference_streams_and_broken_pointers():
    from paperboy.engine.sanitize import _revisions
    assert _revisions(b"%PDF-1.7\nnothing to see") == 0
    # Two xref streams chained by /Prev, the newest last.
    data = (b"%PDF-1.7\n1 0 obj\n<< /Type /XRef /Size 3 >>\nstream\nx\nendstream\n"
            b"2 0 obj\n<< /Type /XRef /Size 3 /Prev 9 >>\nstream\nx\nendstream\n")
    newest = data.index(b"2 0 obj")
    data += b"startxref\n%d\n%%%%EOF\n" % newest
    assert _revisions(data) == 1
    assert _revisions(data.replace(b"/Prev 9", b"/Prev 999999")) == 1  # points nowhere


@pytest.mark.parametrize("wrap", ["hex", "a85", "lzw", "bomb"])
def test_wrapped_jpeg_filters(tmp_path, wrap, monkeypatch):
    import base64
    import binascii
    import zlib

    from paperboy.engine import sanitize
    jpeg = _jpeg_with_exif(tmp_path / "photo.jpg").read_bytes()
    pdf = pikepdf.new()
    encoded, outer = {
        "hex": (binascii.hexlify(jpeg) + b">", "/ASCIIHexDecode"),
        "a85": (base64.a85encode(jpeg) + b"~>", "/ASCII85Decode"),
        "lzw": (jpeg, "/LZWDecode"),
        "bomb": (zlib.compress(jpeg), "/FlateDecode"),
    }[wrap]
    stream = pikepdf.Stream(pdf, encoded)
    stream.Filter = pikepdf.Array([pikepdf.Name(outer), pikepdf.Name.DCTDecode])
    if wrap == "bomb":
        monkeypatch.setattr(sanitize, "MAX_JPEG_BYTES", 100)  # inflating past this stops
    found = sanitize._jpeg_bytes(stream)
    if wrap in ("hex", "a85"):
        assert found == jpeg
    else:
        assert found is None  # can't be unwrapped (LZW) or too large when inflated


def test_corrupt_wrapped_jpeg_cannot_be_unwrapped():
    from paperboy.engine import sanitize
    pdf = pikepdf.new()
    stream = pikepdf.Stream(pdf, b"not deflate data")
    stream.Filter = pikepdf.Array([pikepdf.Name.FlateDecode, pikepdf.Name.DCTDecode])
    assert sanitize._jpeg_bytes(stream) is None



# --- third-pass findings ------------------------------------------------------------------------


@pytest.mark.parametrize("extra", [
    {"P": pikepdf.String("x")}, {"K": 1}, {"Contents": pikepdf.String("x")},
    {"Parent": pikepdf.Dictionary()}, {"Type": pikepdf.Name("/Foo")},
], ids=["P", "K", "Contents", "Parent", "Type"])
def test_actions_with_extra_keys_are_still_found_and_removed(tmp_path, settings, extra):
    path = _plain(tmp_path / "p.pdf", pages=1)
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        js = pikepdf.Dictionary(S=pikepdf.Name.JavaScript, JS=pikepdf.String("HIDDEN_JS()"))
        launch = pikepdf.Dictionary(S=pikepdf.Name.Launch, F=pikepdf.String("HIDDEN.exe"))
        for key, value in extra.items():
            js[f"/{key}"], launch[f"/{key}"] = value, value
        pdf.Root.OpenAction = pdf.make_indirect(js)
        pdf.pages[0].obj.Annots = pdf.make_indirect(pikepdf.Array([_annot(
            pdf, Subtype=pikepdf.Name.Link, A=pdf.make_indirect(launch))]))
        pdf.save(path)
    found = E.inspect_hidden_data(path, settings)["found"]
    assert found["javascript"] == 1 and found["risky_actions"] == 1
    out = tmp_path / "clean.pdf"
    E.sanitize_pdf(path, out, settings)
    data = _all_bytes(out)
    assert b"HIDDEN_JS" not in data and b"HIDDEN.exe" not in data


def test_script_payload_on_a_structural_dictionary_is_removed_but_structure_kept(tmp_path,
                                                                              settings):
    path = _plain(tmp_path / "p.pdf", pages=2)
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        page = pdf.pages[0].obj
        page.S, page.JS = pikepdf.Name.JavaScript, pikepdf.String("PAGE_JS()")
        pdf.save(path)
    assert E.inspect_hidden_data(path, settings)["found"]["javascript"] == 1
    out = tmp_path / "clean.pdf"
    E.sanitize_pdf(path, out, settings)
    assert b"PAGE_JS" not in _all_bytes(out)
    assert page_labels(out) == ["PAGE-1", "PAGE-2"]


def _wrapped_image(tmp_path, filters, payload):
    path = _plain(tmp_path / "p.pdf", pages=1)
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        image = pikepdf.Stream(pdf, payload)
        image.Type, image.Subtype = pikepdf.Name.XObject, pikepdf.Name.Image
        image.Width, image.Height, image.BitsPerComponent = 40, 30, 8
        image.ColorSpace = pikepdf.Name.DeviceRGB
        image.Filter = pikepdf.Array([pikepdf.Name(f) for f in filters])
        pdf.pages[0].obj.Resources.XObject = pikepdf.Dictionary(Im1=pdf.make_indirect(image))
        # Drawn on the page, so it's a used resource (unused ones are dropped anyway).
        pdf.pages[0].contents_add(pikepdf.Stream(pdf, b"q 40 0 0 30 72 72 cm /Im1 Do Q"))
        pdf.save(path)
    return path


def test_jpeg_inside_several_wrappers_is_stripped(tmp_path, settings):
    import base64
    import zlib
    jpeg = _jpeg_with_exif(tmp_path / "photo.jpg").read_bytes()
    # Decoded in the listed order: ASCII85 first, then Flate, then the JPEG.
    path = _wrapped_image(tmp_path, ["/ASCII85Decode", "/FlateDecode", "/DCTDecode"],
                          base64.a85encode(zlib.compress(jpeg)) + b"~>")
    assert E.inspect_hidden_data(path, settings)["found"]["image_metadata"] == 1
    out = tmp_path / "clean.pdf"
    E.sanitize_pdf(path, out, settings)
    assert b"EXIF_ARTIST_NAME" not in _all_bytes(out)


def test_jpeg_that_cannot_be_unwrapped_is_reported_and_refused(tmp_path, settings):
    jpeg = _jpeg_with_exif(tmp_path / "photo.jpg").read_bytes()
    path = _wrapped_image(tmp_path, ["/LZWDecode", "/DCTDecode"], jpeg)  # not LZW, really
    assert E.inspect_hidden_data(path, settings)["found"]["image_metadata"] == 1
    with pytest.raises(PdfUserError, match="could not remove.*image_metadata.*can't be "
                                           "checked.*remove_metadata=false"):
        E.sanitize_pdf(path, tmp_path / "clean.pdf", settings)
    # Keeping metadata, nothing needs unwrapping, so the copy is written.
    E.sanitize_pdf(path, tmp_path / "kept.pdf", settings, remove_metadata=False)
