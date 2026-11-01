"""Outputs are written only where they should be, whole, and never through a link.

Every output path, given or default, is resolved and confined to the root; files are
written beside their destination and moved into place only once complete; a failed
call leaves what was there before; folders are made only for the first file.
"""
from __future__ import annotations

import dataclasses
import os

import anyio
import pikepdf
import pytest
from PIL import Image

import paperboy.server as S
from paperboy.engine import create

from conftest import HAS_QPDF, page_labels


@pytest.fixture(autouse=True)
def sandbox(settings, monkeypatch):
    monkeypatch.setattr(S, "SETTINGS", settings)
    return settings


@pytest.fixture
def outside(tmp_path_factory):
    return tmp_path_factory.mktemp("outside")


def _text(tmp_path):
    path = tmp_path / "notes.txt"
    path.write_text("hello\n")
    return path


SINGLE_OUTPUT = {
    "rotate_pages": lambda src, **kw: S.rotate_pages(input_path=str(src), degrees=90, **kw),
    "set_metadata": lambda src, **kw: S.set_metadata(input_path=str(src),
                                                     fields={"title": "T"}, **kw),
    "add_page_numbers": lambda src, **kw: S.add_page_numbers(input_path=str(src), **kw),
    "optimize_pdf": lambda src, **kw: S.optimize_pdf(input_path=str(src), **kw),
    "convert_to_pdf": lambda src, **kw: S.convert_to_pdf(
        input_path=str(_text(src.parent)), **kw),
}
if HAS_QPDF:
    SINGLE_OUTPUT["flatten_pdf"] = lambda src, **kw: S.flatten_pdf(input_path=str(src), **kw)


@pytest.mark.parametrize("target", ["missing", "existing"])
@pytest.mark.parametrize("tool", sorted(SINGLE_OUTPUT))
def test_a_link_at_the_default_output_name_cant_lead_outside_the_root(tool, target, doc,
                                                                      outside):
    default = os.fspath(SINGLE_OUTPUT[tool](doc)["output"])
    os.unlink(default)
    victim = outside / "victim.pdf"
    if target == "existing":
        victim.write_bytes(b"keep me")
    os.symlink(victim, default)
    with pytest.raises(S.ToolError, match="outside the allowed root"):
        SINGLE_OUTPUT[tool](doc, overwrite=True)
    assert victim.read_bytes() == b"keep me" if target == "existing" else \
        not victim.exists()


def test_a_link_at_the_default_split_folder_cant_lead_outside_the_root(doc, outside):
    os.symlink(outside, doc.with_name("doc_split"))
    with pytest.raises(S.ToolError, match="outside the allowed root"):
        S.split_pdf(input_path=str(doc))
    assert not list(outside.iterdir())


@pytest.mark.parametrize("tool", ["split_pdf", "render_pages"])
@pytest.mark.parametrize("problem", ["password", "pages"])
def test_a_refused_call_creates_no_folder(tool, problem, doc, make_encrypted, tmp_path):
    folder = tmp_path / "new" / "deep"
    if problem == "password":
        args = {"input_path": str(make_encrypted()), "password": "wrong"}
    else:
        args = {"input_path": str(doc), "ranges" if tool == "split_pdf" else "pages": "99"}
    with pytest.raises(S.ToolError):
        getattr(S, tool)(output_dir=str(folder), **args)
    assert not (tmp_path / "new").exists()


def test_split_checks_every_name_before_writing_any(doc, tmp_path):
    folder = tmp_path / "parts"
    folder.mkdir()
    (folder / "doc_part003.pdf").write_bytes(b"keep me")
    with pytest.raises(S.ToolError, match="already exists"):
        S.split_pdf(input_path=str(doc), output_dir=str(folder))
    assert sorted(p.name for p in folder.iterdir()) == ["doc_part003.pdf"]
    res = S.split_pdf(input_path=str(doc), output_dir=str(folder), overwrite=True)
    assert res["count"] == 5 and page_labels(folder / "doc_part003.pdf") == ["PAGE-3"]


def test_split_never_writes_through_a_link(doc, tmp_path):
    folder = tmp_path / "parts"
    folder.mkdir()
    os.symlink(doc, folder / "doc_part001.pdf")
    with pytest.raises(S.ToolError, match="not a regular file"):
        S.split_pdf(input_path=str(doc), output_dir=str(folder), overwrite=True)
    assert page_labels(doc) == [f"PAGE-{n}" for n in range(1, 6)]


def test_a_failing_split_removes_the_parts_it_wrote(doc, tmp_path, monkeypatch):
    from paperboy.engine import extract
    written = []
    real = extract._write

    def write(path, data):
        if written:
            raise OSError("disk full")
        written.append(path)
        real(path, data)
    monkeypatch.setattr(extract, "_write", write)
    with pytest.raises(S.ToolError):
        S.split_pdf(input_path=str(doc), output_dir=str(tmp_path / "parts"))
    assert not (tmp_path / "parts").exists()


