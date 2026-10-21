"""MCP server adapter, in-process: policy (confinement, overwrite), wiring, errors.

The tool functions are called directly; ``@mcp.tool()`` returns them unchanged.
``test_server_stdio.py`` covers the same server over the real stdio protocol.
"""
from __future__ import annotations

import dataclasses
import logging

import pytest
from PIL import Image

import paperboy.server as S
from paperboy.config import Settings

from conftest import page_labels


@pytest.fixture(autouse=True)
def sandbox(settings, monkeypatch):
    monkeypatch.setattr(S, "SETTINGS", settings)
    return settings


@pytest.fixture
def outside(tmp_path_factory):
    d = tmp_path_factory.mktemp("outside")
    from reportlab.pdfgen import canvas
    c = canvas.Canvas(str(d / "secret.pdf"))
    c.drawString(72, 720, "SECRET")
    c.showPage()
    c.save()
    return d


def _png(path):
    Image.new("RGB", (20, 20), "red").save(path)
    return path


# --- every tool is confined to the root ---------------------------------------------

INPUT_TOOLS = {
    "pdf_info": {}, "get_metadata": {}, "check_pdf": {}, "extract_text": {},
    "encrypt_pdf": {"user_password": "pw"}, "decrypt_pdf": {"password": "pw"},
    "rearrange_pages": {"page_order": "1"}, "delete_pages": {"pages": "1"},
    "extract_pages": {"pages": "1"}, "rotate_pages": {"degrees": 90},
    "split_pdf": {}, "optimize_pdf": {}, "repair_pdf": {}, "flatten_pdf": {},
    "set_metadata": {"fields": {"title": "x"}}, "convert_to_pdf": {},
}


@pytest.mark.parametrize("tool", sorted(INPUT_TOOLS))
def test_every_tool_refuses_inputs_outside_root(tool, outside):
    with pytest.raises(S.ToolError, match="outside the allowed root"):
        getattr(S, tool)(input_path=str(outside / "secret.pdf"), **INPUT_TOOLS[tool])


@pytest.mark.parametrize("tool", sorted(t for t in INPUT_TOOLS if t not in {
    "pdf_info", "get_metadata", "check_pdf", "extract_text", "split_pdf"}))
def test_every_writer_refuses_outputs_outside_root(tool, doc, outside):
    args = dict(INPUT_TOOLS[tool], input_path=str(doc), output_path=str(outside / "o.pdf"))
    with pytest.raises(S.ToolError, match="outside the allowed root"):
        getattr(S, tool)(**args)
    assert not (outside / "o.pdf").exists()


def test_merge_refuses_paths_outside_root(doc, outside):
    with pytest.raises(S.ToolError, match="outside the allowed root"):
        S.merge_pdfs([str(doc), str(outside / "secret.pdf")], str(doc.with_name("m.pdf")))
    with pytest.raises(S.ToolError, match="outside the allowed root"):
        S.merge_pdfs([str(doc), str(doc)], str(outside / "m.pdf"))


def test_images_to_pdf_refuses_paths_outside_root(tmp_path, outside):
    img = _png(tmp_path / "a.png")
    with pytest.raises(S.ToolError, match="outside the allowed root"):
        S.images_to_pdf([str(img)], str(outside / "o.pdf"))


def test_split_refuses_output_dir_outside_root(doc, outside):
    with pytest.raises(S.ToolError, match="outside the allowed root"):
        S.split_pdf(str(doc), output_dir=str(outside / "parts"))
    assert not (outside / "parts").exists()


# --- default output names and the overwrite policy -------------------------------------


@pytest.mark.parametrize(("tool", "args", "suffix"), [
    ("encrypt_pdf", {"user_password": "pw"}, "_encrypted"),
    ("rearrange_pages", {"page_order": "2,1"}, "_reordered"),
    ("delete_pages", {"pages": "1"}, "_trimmed"),
    ("extract_pages", {"pages": "1"}, "_extract"),
    ("rotate_pages", {"degrees": 90}, "_rotated"),
    ("optimize_pdf", {}, "_optimized"),
    ("repair_pdf", {}, "_repaired"),
    ("set_metadata", {"fields": {"title": "T"}}, "_meta"),
])
def test_writers_default_to_a_suffixed_name_and_never_overwrite(tool, args, suffix, doc):
    res = getattr(S, tool)(input_path=str(doc), **args)
    expected = doc.with_name(f"doc{suffix}.pdf")
    assert res["output"] == str(expected) and expected.is_file()
    with pytest.raises(S.ToolError, match="already exists"):
        getattr(S, tool)(input_path=str(doc), **args)
    getattr(S, tool)(input_path=str(doc), overwrite=True, **args)


