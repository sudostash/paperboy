"""markdown_to_pdf: Markdown laid out as a PDF."""
from __future__ import annotations

import pikepdf
import pytest
from PIL import Image

import paperboy.engine.create as C
import paperboy.engine.markdown_pdf as M
from paperboy.errors import PdfUserError

from conftest import page_labels


def _make(tmp_path, settings, text, **kwargs):
    out = tmp_path / "out.pdf"
    kwargs.setdefault("base", tmp_path)
    return M.markdown_to_pdf(text, out, settings, **kwargs), out


def _text(out) -> str:
    return "\n".join(page_labels(out))


def _fonts(out) -> set:
    names = set()
    with pikepdf.open(out) as pdf:
        for page in pdf.pages:
            fonts = page.Resources.get("/Font") or {}
            for key in fonts.keys():     # no .values() on pikepdf 8
                names.add(str(fonts[key].BaseFont).lstrip("/").split("+")[-1])
    return names


def _links(out) -> list:
    with pikepdf.open(out) as pdf:
        return [(a.get("/A") or {}).get("/URI") or str(a.get("/Dest", ""))
                for page in pdf.pages for a in page.get("/Annots", [])]


def _outline(out) -> list:
    with pikepdf.open(out) as pdf, pdf.open_outline() as outline:
        def walk(items, depth):
            return [x for item in items
                    for x in [(depth, item.title), *walk(item.children, depth + 1)]]
        return walk(outline.root, 0)


def _images(page) -> list:
    objects = page.Resources.XObject
    return [objects[k] for k in objects.keys() if objects[k].Subtype == "/Image"]


def _png(path, size=(40, 20), mode="RGB", **kwargs):
    Image.new(mode, size, "red" if mode == "RGB" else (255, 0, 0, 128)).save(path, **kwargs)
    return path


DOC = """# Report

Some **bold**, *italic*, `code`, ~~old~~ text with a [site](https://example.com),
[mail](mailto:a@example.com), a [jump](#costs-and-more) and a [nowhere](#nope).
Raw <b>html</b> & <script>x()</script> stays text; [file](file:///etc/passwd) isn't a link.

## Costs and more

| Item | Amount | Note |
|:-----|-------:|:----:|
| Fuel | $1,200 | *est* |

1. First
2. Second
   - nested

> Quoted **words**

```
code  block
```

---

### Deep
"""


def test_a_document_is_laid_out(tmp_path, settings):
    res, out = _make(tmp_path, settings, DOC)
    assert res == {"output": str(out), "pages": 1, "title": "Report", "headings": 3,
                   "images": 0, "images_skipped": [], "unicode_font": False,
                   "missing_characters": []}
    text = _text(out)
    for part in ("Report", "bold", "Fuel", "$1,200", "First", "nested", "Quoted words",
                 "code  block", "<b>html</b> &", "<script>x()</script>", "Deep"):
        assert part in text, part
    for marker in ("**", "~~", "| Item", "```", "](https"):
        assert marker not in text, marker
    assert _fonts(out) <= {"Helvetica", "Helvetica-Bold", "Helvetica-Oblique", "Courier"}


def test_only_safe_links_are_clickable(tmp_path, settings):
    _, out = _make(tmp_path, settings, DOC)
    # Web, email, and the heading (a page destination); not file: or a missing anchor.
    links = _links(out)
    assert links[:2] == ["https://example.com", "mailto:a@example.com"]
    assert len(links) == 3 and "/Fit" in links[2]


def test_headings_become_bookmarks_and_the_title(tmp_path, settings):
    res, out = _make(tmp_path, settings, "# One\n\n### Three\n\n# One\n\n## Two\n\n#### !!!")
    assert _outline(out) == [(0, "One"), (1, "Three"), (0, "One"), (1, "Two"),
                             (2, "!!!")]   # never more than one level deeper
    with pikepdf.open(out) as pdf:
        assert pdf.Root.PageMode == "/UseOutlines"
        assert str(pdf.docinfo.Title) == "One" and str(pdf.docinfo.Creator) == "Paperboy"
    assert M._slug("!!!", set(), {}) == "section"
    taken, counts = set(), {}
    assert [M._slug(t, taken, counts) for t in ("A b", "a-b-1", "A b", "A b")] == \
        ["a-b", "a-b-1", "a-b-2", "a-b-3"]   # a heading named like a repeat isn't reused


def test_sibling_headings_stay_siblings_in_the_outline(tmp_path, settings):
    _, out = _make(tmp_path, settings, "### a\n\n### b\n\n#### c\n\n# d\n\n### e")
    assert _outline(out) == [(0, "a"), (0, "b"), (1, "c"), (0, "d"), (1, "e")]