@pytest.mark.skipif(not HAS_QPDF, reason="qpdf CLI not installed")
def test_a_failed_flatten_keeps_the_file_it_would_have_replaced(doc, fake_binary, sandbox,
                                                                 monkeypatch, tmp_path):
    out = tmp_path / "flat.pdf"
    out.write_bytes(b"keep me")
    failing = fake_binary("qpdf", "echo broken >&2\nexit 2\n")
    monkeypatch.setattr(S, "SETTINGS", dataclasses.replace(sandbox, qpdf_path=failing))
    with pytest.raises(S.ToolError, match="Flatten failed"):
        S.flatten_pdf(input_path=str(doc), output_path=str(out), overwrite=True)
    assert out.read_bytes() == b"keep me"
    assert not [p for p in tmp_path.iterdir() if p.name.startswith(".paperboy-")]


@pytest.mark.skipif(not HAS_QPDF, reason="qpdf CLI not installed")
def test_flatten_works_beside_a_planted_temporary_name(doc, tmp_path, outside):
    # The name qpdf used to write to: a link there no longer matters.
    os.symlink(outside / "stolen.pdf", tmp_path / ".flat.pdf.flattening")
    S.flatten_pdf(input_path=str(doc), output_path=str(tmp_path / "flat.pdf"))
    assert not (outside / "stolen.pdf").exists()
    assert page_labels(tmp_path / "flat.pdf")[0] == "PAGE-1"


# --- created PDFs are checked like inputs --------------------------------------------


def test_images_to_pdf_has_a_pixel_limit_per_call(tmp_path, monkeypatch):
    paths = [tmp_path / f"{n}.png" for n in range(3)]
    for p in paths:
        Image.new("RGB", (100, 100), "red").save(p)
    monkeypatch.setattr(create, "MAX_TOTAL_PIXELS", 25_000)
    with pytest.raises(S.ToolError, match="million pixels, over the 0 million limit"):
        S.images_to_pdf(input_paths=[str(p) for p in paths],
                        output_path=str(tmp_path / "album.pdf"))
    assert not (tmp_path / "album.pdf").exists()


def test_images_to_pdf_makes_one_page_per_image_in_order(tmp_path):
    colours = ["red", "green", "blue"]
    paths = []
    for n, colour in enumerate(colours):
        p = tmp_path / f"{n}.png"
        Image.new("RGBA" if n == 1 else "RGB", (40 + n, 30), colour).save(p)
        paths.append(str(p))
    res = S.images_to_pdf(input_paths=paths, output_path=str(tmp_path / "album.pdf"))
    assert res["pages"] == 3
    with pikepdf.open(tmp_path / "album.pdf") as pdf:
        widths = [float(page.mediabox[2]) for page in pdf.pages]
    assert widths == sorted(widths)  # 40, 41, 42 px at 150 dpi, in the order given


def test_a_created_pdf_over_the_size_limit_is_not_written(tmp_path, sandbox, monkeypatch):
    monkeypatch.setattr(S, "SETTINGS", dataclasses.replace(sandbox, max_file_bytes=200))
    with pytest.raises(S.ToolError, match="MB limit"):
        S.convert_to_pdf(input_path=str(_text(tmp_path)))
    assert not (tmp_path / "notes.pdf").exists()
    assert not [p for p in tmp_path.iterdir() if p.name.startswith(".paperboy-")]


def test_an_office_conversion_over_the_page_limit_is_not_written(tmp_path, sandbox,
                                                                 monkeypatch, fake_binary,
                                                                 make_pdf):
    produced = make_pdf("made.pdf", 6)
    soffice = fake_binary("soffice", f'''
for a in "$@"; do last="$prev"; prev="$a"; done
# --outdir <dir> <file>: copy a 6-page PDF there under the input's name
out="$last"; name=$(basename "$prev"); cp "{produced}" "$out/${{name%.*}}.pdf"
''')
    monkeypatch.setattr(S, "SETTINGS", dataclasses.replace(sandbox, soffice_path=soffice,
                                                           max_pages=5))
    doc = tmp_path / "report.docx"
    doc.write_bytes(b"not really")
    with pytest.raises(S.ToolError, match="6 pages, over the 5 page limit"):
        S.convert_to_pdf(input_path=str(doc))
    assert not (tmp_path / "report.pdf").exists()


# --- the server layer ----------------------------------------------------------------


def test_tools_without_passwords_run_in_a_worker_thread():
    import threading
    seen = []

    def tool(x: int = 0) -> dict:
        seen.append(threading.current_thread() is threading.main_thread())
        return {"x": x}
    assert anyio.run(lambda: S._asking(tool)(x=3)) == {"x": 3}
    assert seen == [False]  # (mcp 1.x would otherwise run it on the event loop)


def test_errors_while_checking_a_call_are_tool_errors(caplog):
    class Ctx:  # never reached: the path is refused first
        pass
    tool = S._asking(S.decrypt_pdf.__wrapped__)

    async def call():
        return await tool(input_path="bad\x00name.pdf", password="ask", paperboy_context=Ctx())
    with pytest.raises(S.ToolError, match="Internal error in decrypt_pdf"):
        anyio.run(call)


def test_a_pipeline_checks_a_watermark_image_before_any_step_runs(doc, tmp_path):
    with pytest.raises(S.ToolError, match=r"Step 2 \(add_watermark\): File not found"):
        S.run_pipeline(input_path=str(doc), steps=[
            {"tool": "rotate_pages", "args": {"degrees": 90}},
            {"tool": "add_watermark", "args": {"image_path": str(tmp_path / "gone.png")}}])
    assert not (tmp_path / "doc_processed.pdf").exists()