def test_server_default_overwrite_setting_applies(doc, sandbox, monkeypatch):
    S.rotate_pages(str(doc), 90)
    monkeypatch.setattr(S, "SETTINGS", dataclasses.replace(sandbox, default_overwrite=True))
    S.rotate_pages(str(doc), 180)  # allowed by PAPERBOY_ALLOW_OVERWRITE
    with pytest.raises(S.ToolError, match="already exists"):
        S.rotate_pages(str(doc), 90, overwrite=False)  # explicit flag wins


def test_writers_never_modify_their_input(doc):
    before = doc.read_bytes()
    S.rotate_pages(str(doc), 90)
    S.delete_pages(str(doc), "1")
    S.optimize_pdf(str(doc))
    assert doc.read_bytes() == before


def test_output_equal_to_input_is_refused(doc):
    with pytest.raises(S.ToolError, match="same as the input"):
        S.rotate_pages(str(doc), 90, output_path=str(doc), overwrite=True)


# --- inspection tools ---------------------------------------------------------------------


def test_inspection_tools(doc):
    assert S.pdf_info(str(doc))["pages"] == 5
    assert S.get_metadata(str(doc))["path"] == str(doc)
    assert S.check_pdf(str(doc))["healthy"] is True
    assert S.extract_text(str(doc), pages="2")["text"].count("PAGE-2") == 1


def test_extract_text_max_chars(doc):
    assert S.extract_text(str(doc), max_chars=5)["truncated"] is True


def test_missing_input_is_reported(tmp_path):
    with pytest.raises(S.ToolError, match="File not found"):
        S.pdf_info(str(tmp_path / "nope.pdf"))


# --- security tools -------------------------------------------------------------------------


def test_encrypt_then_decrypt_roundtrip(doc, tmp_path):
    enc = S.encrypt_pdf(str(doc), "pw", algorithm="AES-128")["output"]
    assert S.pdf_info(enc)["is_encrypted"] is True
    dec = S.decrypt_pdf(enc, "pw", output_path=str(tmp_path / "plain.pdf"))["output"]
    assert page_labels(dec)[0] == "PAGE-1"


def test_wrong_password_is_a_tool_error(make_encrypted):
    with pytest.raises(S.ToolError, match="Incorrect password"):
        S.decrypt_pdf(str(make_encrypted()), "nope")


# --- merge / split / images --------------------------------------------------------------------


def test_merge_with_password_keyed_by_the_path_given(make_pdf, make_encrypted, tmp_path,
                                                     monkeypatch):
    monkeypatch.chdir(tmp_path)
    make_pdf("a.pdf", 1)
    make_encrypted("locked.pdf", password="pw", pages=2)
    res = S.merge_pdfs(["a.pdf", "locked.pdf"], "merged.pdf", passwords={"locked.pdf": "pw"})
    assert res["pages"] == 3


@pytest.mark.parametrize("inputs", [[], ["only.pdf"], "a.pdf,b.pdf"])
def test_merge_needs_a_list_of_two(inputs, tmp_path):
    with pytest.raises(S.ToolError, match="at least two"):
        S.merge_pdfs(inputs, str(tmp_path / "m.pdf"))


def test_merge_refuses_to_overwrite(make_pdf, tmp_path):
    a, b = make_pdf("a.pdf", 1), make_pdf("b.pdf", 1)
    out = tmp_path / "m.pdf"
    S.merge_pdfs([str(a), str(b)], str(out))
    with pytest.raises(S.ToolError, match="already exists"):
        S.merge_pdfs([str(a), str(b)], str(out))
    S.merge_pdfs([str(a), str(b)], str(out), overwrite=True)


def test_merge_into_one_of_its_inputs_is_refused(make_pdf):
    a, b = make_pdf("a.pdf", 1), make_pdf("b.pdf", 1)
    with pytest.raises(S.ToolError, match="same as an input"):
        S.merge_pdfs([str(a), str(b)], str(a), overwrite=True)
    assert page_labels(a) == ["PAGE-1"]