def test_headings_inside_lists_and_long_headings_are_bookmarked(tmp_path, settings):
    long = "word " * 600
    text = f"- # Inside\n\n[go](#inside) [far](#{'word-' * 3})\n\n# {long}\n\n# Last {long}"
    res, out = _make(tmp_path, settings, text)
    assert [label for _, label in _outline(out)][0] == "Inside"
    assert len(_outline(out)) == 3 and all(len(t) <= 200 for _, t in _outline(out))
    assert res["pages"] >= 2 and len(_links(out)) == 1     # "#word-word-word-" is no heading


def test_many_repeated_headings_stay_fast(tmp_path, settings):
    import time
    start = time.monotonic()
    taken, counts = set(), {}
    for _ in range(50_000):
        M._slug("a", taken, counts)
    assert time.monotonic() - start < 2


def test_links_to_headings_with_accents(tmp_path, settings):
    _, out = _make(tmp_path, settings, "# \u00dcber uns\n\n[go](#\u00fcber-uns)")
    assert len(_links(out)) == 1


def test_title_and_author_can_be_given(tmp_path, settings):
    res, out = _make(tmp_path, settings, "plain text", title="Plan\x07", author="Ana")
    assert res["title"] == "Plan" and res["headings"] == 0
    with pikepdf.open(out) as pdf:
        assert str(pdf.docinfo.Title) == "Plan" and str(pdf.docinfo.Author) == "Ana"
        assert pdf.Root.get("/PageMode") != "/UseOutlines"   # no bookmarks to show
    assert M.markdown_to_pdf("no heading", tmp_path / "b.pdf", settings)["title"] is None


def test_other_scripts_use_the_bundled_fonts(tmp_path, settings):
    res, out = _make(tmp_path, settings, "# Ελληνικά\n\nПривет and `код` and 中文 and 中")
    assert res["unicode_font"] is True and res["missing_characters"] == ["中", "文"]
    text = _text(out)
    assert "Ελληνικά" in text and "Привет" in text and "??" in text
    # ReportLab names its default font on every page, though nothing is drawn in it.
    assert _fonts(out) - {"Helvetica"} == {"DejaVuSans", "DejaVuSans-Bold", "DejaVuSansMono"}


def test_characters_the_code_font_lacks_are_reported(tmp_path, settings):
    res, out = _make(tmp_path, settings, "```\n☃ अ\n```")   # snowman, Devanagari
    assert "अ" in res["missing_characters"]


def test_control_characters_are_dropped(tmp_path, settings):
    _, out = _make(tmp_path, settings, "a\x00b\x1bc\u200bd")
    assert "abcd" in _text(out)


def test_an_empty_document_is_one_blank_page(tmp_path, settings):
    res, out = _make(tmp_path, settings, "")
    assert res["pages"] == 1 and res["title"] is None
    assert M.markdown_to_pdf("\ufeff  \n", tmp_path / "e.pdf", settings)["pages"] == 1


def test_page_size(tmp_path, settings):
    _, out = _make(tmp_path, settings, "x", page_size="a4")
    with pikepdf.open(out) as pdf:
        assert [round(float(v)) for v in pdf.pages[0].MediaBox] == [0, 0, 595, 842]
    with pytest.raises(PdfUserError, match="page_size"):
        _make(tmp_path, settings, "x", page_size="legal")


def test_long_code_wraps_and_long_documents_split(tmp_path, settings):
    code = "\n".join(f"line {n} " + "x" * 200 for n in range(80))
    quote = "> " + "quoted words " * 400
    res, out = _make(tmp_path, settings, f"```\n{code}\n```\n\n{quote}\n")
    assert res["pages"] >= 3
    text = _text(out)
    assert "line 79" in text and "quoted words" in text


def test_limits(tmp_path, settings, monkeypatch):
    with pytest.raises(PdfUserError, match="over the 1,000,000 limit"):
        _make(tmp_path, settings, "x" * (M.MAX_CHARS + 1))
    wide = "|" + "|".join("c" * 31) + "|\n|" + "|".join(["-"] * 31) + "|\n"
    with pytest.raises(PdfUserError, match="31 columns"):
        _make(tmp_path, settings, wide)
    tall = "| a |\n|---|\n| " + "word " * 20000 + "|\n"
    with pytest.raises(PdfUserError, match="too tall to fit on a page"):
        _make(tmp_path, settings, tall)
    import dataclasses
    with pytest.raises(PdfUserError, match="more than 2 pages"):
        _make(tmp_path, dataclasses.replace(settings, max_pages=2),
              "\n\n".join(["para " * 400] * 20))
    monkeypatch.setattr(M, "BUILD_TIME_LIMIT_S", -1)
    with pytest.raises(PdfUserError, match="took over -1 s"):
        _make(tmp_path, settings, "x")
    assert not (tmp_path / "out.pdf").exists()


def test_nesting_too_deep_is_refused_not_dropped(tmp_path, settings):
    with pytest.raises(PdfUserError, match="nests lists or quotes too deeply"):
        _make(tmp_path, settings, "> " * 45 + "deep\n\nafter")
    lists = "".join("  " * n + "- level\n" for n in range(19))
    res, out = _make(tmp_path, settings, "> " * 30 + "deep\n\n" + lists)
    assert "deep" in _text(out) and _text(out).count("level") == 19


def _right_edge(out) -> float:
    """The rightmost x of anything drawn, from PDFium's object bounds."""
    import pypdfium2 as pdfium
    doc = pdfium.PdfDocument(str(out))
    try:
        # get_bounds since pypdfium2 5; get_pos before.
        return max((getattr(obj, "get_bounds", None) or obj.get_pos)()[2]
                   for page in doc for obj in page.get_objects())
    finally:
        doc.close()


def test_deep_content_stays_on_the_page(tmp_path, settings):
    _png(tmp_path / "wide.png", size=(3000, 200))
    deep = "".join("  " * n + "- level\n" for n in range(12))
    pad = "  " * 12
    table = f"\n{pad}| a | b |\n{pad}|---|---|\n{pad}| {'x ' * 40}| y |\n"
    code = f"\n{pad}```\n{pad}{'z' * 300}\n{pad}```\n"
    text = deep + f"\n{pad}![w](wide.png)\n" + table + code + "\n" + "> " * 30 + "![w](wide.png)"
    res, out = _make(tmp_path, settings, text)
    assert res["images"] == 2
    assert _right_edge(out) <= 612 - 54 + 1


def test_table_cells_short_rows_and_pictures(tmp_path, settings):
    _png(tmp_path / "p.png")
    res, out = _make(tmp_path, settings,
                     "| a | b |\n|---|---|\n| ![pic](p.png) |\n\n# ![logo](p.png) Title")
    text = _text(out)
    assert "[pic]" in text and "[logo] Title" in text
    assert res["images"] == 0 and res["title"] == "logo Title"


# --- pictures ---------------------------------------------------------------------------


def test_pictures_are_embedded_from_files_here(tmp_path, settings):
    _png(tmp_path / "a.png", size=(2000, 1000))
    _png(tmp_path / "b.png", mode="RGBA")
    Image.new("RGB", (30, 30), "blue").save(tmp_path / "c.jpg")
    Image.new("P", (30, 30)).save(tmp_path / "d.gif", transparency=0)
    (tmp_path / "sub").mkdir()
    _png(tmp_path / "sub" / "e.png")
    text = (f"Text ![a](a.png) between ![b](b.png)\n\n![c]({tmp_path / 'c.jpg'})\n\n"
            "![d](d.gif) ![e](sub/e.png) ![f](file:sub/e.png)")
    res, out = _make(tmp_path, settings, text)
    assert res["images"] == 5 and res["images_skipped"] == []
    assert "![f](file:sub/e.png)" in _text(out)     # file: links are refused, kept as text
    assert "Text" in _text(out) and "between" in _text(out)
    with pikepdf.open(out) as pdf:
        images = [x for p in pdf.pages for x in _images(p)]
        assert max(int(i.Width) for i in images) == 2000    # under the 2400 px cap: kept
        assert any("/SMask" in i for i in images)            # transparency kept


def test_big_pictures_are_scaled_down(tmp_path, settings):
    _png(tmp_path / "big.png", size=(5000, 100))
    _, out = _make(tmp_path, settings, "![big](big.png)")
    with pikepdf.open(out) as pdf:
        (image,) = _images(pdf.pages[0])
        assert int(image.Width) == 2400