def test_images_to_pdf_into_one_of_its_inputs_is_refused(tmp_path):
    img = _png(tmp_path / "a.png")
    before = img.read_bytes()
    with pytest.raises(S.ToolError, match="same as an input"):
        S.images_to_pdf([str(img)], str(img), overwrite=True)
    assert img.read_bytes() == before


def test_merge_creates_output_directory(make_pdf, tmp_path):
    a, b = make_pdf("a.pdf", 1), make_pdf("b.pdf", 1)
    S.merge_pdfs([str(a), str(b)], str(tmp_path / "new" / "m.pdf"))
    assert (tmp_path / "new" / "m.pdf").is_file()


def test_split_defaults_to_a_sibling_folder(doc):
    res = S.split_pdf(str(doc), ranges="1-2;3-")
    assert res["output_dir"] == str(doc.parent / "doc_split")
    assert res["count"] == 2


def test_split_into_named_folder(doc, tmp_path):
    res = S.split_pdf(str(doc), output_dir=str(tmp_path / "out"))
    assert res["count"] == 5


def test_images_to_pdf(tmp_path):
    imgs = [str(_png(tmp_path / f"{n}.png")) for n in range(2)]
    out = tmp_path / "imgs.pdf"
    assert S.images_to_pdf(imgs, str(out))["pages"] == 2
    with pytest.raises(S.ToolError, match="already exists"):
        S.images_to_pdf(imgs, str(out))


@pytest.mark.parametrize("inputs", [[], "a.png"])
def test_images_to_pdf_needs_a_list(inputs, tmp_path):
    with pytest.raises(S.ToolError, match="at least one image"):
        S.images_to_pdf(inputs, str(tmp_path / "o.pdf"))


def test_convert_text_defaults_to_same_name(tmp_path):
    src = tmp_path / "notes.txt"
    src.write_text("hello")
    assert S.convert_to_pdf(str(src))["output"] == str(tmp_path / "notes.pdf")


def test_set_metadata_needs_fields(doc):
    with pytest.raises(S.ToolError, match="at least one metadata field"):
        S.set_metadata(str(doc), {})


def test_flatten_without_qpdf_is_a_tool_error(doc, sandbox, monkeypatch):
    monkeypatch.setattr(S, "SETTINGS", dataclasses.replace(sandbox, qpdf_path=None))
    with pytest.raises(S.ToolError, match="needs the qpdf"):
        S.flatten_pdf(str(doc))


# --- error presentation -------------------------------------------------------------------------


def test_user_errors_keep_their_message(doc):
    with pytest.raises(S.ToolError, match="multiple of 90"):
        S.rotate_pages(str(doc), 45)


def test_internal_errors_are_logged_but_not_shown(doc, monkeypatch, caplog):
    def boom(*args, **kwargs):
        raise RuntimeError("internal detail /secret/path token=abc123")

    monkeypatch.setattr(S.engine, "pdf_info", boom)
    with caplog.at_level(logging.ERROR, logger="paperboy"):
        with pytest.raises(S.ToolError) as exc:
            S.pdf_info(str(doc))
    assert "abc123" not in str(exc.value)
    assert "Internal error in pdf_info" in str(exc.value)
    assert "abc123" in caplog.text  # still available to the operator


async def test_internal_errors_are_hidden_through_the_sdk(doc, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("token=abc123")

    monkeypatch.setattr(S.engine, "pdf_info", boom)
    with pytest.raises(Exception) as exc:
        await S.mcp.call_tool("pdf_info", {"input_path": str(doc)})
    assert "abc123" not in str(exc.value)


def test_guard_preserves_tool_signatures():
    import inspect
    assert list(inspect.signature(S.rotate_pages).parameters) == [
        "input_path", "degrees", "pages", "output_path", "password", "overwrite"]


# --- serve() ----------------------------------------------------------------------------------


def test_serve_warns_when_unconfined(monkeypatch, caplog):
    monkeypatch.setattr(S, "SETTINGS", Settings(root=None))
    monkeypatch.setattr(S.mcp, "run", lambda: None)
    with caplog.at_level(logging.WARNING, logger="paperboy"):
        S.serve()
    assert "PAPERBOY_ROOT is not set" in caplog.text


def test_serve_runs_quietly_when_confined(monkeypatch, caplog, sandbox):
    ran = []
    monkeypatch.setattr(S.mcp, "run", lambda: ran.append(True))
    with caplog.at_level(logging.WARNING, logger="paperboy"):
        S.serve()
    assert ran == [True]
    assert "PAPERBOY_ROOT is not set" not in caplog.text