@pytest.mark.parametrize(("source", "reason"), [
    ("https://example.com/a.png", "links aren't fetched"),
    ("data:image/png;base64,AAAA", "links aren't fetched"),
    ("missing.png", "not found"),
    ("notes.txt", "not a picture file"),
    ("fake.png", "not a readable picture"),
    ("folder", "not found"),
    ("../../outside.png", "outside the allowed root"),
])
def test_pictures_that_cant_be_used_are_listed(tmp_path, settings, source, reason):
    (tmp_path / "notes.txt").write_text("x")
    (tmp_path / "fake.png").write_bytes(b"not a png")
    (tmp_path / "folder").mkdir()
    res, out = _make(tmp_path, settings, f"See ![alt\ntext]({source})")
    assert res["images"] == 0
    (skipped,) = res["images_skipped"]
    assert skipped["source"] == source and reason in skipped["reason"]
    assert "[alt text]" in _text(out)


def test_relative_pictures_need_a_base(tmp_path, settings):
    _png(tmp_path / "p.png")
    res, _ = _make(tmp_path, settings, "![p](p.png)", base=None)
    assert "relative path needs" in res["images_skipped"][0]["reason"]


def test_picture_limits(tmp_path, settings, monkeypatch):
    _png(tmp_path / "p.png")
    monkeypatch.setattr(M, "MAX_IMAGES", 1)
    res, _ = _make(tmp_path, settings, "![1](p.png)\n\n![2](p.png)")
    assert res["images"] == 1 and "over 1 pictures" in res["images_skipped"][0]["reason"]
    monkeypatch.setattr(M, "MAX_IMAGES", 100)
    monkeypatch.setattr(C, "MAX_TOTAL_PIXELS", 1000)
    res, _ = _make(tmp_path, settings, "![1](p.png)\n\n![2](p.png)")
    assert res["images"] == 1 and "too many pixels" in res["images_skipped"][0]["reason"]
    import dataclasses
    with open(tmp_path / "p.png", "ab") as f:      # PNG readers ignore what follows
        f.write(b"\0" * 100_000)
    res, _ = _make(tmp_path, dataclasses.replace(settings, max_file_bytes=50_000),
                   "![1](p.png)")
    assert "file-size limit" in res["images_skipped"][0]["reason"]


def test_a_decompression_bomb_is_skipped(tmp_path, settings, monkeypatch):
    _png(tmp_path / "p.png", size=(100, 100))
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 10)
    res, _ = _make(tmp_path, settings, "![p](p.png)")
    assert res["images"] == 0 and res["images_skipped"]


def test_only_fifty_skipped_pictures_are_listed(tmp_path, settings):
    res, _ = _make(tmp_path, settings, " ".join(f"![{n}](m{n}.png)" for n in range(60)))
    assert len(res["images_skipped"]) == 50


def test_a_long_picture_source_is_shortened(tmp_path, settings):
    res, _ = _make(tmp_path, settings, f"![x]({'a' * 300}.png)")
    assert res["images_skipped"][0]["source"] == "a" * 200 + "..."


# --- the tool ---------------------------------------------------------------------------


@pytest.fixture
def server(settings, monkeypatch):
    import paperboy.server as S
    monkeypatch.setattr(S, "SETTINGS", settings)
    return S


def test_the_tool_takes_a_file_or_text(server, tmp_path):
    (tmp_path / "notes").mkdir()
    _png(tmp_path / "notes" / "p.png")
    src = tmp_path / "notes" / "plan.md"
    src.write_text("﻿# Plan\n\n![p](p.png)\n", encoding="utf-8")
    res = server.markdown_to_pdf(input_path=str(src))
    assert res["output"] == str(tmp_path / "notes" / "plan.pdf") and res["images"] == 1
    _png(tmp_path / "root.png")                   # text: pictures are found from the root
    res = server.markdown_to_pdf(markdown="Hi ![r](root.png)",
                                 output_path=str(tmp_path / "hi.pdf"), title="Hello")
    assert res["title"] == "Hello" and res["images"] == 1


@pytest.mark.parametrize(("kwargs", "message"), [
    ({}, "Give either markdown"),
    ({"markdown": "x", "input_path": "y"}, "Give either markdown"),
    ({"markdown": "x"}, "output_path is needed"),
])
def test_the_tool_checks_its_arguments(server, kwargs, message):
    with pytest.raises(server.ToolError, match=message):
        server.markdown_to_pdf(**kwargs)


def test_the_tool_never_replaces_without_overwrite(server, tmp_path, tmp_path_factory):
    out = tmp_path / "a.pdf"
    out.write_bytes(b"keep")
    with pytest.raises(server.ToolError, match="already exists"):
        server.markdown_to_pdf(markdown="x", output_path=str(out))
    assert out.read_bytes() == b"keep"
    assert server.markdown_to_pdf(markdown="x", output_path=str(out), overwrite=True)
    elsewhere = tmp_path_factory.mktemp("elsewhere")
    with pytest.raises(server.ToolError, match="outside the allowed root"):
        server.markdown_to_pdf(markdown="x", output_path=str(elsewhere / "a.pdf"))
    src = tmp_path / "same.md"
    src.write_text("x")
    with pytest.raises(server.ToolError, match="same as the input"):
        server.markdown_to_pdf(input_path=str(src), output_path=str(src))


def test_long_tables_are_laid_out_in_parts_with_the_header(tmp_path, settings, monkeypatch):
    monkeypatch.setattr(M, "MAX_TABLE_ROWS", 10)
    rows = "".join(f"| r{n} |\n" for n in range(25))
    res, out = _make(tmp_path, settings, "| Head |\n|---|\n" + rows)
    text = _text(out)
    assert text.count("Head") == 3 and "r0" in text and "r24" in text
    _, out = _make(tmp_path, settings, "| a |\n|---|\n")   # a header and no rows
    assert "a" in _text(out)


def test_a_layout_that_never_progresses_stops_at_the_time_limit(tmp_path, settings,
                                                                monkeypatch):
    import time

    from reportlab.platypus import SimpleDocTemplate

    def stuck(self, flowables):   # as ReportLab does when something can never fit
        time.sleep(0.2)
    monkeypatch.setattr(M, "BUILD_TIME_LIMIT_S", 0.5)
    monkeypatch.setattr(SimpleDocTemplate, "handle_flowable", stuck)
    with pytest.raises(PdfUserError, match="took over 0.5 s"):
        _make(tmp_path, settings, "x")


def test_entities_choose_the_fonts_too(tmp_path, settings):
    res, out = _make(tmp_path, settings, "&#x4e2d; &#x3b1;")
    assert res["unicode_font"] is True and res["missing_characters"] == ["中"]
    assert "α" in _text(out)


def test_a_blank_title_falls_back_to_the_first_heading(tmp_path, settings):
    res, out = _make(tmp_path, settings, "# Real\n", title="   ", author=" A  b ")
    assert res["title"] == "Real"
    with pikepdf.open(out) as pdf:
        assert str(pdf.docinfo.Title) == "Real" and str(pdf.docinfo.Author) == "A b"
    res, _ = _make(tmp_path, settings, "# " + "t" * 900)
    assert len(res["title"]) == 200


def test_a_nul_in_a_picture_path_is_skipped(tmp_path, settings):
    res, _ = _make(tmp_path, settings, "![x](%00.png) ![y](a%00b.png)")
    assert [s["reason"] for s in res["images_skipped"]] == ["not found", "not found"]


@pytest.mark.skipif(__import__("sys").platform == "win32", reason="symlinks need privileges")
def test_pictures_cant_escape_the_root(tmp_path, settings, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside")
    _png(outside / "secret.png")
    (tmp_path / "link.png").symlink_to(outside / "secret.png")
    text = (f"![a](link.png) ![b](..%2F{outside.name}%2Fsecret.png) "
            f"![c]({outside / 'secret.png'})")
    res, _ = _make(tmp_path, settings, text)
    assert res["images"] == 0 and len(res["images_skipped"]) == 3
    assert all("outside the allowed root" in s["reason"] for s in res["images_skipped"])


def test_pictures_stop_at_the_file_size_limit(tmp_path, settings):
    import dataclasses
    import io
    import os
    noise = Image.frombytes("RGB", (600, 600), os.urandom(600 * 600 * 3))
    noise.save(tmp_path / "n.jpg", quality=90)
    encoded = io.BytesIO()
    noise.save(encoded, "JPEG", quality=90)     # as each copy is embedded
    limit = dataclasses.replace(settings, max_file_bytes=int(encoded.tell() * 2.5))
    res, _ = _make(tmp_path, limit, "![1](n.jpg)\n\n![2](n.jpg)\n\n![3](n.jpg)")
    assert res["images"] < 3
    assert "more than the file-size limit" in res["images_skipped"][-1]["reason"]


def test_a_picture_that_decodes_bigger_than_its_header_counts_its_real_size(
        tmp_path, settings, monkeypatch):
    _png(tmp_path / "p.png", size=(20, 20))
    monkeypatch.setattr(C, "MAX_TOTAL_PIXELS", 1000)
    real = C._open_image

    def swapped(path):      # the file changed between the header check and decoding
        return real(path).resize((100, 100))
    monkeypatch.setattr(C, "_open_image", swapped)
    res, _ = _make(tmp_path, settings, "![p](p.png)")
    assert res["images"] == 0 and "too many pixels" in res["images_skipped"][0]["reason"]
