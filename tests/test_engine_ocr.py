"""OCR: an invisible text layer placed where each word is seen, the page otherwise kept.

Most tests use a stand-in tesseract (a Python script that answers like Tesseract and
writes a text-only PDF with one known word at a known spot), so they run without
Tesseract installed. Tests marked ``real`` run the installed tesseract, when there is one.
"""
from __future__ import annotations

import dataclasses
import io
import json
import shutil
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pikepdf
import pypdfium2 as pdfium
import pytest
from PIL import Image
from reportlab.pdfgen import canvas

from paperboy import engine as E
from paperboy.engine import ocr, ocr_worker
from paperboy.errors import DependencyError, PdfUserError

FAKE = textwrap.dedent('''\
    #!{python}
    """Answers like Tesseract 5. FAKE_TESSERACT_MODE picks a misbehaviour."""
    import json, os, sys, time
    mode = os.environ.get("FAKE_TESSERACT_MODE", "ok")
    args = sys.argv[1:]
    if args == ["--version"]:
        print("tesseract 3.05.02" if mode == "old" else
              "nothing to see" if mode == "notesseract" else "tesseract 5.3.0\\n leptonica")
        sys.exit(0)
    if args == ["--list-langs"]:
        print('List of available languages in "/x/tessdata/" (3):')
        print("deu\\neng\\nosd")
        sys.exit(0)
    log = os.environ.get("FAKE_TESSERACT_LOG")
    if log:
        with open(log, "a") as f:
            f.write(json.dumps({{"args": args, "omp": os.environ.get("OMP_THREAD_LIMIT"),
                                "cwd": os.getcwd()}})
                    + "\\n")
    if mode == "fail":
        print("Error: could not read image", file=sys.stderr)
        sys.exit(1)
    if mode == "hang":
        time.sleep(30)
    png, base = args[0], args[1]
    dpi = float(args[args.index("--dpi") + 1])
    if mode == "nooutput":
        sys.exit(0)
    if mode == "badpdf":
        open(base + ".pdf", "wb").write(b"not a pdf")
        open(base + ".tsv", "w").write(HEADER + WORD.format(conf=90, text="x"))
        sys.exit(0)
    from PIL import Image
    from reportlab.pdfgen import canvas
    w, h = Image.open(png).size
    pw, ph = w / dpi * 72, h / dpi * 72
    c = canvas.Canvas(base + ".pdf", pagesize=(pw, ph))
    word = "W%dx%d" % (w, h)
    if mode != "empty":
        t = c.beginText(pw * 0.1, ph * 0.85)   # near the top-left, as displayed
        t.setTextRenderMode(3)
        t.setFont("Helvetica", 12)
        t.textOut(word)
        c.drawText(t)
    c.showPage()
    c.save()
    with open(base + ".tsv", "w") as f:
        f.write(HEADER)
        if mode != "empty":
            f.write(WORD.format(conf=91.5, text=word))
            f.write(WORD.format(conf=-1, text=""))
''')
FAKE_TAIL = textwrap.dedent('''\
    HEADER = "\\t".join(["level", "page_num", "block_num", "par_num", "line_num",
                         "word_num", "left", "top", "width", "height", "conf", "text"]) + "\\n"
    WORD = "5\\t1\\t1\\t1\\t1\\t1\\t10\\t10\\t50\\t20\\t{conf}\\t{text}\\n"
''')


def fake_tesseract(folder) -> str:
    """Write the stand-in tesseract into *folder*/bin; returns its path."""
    path = folder / "bin" / "tesseract"
    path.parent.mkdir(exist_ok=True)
    head, _, rest = FAKE.format(python=sys.executable).partition("mode = ")
    path.write_text(head + FAKE_TAIL + "mode = " + rest)
    path.chmod(0o755)
    return str(path)


@pytest.fixture
def tesseract(tmp_path, monkeypatch):
    """A stand-in tesseract; returns (its path, the log of its runs)."""
    path = fake_tesseract(tmp_path)
    log = tmp_path / "tesseract.log"
    monkeypatch.setenv("FAKE_TESSERACT_LOG", str(log))
    monkeypatch.delenv("FAKE_TESSERACT_MODE", raising=False)
    ocr._probe_cached.cache_clear()
    return path, log


@pytest.fixture
def ocr_settings(settings, tesseract):
    return dataclasses.replace(settings, tesseract_path=tesseract[0])


def _calls(log):
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def scan(path, pages=1, size=(612, 792), rotate=0, text=None):
    """An image-only PDF, like a scanner makes: each page one picture, no text."""
    c = canvas.Canvas(str(path), pagesize=size)
    for n in range(pages):
        image = Image.new("L", (int(size[0] / 72 * 100), int(size[1] / 72 * 100)), 255)
        if text:
            src = io.BytesIO()
            t = canvas.Canvas(src, pagesize=size)
            t.setFont("Helvetica", 20)
            for k, line in enumerate(text):
                t.drawString(72, size[1] - 120 - 34 * k, line)
            t.showPage()
            t.save()
            image = pdfium.PdfDocument(src.getvalue())[0].render(scale=200 / 72,
                                                                 grayscale=True).to_pil()
        else:
            image.paste(0, (100, 100, 300, 140 + 10 * n))  # a smudge, different per page
        buf = io.BytesIO()
        image.save(buf, "PNG")
        from reportlab.lib.utils import ImageReader
        c.drawImage(ImageReader(io.BytesIO(buf.getvalue())), 0, 0, *size)
        c.showPage()
    c.save()
    if rotate:
        with pikepdf.open(path, allow_overwriting_input=True) as pdf:
            for page in pdf.pages:
                page.Rotate = rotate
            pdf.save(path)
    return path


def typed(path, pages=1):
    """A PDF with real text on every page."""
    c = canvas.Canvas(str(path))
    for n in range(pages):
        c.drawString(72, 720, f"TYPED-{n + 1}")
        c.showPage()
    c.save()
    return path


def text_of(path, index=0, password=None):
    doc = pdfium.PdfDocument(str(path), password=password)
    try:
        return doc[index].get_textpage().get_text_range()
    finally:
        doc.close()


def word_box(path, word, index=0):
    """Where *word* is on the page, in user space: (left, bottom, right, top)."""
    doc = pdfium.PdfDocument(str(path))
    try:
        textpage = doc[index].get_textpage()
        search = textpage.search(word)
        found = search.get_next()
        assert found, f"{word!r} not on page {index + 1}"
        start, count = found
        boxes = [textpage.get_charbox(k) for k in range(start, start + count)]
        return (min(b[0] for b in boxes), min(b[1] for b in boxes),
                max(b[2] for b in boxes), max(b[3] for b in boxes))
    finally:
        doc.close()


# --- what it does ------------------------------------------------------------------------


def test_scan_gains_an_invisible_text_layer(tmp_path, ocr_settings, tesseract):
    src = scan(tmp_path / "scan.pdf")
    out = tmp_path / "out.pdf"
    res = E.ocr_pdf(src, out, ocr_settings)
    assert res["output"] == str(out)
    assert res["language"] == "eng"
    assert res["pages"] == [{"page": 1, "words": 1, "mean_confidence": 91.5, "dpi": 300}]
    assert res["words"] == 1 and res["pages_with_text"] == []
    assert res["password_protected"] is False
    assert "W2550x330" in text_of(out)  # PDFium may round a pixel up
    call, = _calls(tesseract[1])
    assert call["omp"] == "1"
    args = call["args"]
    # Settings only: config-file names ("pdf", "tsv") would be looked for in the
    # current directory too.
    assert args[2:] == ["--dpi", "300", "-l", "eng", "-c", "textonly_pdf=1",
                        "-c", "tessedit_create_pdf=1", "-c", "tessedit_create_tsv=1"]
    assert call["cwd"] == str(Path(args[0]).parent.resolve())


def test_the_text_is_invisible_and_the_page_looks_the_same(tmp_path, ocr_settings):
    src = scan(tmp_path / "scan.pdf")
    out = tmp_path / "out.pdf"
    E.ocr_pdf(src, out, ocr_settings)
    before = pdfium.PdfDocument(str(src))[0].render(scale=0.5).to_pil()
    after = pdfium.PdfDocument(str(out))[0].render(scale=0.5).to_pil()
    assert before.tobytes() == after.tobytes()
    with pikepdf.open(out) as pdf:
        form = next(v for k, v in pdf.pages[0].Resources.XObject.items() if k.startswith("/Ocr"))
        assert b"3 Tr" in form.read_bytes()


def test_original_content_and_pictures_are_kept(tmp_path, ocr_settings):
    src = scan(tmp_path / "scan.pdf")
    out = tmp_path / "out.pdf"
    E.ocr_pdf(src, out, ocr_settings)
    with pikepdf.open(src) as a, pikepdf.open(out) as b:
        old, new = a.pages[0], b.pages[0]
        for key, image in old.Resources.XObject.items():
            assert new.Resources.XObject[key].read_bytes() == image.read_bytes()
        assert old.Contents.read_bytes() in b"".join(s.read_bytes() for s in new.Contents)


def test_jpeg_scans_are_not_recompressed(tmp_path, ocr_settings):
    picture = Image.new("RGB", (850, 1100), "white")
    picture.paste((40, 40, 40), (100, 100, 400, 140))
    src = tmp_path / "scan.pdf"
    picture.save(src, "PDF", resolution=100)  # Pillow stores it as JPEG (DCTDecode)
    out = tmp_path / "out.pdf"
    E.ocr_pdf(src, out, ocr_settings)
    with pikepdf.open(src) as a, pikepdf.open(out) as b:
        (key, image), = a.pages[0].Resources.XObject.items()
        assert image.Filter == pikepdf.Name.DCTDecode
        assert b.pages[0].Resources.XObject[key].read_raw_bytes() == image.read_raw_bytes()


def test_pages_with_text_are_skipped_by_default(tmp_path, ocr_settings, tesseract):
    typed_page = typed(tmp_path / "typed.pdf")
    scanned = scan(tmp_path / "scan.pdf")
    mixed = tmp_path / "mixed.pdf"
    with pikepdf.open(typed_page) as a, pikepdf.open(scanned) as b:
        a.pages.extend(b.pages)
        a.save(mixed)
    out = tmp_path / "out.pdf"
    res = E.ocr_pdf(mixed, out, ocr_settings)
    assert res["pages_with_text"] == [1]
    assert [p["page"] for p in res["pages"]] == [2]
    assert len(_calls(tesseract[1])) == 1
    assert text_of(out, 0) == "TYPED-1"


def test_pages_with_text_can_be_read_too(tmp_path, ocr_settings):
    out = tmp_path / "out.pdf"
    res = E.ocr_pdf(typed(tmp_path / "typed.pdf"), out, ocr_settings,
                    skip_pages_with_text=False)
    assert res["pages_with_text"] == []
    assert res["pages"][0]["words"] == 1
    assert "TYPED-1" in text_of(out) and "W" in text_of(out)


def test_an_earlier_ocr_layer_counts_as_text(tmp_path, ocr_settings, tesseract):
    once = tmp_path / "once.pdf"
    E.ocr_pdf(scan(tmp_path / "scan.pdf"), once, ocr_settings)
    res = E.ocr_pdf(once, tmp_path / "twice.pdf", ocr_settings)
    assert res["pages"] == [] and res["pages_with_text"] == [1]
    assert len(_calls(tesseract[1])) == 1


def test_page_selection(tmp_path, ocr_settings):
    res = E.ocr_pdf(scan(tmp_path / "scan.pdf", pages=4), tmp_path / "out.pdf",
                    ocr_settings, pages="2,4")
    assert [p["page"] for p in res["pages"]] == [2, 4]
    assert text_of(tmp_path / "out.pdf", 0) == ""


def test_bad_page_spec_is_refused(tmp_path, ocr_settings):
    with pytest.raises(PdfUserError):
        E.ocr_pdf(scan(tmp_path / "scan.pdf"), tmp_path / "out.pdf", ocr_settings, pages="9")
    assert not (tmp_path / "out.pdf").exists()


def test_many_pages_come_back_in_order(tmp_path, ocr_settings, monkeypatch):
    """Pages run several at a time; each result must land on its own page."""
    src = tmp_path / "sizes.pdf"
    pdf = pikepdf.new()
    for n in range(9):
        one = scan(tmp_path / f"s{n}.pdf", size=(300 + 20 * n, 400))
        with pikepdf.open(one) as part:
            pdf.pages.extend(part.pages)
    pdf.save(src)
    out = tmp_path / "out.pdf"
    res = E.ocr_pdf(src, out, ocr_settings)
    assert [p["page"] for p in res["pages"]] == list(range(1, 10))
    for n in range(9):
        width = (300 + 20 * n) / 72 * 300
        assert abs(int(text_of(out, n).strip()[1:].split("x")[0]) - width) <= 1


def test_a_page_without_words_gets_no_layer(tmp_path, ocr_settings, monkeypatch):
    monkeypatch.setenv("FAKE_TESSERACT_MODE", "empty")
    src = scan(tmp_path / "scan.pdf")
    out = tmp_path / "out.pdf"
    res = E.ocr_pdf(src, out, ocr_settings)
    assert res["pages"] == [{"page": 1, "words": 0, "mean_confidence": None, "dpi": 300}]
    assert res["words"] == 0
    with pikepdf.open(out) as pdf:
        assert not any(k.startswith("/Ocr") for k in pdf.pages[0].Resources.XObject.keys())


def test_tesseracts_font_is_stored_once(tmp_path, ocr_settings):
    out = tmp_path / "out.pdf"
    E.ocr_pdf(scan(tmp_path / "scan.pdf", pages=3), out, ocr_settings)
    with pikepdf.open(out) as pdf:
        fonts = {font.objgen for page in pdf.pages
                 for key, form in page.Resources.XObject.items() if key.startswith("/Ocr")
                 for font in (form.Resources.Font[k] for k in form.Resources.Font.keys())}
    assert len(fonts) == 1


def test_fonts_that_differ_are_not_shared():
    pdf = pikepdf.new()

    def form(base):
        font = pdf.make_indirect(pikepdf.Dictionary(Type=pikepdf.Name.Font,
                                                    BaseFont=pikepdf.Name(base)))
        return pikepdf.Stream(pdf, b"", Resources=pikepdf.Dictionary(
            Font=pikepdf.Dictionary(F1=font)))

    fonts: dict = {}
    a, b, c = form("/A"), form("/B"), form("/A")
    for f in (a, b, c):
        ocr._share_fonts(f, fonts)
    assert a.Resources.Font.F1.objgen == c.Resources.Font.F1.objgen
    assert a.Resources.Font.F1.objgen != b.Resources.Font.F1.objgen
    ocr._share_fonts(pikepdf.Stream(pdf, b""), fonts)  # no resources: nothing to do


def test_fingerprint_of_deep_objects_is_refused():
    deep = pikepdf.Array([])
    for _ in range(ocr._FINGERPRINT_DEPTH + 2):
        deep = pikepdf.Array([deep])
    with pytest.raises(ValueError):
        ocr._fingerprint(deep)
    pdf = pikepdf.new()
    form = pikepdf.Stream(pdf, b"", Resources=pikepdf.Dictionary(
        Font=pikepdf.Dictionary(F1=deep)))
    ocr._share_fonts(form, {})  # left as it is


# --- placement ---------------------------------------------------------------------------


@pytest.mark.parametrize("rotate, corner", [
    (0, "top-left"), (90, "bottom-left"), (180, "bottom-right"), (270, "top-right")])
def test_text_is_placed_where_it_is_seen_on_rotated_pages(tmp_path, ocr_settings, rotate,
                                                         corner):
    """The stand-in puts its word near the top-left as displayed; in the page's own
    (unrotated) coordinates that corner moves with /Rotate."""
    out = tmp_path / "out.pdf"
    E.ocr_pdf(scan(tmp_path / "scan.pdf", rotate=rotate), out, ocr_settings)
    left, bottom, right, top = word_box(out, "W")
    x, y = (left + right) / 2, (bottom + top) / 2
    vertical, horizontal = corner.split("-")
    assert (y > 792 * 0.6) if vertical == "top" else (y < 792 * 0.4)
    assert (x < 612 * 0.4) if horizontal == "left" else (x > 612 * 0.6)


def test_text_is_placed_in_the_crop_box(tmp_path, ocr_settings):
    src = scan(tmp_path / "scan.pdf")
    with pikepdf.open(src, allow_overwriting_input=True) as pdf:
        pdf.pages[0].CropBox = [200, 100, 500, 700]
        pdf.save(src)
    out = tmp_path / "out.pdf"
    res = E.ocr_pdf(src, out, ocr_settings)
    assert "W1250x250" in text_of(out)  # 300 x 600 pt read at 300 dpi
    assert res["pages"][0]["dpi"] == 300
    left, bottom, _, top = word_box(out, "W")
    # 10% in from the crop box's left, 15% down from its top
    assert left == pytest.approx(200 + 300 * 0.1, abs=1)
    assert bottom == pytest.approx(700 - 600 * 0.15, abs=4)


def test_content_that_leaves_its_graphics_state_changed_cannot_move_the_text(
        tmp_path, ocr_settings):
    src = scan(tmp_path / "scan.pdf")
    with pikepdf.open(src, allow_overwriting_input=True) as pdf:
        page = pdf.pages[0]
        page.contents_add(pikepdf.Stream(pdf, b"Q Q q 0.5 0 0 0.5 300 300 cm BT"))
        pdf.save(src)
    plain = tmp_path / "plain.pdf"
    E.ocr_pdf(scan(tmp_path / "scan2.pdf"), plain, ocr_settings)
    out = tmp_path / "out.pdf"
    E.ocr_pdf(src, out, ocr_settings)
    assert word_box(out, "W") == pytest.approx(word_box(plain, "W"), abs=0.01)


def test_pages_sharing_resources_keep_their_own(tmp_path, ocr_settings):
    src = scan(tmp_path / "scan.pdf", pages=2)
    with pikepdf.open(src, allow_overwriting_input=True) as pdf:
        pdf.pages[1].Resources = pdf.pages[0].Resources
        pdf.save(src)
    out = tmp_path / "out.pdf"
    E.ocr_pdf(src, out, ocr_settings, pages="1")
    with pikepdf.open(out) as pdf:
        assert any(k.startswith("/Ocr") for k in pdf.pages[0].Resources.XObject.keys())
        assert not any(k.startswith("/Ocr") for k in pdf.pages[1].Resources.XObject.keys())


def test_large_pages_are_read_at_fewer_dpi(tmp_path, ocr_settings, tesseract):
    out = tmp_path / "out.pdf"
    res = E.ocr_pdf(scan(tmp_path / "big.pdf", size=(2400, 2400)), out, ocr_settings,
                    dpi=600)
    page, = res["pages"]
    assert page["dpi"] < 600
    w, h = (int(v) for v in text_of(out).strip()[1:].split("x"))
    assert w * h <= ocr.OCR_MAX_PIXELS * 1.01
    call, = _calls(tesseract[1])
    assert call["args"][3] == str(page["dpi"])


def _record(**changes):
    return dict({"index": 0, "width": 612.0, "height": 792.0, "rotation": 0}, **changes)


@pytest.mark.parametrize("record", [
    _record(width=100.0), _record(height=0.0), _record(rotation=90)])
def test_pages_pdfium_reads_differently_are_refused(tmp_path, record):
    with pikepdf.open(scan(tmp_path / "scan.pdf")) as pdf:
        with pytest.raises(PdfUserError, match="page boxes or rotation are malformed"):
            ocr._check(pdf, record, [10**9, 10**9])
        ocr._check(pdf, _record(width=612.4), [10**9, 10**9])  # within tolerance


def test_a_rotation_pdfium_reads_differently_is_refused(tmp_path, ocr_settings):
    """/Rotate -45: Paperboy's helpers round it to 270, PDFium to 0; on a square page the
    sizes agree, so only the rotation tells the text would land elsewhere."""
    src = scan(tmp_path / "scan.pdf", size=(600, 600))
    with pikepdf.open(src, allow_overwriting_input=True) as pdf:
        pdf.pages[0].Rotate = -45
        pdf.save(src)
    with pytest.raises(PdfUserError, match="rotation are malformed"):
        E.ocr_pdf(src, tmp_path / "out.pdf", ocr_settings)


def _content(pdf, data, filters=None):
    stream = pikepdf.Stream(pdf, data)
    if filters:
        stream.Filter = filters
    return stream


def test_pages_with_too_much_content_are_refused_before_reading(tmp_path, ocr_settings,
                                                                tesseract, monkeypatch):
    src = scan(tmp_path / "scan.pdf")
    with pikepdf.open(src, allow_overwriting_input=True) as pdf:
        pdf.pages[0].contents_add(pikepdf.Stream(pdf, b"0 0 m\n" * 1000))
        pdf.save(src)
    monkeypatch.setattr(ocr, "MAX_STREAM_TOKENS", 1000)
    with pytest.raises(PdfUserError, match="Page 1 has too much content"):
        E.ocr_pdf(src, tmp_path / "out.pdf", ocr_settings)
    assert _calls(tesseract[1]) == []  # refused before Tesseract ran


def test_content_measurement(tmp_path):
    pdf = pikepdf.new()
    pdf.add_blank_page()
    page = pdf.pages[0]
    page.obj.Contents = _content(pdf, b"q 1 0 0 1 0 0 cm Q")
    budget = [1000, 1000]
    ocr._measure(page, 1, budget)
    assert budget[0] == 1000 - 18 and budget[1] < 1000
    for bad in (_content(pdf, b"x", pikepdf.Name.LZWDecode),           # can't be measured
                pikepdf.Array([_content(pdf, b"q"), pikepdf.Dictionary()])):  # not a stream
        page.obj.Contents = bad
        with pytest.raises(PdfUserError, match="can't be measured"):
            ocr._measure(page, 1, [1000, 1000])
    page.obj.Contents = _content(pdf, b"q Q")
    with pytest.raises(PdfUserError, match="too much content"):
        ocr._measure(page, 1, [2, 1000])  # over what the call has left
    del page.obj.Contents
    ocr._measure(page, 1, [0, 0])  # no content at all


# --- arguments ---------------------------------------------------------------------------


@pytest.mark.parametrize("language", [
    "", "eng;rm -rf", "../eng", "eng+", "+eng", "-c", "eng+-psm", "a/../b",
    "x" * 65, "eng+deu+eng+deu+eng+deu"])
def test_bad_language_codes_are_refused(tmp_path, ocr_settings, language, tesseract):
    with pytest.raises(PdfUserError, match="language must be"):
        E.ocr_pdf(scan(tmp_path / "scan.pdf"), tmp_path / "out.pdf", ocr_settings,
                  language=language)
    assert _calls(tesseract[1]) == []


def test_languages_that_are_not_installed_are_refused(tmp_path, ocr_settings):
    with pytest.raises(PdfUserError, match=r"no language data for fra \(installed: deu, eng\)"):
        E.ocr_pdf(scan(tmp_path / "scan.pdf"), tmp_path / "out.pdf", ocr_settings,
                  language="eng+fra")


def test_several_languages(tmp_path, ocr_settings, tesseract):
    res = E.ocr_pdf(scan(tmp_path / "scan.pdf"), tmp_path / "out.pdf", ocr_settings,
                    language="deu+eng")
    assert res["language"] == "deu+eng"
    assert _calls(tesseract[1])[0]["args"][5] == "deu+eng"


@pytest.mark.parametrize("dpi", [149, 601, 300.0, True, "300"])
def test_dpi_is_checked(tmp_path, ocr_settings, dpi):
    with pytest.raises(PdfUserError, match="dpi must be"):
        E.ocr_pdf(scan(tmp_path / "scan.pdf"), tmp_path / "out.pdf", ocr_settings, dpi=dpi)


def test_dpi_is_passed_on(tmp_path, ocr_settings, tesseract):
    res = E.ocr_pdf(scan(tmp_path / "scan.pdf"), tmp_path / "out.pdf", ocr_settings,
                    dpi=150)
    assert res["pages"][0]["dpi"] == 150
    assert _calls(tesseract[1])[0]["args"][3] == "150"


# --- Tesseract missing or misbehaving ----------------------------------------------------


def test_without_tesseract(tmp_path, settings):
    with pytest.raises(DependencyError, match="PAPERBOY_TESSERACT"):
        E.ocr_pdf(scan(tmp_path / "scan.pdf"), tmp_path / "out.pdf",
                  dataclasses.replace(settings, tesseract_path=None))


def test_tesseract_that_cannot_run(tmp_path, settings):
    missing = dataclasses.replace(settings, tesseract_path=str(tmp_path / "nope"))
    with pytest.raises(DependencyError, match="Could not run Tesseract"):
        E.ocr_pdf(scan(tmp_path / "scan.pdf"), tmp_path / "out.pdf", missing)


@pytest.mark.parametrize("mode, message", [
    ("old", "needs Tesseract 4.1 or later"), ("notesseract", "doesn't look like Tesseract")])
def test_wrong_program(tmp_path, ocr_settings, monkeypatch, mode, message):
    monkeypatch.setenv("FAKE_TESSERACT_MODE", mode)
    with pytest.raises(DependencyError, match=message):
        E.ocr_pdf(scan(tmp_path / "scan.pdf"), tmp_path / "out.pdf", ocr_settings)


def test_tesseract_is_asked_once_for_its_version_and_languages(tmp_path, ocr_settings,
                                                               tesseract, monkeypatch):
    runs = []
    real = ocr.subprocess.run
    monkeypatch.setattr(ocr.subprocess, "run", lambda cmd, **kw: runs.append(cmd) or
                        real(cmd, **kw))
    for n in range(2):
        E.ocr_pdf(scan(tmp_path / f"s{n}.pdf"), tmp_path / f"o{n}.pdf", ocr_settings)
    assert [cmd[1:] for cmd in runs] == [["--version"], ["--list-langs"]]


def test_probe_that_hangs(tmp_path, fake_binary, settings, monkeypatch):
    def slow(*args, **kwargs):
        raise ocr.subprocess.TimeoutExpired("tesseract", 30)

    monkeypatch.setattr(ocr.subprocess, "run", slow)
    ocr._probe_cached.cache_clear()
    with pytest.raises(DependencyError, match="Could not run Tesseract"):
        ocr._probe(fake_binary("tesseract", "exit 0\n"))


def test_failure_is_reported_and_nothing_written(tmp_path, ocr_settings, monkeypatch):
    monkeypatch.setenv("FAKE_TESSERACT_MODE", "fail")
    out = tmp_path / "out.pdf"
    with pytest.raises(PdfUserError, match="failed on page 1: Error: could not read image"):
        E.ocr_pdf(scan(tmp_path / "scan.pdf"), out, ocr_settings)
    assert not out.exists()


def test_failure_without_a_message(tmp_path, settings, fake_binary, monkeypatch):
    ocr._probe_cached.cache_clear()
    exe = fake_binary("tesseract", textwrap.dedent('''\
        case "$1" in
          --version) echo "tesseract 5.0.0"; exit 0;;
          --list-langs) printf 'List\\neng\\n'; exit 0;;
        esac
        exit 3
    '''))
    with pytest.raises(PdfUserError, match="failed on page 1: 3"):
        E.ocr_pdf(scan(tmp_path / "scan.pdf"), tmp_path / "out.pdf",
                  dataclasses.replace(settings, tesseract_path=exe))


def test_no_output(tmp_path, ocr_settings, monkeypatch):
    monkeypatch.setenv("FAKE_TESSERACT_MODE", "nooutput")
    with pytest.raises(PdfUserError, match="wrote no output for page 1"):
        E.ocr_pdf(scan(tmp_path / "scan.pdf"), tmp_path / "out.pdf", ocr_settings)


def test_unreadable_output(tmp_path, ocr_settings, monkeypatch):
    monkeypatch.setenv("FAKE_TESSERACT_MODE", "badpdf")
    with pytest.raises(PdfUserError, match="Could not read Tesseract's output for page 1"):
        E.ocr_pdf(scan(tmp_path / "scan.pdf"), tmp_path / "out.pdf", ocr_settings)


def test_oversized_output(tmp_path, ocr_settings, monkeypatch):
    monkeypatch.setattr(ocr, "MAX_OUTPUT_BYTES", 10)
    with pytest.raises(PdfUserError, match="output for page 1 is too large"):
        E.ocr_pdf(scan(tmp_path / "scan.pdf"), tmp_path / "out.pdf", ocr_settings)


def test_a_slow_page_is_stopped(tmp_path, ocr_settings, monkeypatch):
    monkeypatch.setenv("FAKE_TESSERACT_MODE", "hang")
    monkeypatch.setattr(ocr, "PAGE_TIME_LIMIT_S", 1)
    with pytest.raises(PdfUserError, match="took over 1 s on page 1"):
        E.ocr_pdf(scan(tmp_path / "scan.pdf"), tmp_path / "out.pdf", ocr_settings)


def test_the_call_time_limit_stops_every_page(tmp_path, ocr_settings, monkeypatch):
    import time

    monkeypatch.setenv("FAKE_TESSERACT_MODE", "hang")
    monkeypatch.setattr(ocr, "OCR_TIME_LIMIT_S", 1.5)
    out = tmp_path / "out.pdf"
    started = time.monotonic()
    with pytest.raises(PdfUserError, match="OCR took over 1.5 s.*Nothing was written"):
        E.ocr_pdf(scan(tmp_path / "scan.pdf", pages=12), out, ocr_settings)
    assert time.monotonic() - started < 10  # running pages were killed, not waited for
    assert not out.exists()


def test_the_time_limit_is_checked_before_each_page(tmp_path, ocr_settings, monkeypatch):
    monkeypatch.setattr(ocr, "OCR_TIME_LIMIT_S", 0)
    with pytest.raises(PdfUserError, match="OCR took over 0 s"):
        E.ocr_pdf(scan(tmp_path / "scan.pdf"), tmp_path / "out.pdf", ocr_settings)


def _bomb(path, depth):
    """One page whose forms draw each other ten times over, *depth* levels deep: a few
    KB that keep PDFium busy for a long time."""
    pdf = pikepdf.new()
    pdf.add_blank_page(page_size=(200, 200))
    inner = pdf.make_stream(b"0 0 1 1 re f")
    inner.BBox, inner.Subtype = [0, 0, 1, 1], pikepdf.Name.Form
    for _ in range(depth):
        outer = pdf.make_stream(b"/F Do " * 10)
        outer.BBox, outer.Subtype = [0, 0, 200, 200], pikepdf.Name.Form
        outer.Resources = pikepdf.Dictionary(XObject=pikepdf.Dictionary(F=inner))
        inner = outer
    page = pdf.pages[0]
    page.obj.Resources = pikepdf.Dictionary(XObject=pikepdf.Dictionary(F=inner))
    page.obj.Contents = pdf.make_stream(b"/F Do")
    for _ in range(3):
        pdf.pages.append(pdf.pages[0])
    pdf.save(path)
    return path


def test_pages_slow_to_render_are_stopped_at_the_deadline(tmp_path, ocr_settings,
                                                         tesseract, monkeypatch):
    """Rendering runs in a child process, killed at the deadline; the server's PDFium
    lock is never taken, so other tools keep working meanwhile."""
    from paperboy.engine.render import _PDFIUM_LOCK

    monkeypatch.setattr(ocr, "OCR_TIME_LIMIT_S", 2)
    src = _bomb(tmp_path / "bomb.pdf", 7)
    outcome = []
    worker = threading.Thread(target=lambda: outcome.append(pytest.raises(
        PdfUserError, E.ocr_pdf, src, tmp_path / "out.pdf", ocr_settings)))
    started = time.monotonic()
    worker.start()
    time.sleep(0.5)
    assert _PDFIUM_LOCK.acquire(timeout=1)
    _PDFIUM_LOCK.release()
    worker.join(30)
    assert time.monotonic() - started < 10
    assert "OCR took over 2 s" in str(outcome[0].value)
    assert _calls(tesseract[1]) == []


@pytest.mark.parametrize("body, message", [
    ("echo 'No module named paperboy' >&2\nexit 1\n",
     "Could not start the page renderer: No module named paperboy"),
    ("exit 1\n", "Could not start the page renderer$"),
    ("echo '{\"ready\": true}'\nexit 1\n", "the renderer stopped"),
])
def test_a_renderer_that_stops_early(tmp_path, ocr_settings, monkeypatch, fake_binary,
                                     body, message):
    monkeypatch.setattr(ocr.sys, "executable", fake_binary("python", body))
    with pytest.raises(PdfUserError, match=message):
        E.ocr_pdf(scan(tmp_path / "scan.pdf"), tmp_path / "out.pdf", ocr_settings)


@pytest.mark.parametrize("line", [
    "not json", '{"index": 0, "wid', "[1, 2]", '{"index": 0}', '{"index": 1, "text": true}',
    '{"index": true, "text": true}', '{"index": -1, "text": true}',
    '{"index": 0, "text": true, "png": "x"}', '{"error": 5}',
    '{"index": 0, "width": 612, "height": 792, "rotation": 45, "png": "P", "scale": 1}',
    '{"index": 0, "width": NaN, "height": 792, "rotation": 0, "png": "P", "scale": 1}',
    '{"index": 0, "width": 612, "height": 792, "rotation": 0, "png": "P", "scale": 0}',
    '{"index": 0, "width": 612, "height": 792, "rotation": 0, "png": "P", "scale": true}',
    '{"index": 0, "width": 612, "height": 792, "rotation": 0, "png": "VICTIM", "scale": 1}',
])
def test_nothing_the_renderer_says_can_touch_other_files(tmp_path, ocr_settings, monkeypatch,
                                                        fake_binary, tesseract, line):
    """The worker reads hostile PDFs; whatever it prints, the server only accepts the
    record it asked for, so it never reads, OCRs or deletes a file the worker names."""
    victim = tmp_path / "keep.png"
    Image.new("L", (10, 10)).save(victim)
    (tmp_path / "keep.pdf").write_bytes(b"mine")
    made = []
    real = ocr.tempfile.mkdtemp
    monkeypatch.setattr(ocr.tempfile, "mkdtemp",
                        lambda **kw: made.append(real(dir=tmp_path, **kw)) or made[-1])
    line = line.replace("VICTIM", str(victim)).replace("P", "%s")
    script = tmp_path / "say.txt"
    monkeypatch.setattr(ocr.sys, "executable", fake_binary("python", f"""
        work=$(ls -d {tmp_path}/paperboy-ocr-*)
        echo '{{"ready": true}}'
        printf '%s\\n' "$(sed "s#%s#$work/p0.png#" {script})"
        sleep 5
    """))
    script.write_text(line)
    with pytest.raises(PdfUserError, match="returned something unexpected|^5$"):
        E.ocr_pdf(scan(tmp_path / "scan.pdf"), tmp_path / "out.pdf", ocr_settings)
    assert victim.exists() and (tmp_path / "keep.pdf").read_bytes() == b"mine"
    assert _calls(tesseract[1]) == []


def test_an_error_from_the_renderer_is_shown(tmp_path, ocr_settings, monkeypatch,
                                            fake_binary):
    monkeypatch.setattr(ocr.sys, "executable", fake_binary(
        "python", 'echo \'{"ready": true}\'\necho \'{"error": "Could not render page 1: x"}\'\n'))
    with pytest.raises(PdfUserError, match="^Could not render page 1: x$"):
        E.ocr_pdf(scan(tmp_path / "scan.pdf"), tmp_path / "out.pdf", ocr_settings)


def test_a_renderer_that_stops_after_the_deadline(tmp_path, ocr_settings, monkeypatch,
                                                 fake_binary):
    monkeypatch.setattr(ocr.sys, "executable", fake_binary(
        "python", "echo '{\"ready\": true}'\nsleep 1\n"))
    monkeypatch.setattr(ocr, "OCR_TIME_LIMIT_S", 0.5)
    real_line = ocr._Pages._line
    monkeypatch.setattr(ocr._Pages, "_line",  # the end of output arrives after the deadline
                        lambda self, deadline: real_line(self, deadline + 5))
    with pytest.raises(PdfUserError, match="OCR took over 0.5 s"):
        E.ocr_pdf(scan(tmp_path / "scan.pdf"), tmp_path / "out.pdf", ocr_settings)


def test_a_pdf_pdfium_cannot_open(tmp_path, ocr_settings, monkeypatch):
    monkeypatch.setattr(ocr, "_plain", lambda pdf: b"not a pdf")
    with pytest.raises(PdfUserError, match="Could not open PDF"):
        E.ocr_pdf(scan(tmp_path / "scan.pdf"), tmp_path / "out.pdf", ocr_settings)


def _worker(request, data, ready=lambda: True):
    out = io.StringIO()
    real = ocr_worker.sys.stdout
    ocr_worker.sys.stdout = out
    try:
        ocr_worker.run(dict({"pages": [0], "dpi": 72, "skip": True, "max_pixels": 10**6,
                             "work": "."}, **request), data, ready)
    finally:
        ocr_worker.sys.stdout = real
    ready, *records = [json.loads(line) for line in out.getvalue().splitlines()]
    assert ready == {"ready": True}
    return records


def test_worker_renders_skips_and_reports(tmp_path):
    scanned = scan(tmp_path / "scan.pdf").read_bytes()
    typed_page = typed(tmp_path / "typed.pdf").read_bytes()
    record, = _worker({"work": str(tmp_path)}, scanned)
    assert record["index"] == 0 and record["rotation"] == 0
    assert (record["width"], record["height"]) == (612, 792) and record["scale"] == 1
    assert Image.open(record["png"]).size == (612, 792)
    assert _worker({}, typed_page) == [{"index": 0, "text": True}]
    record, = _worker({"skip": False, "work": str(tmp_path), "max_pixels": 1000},
                      typed_page)
    assert record["scale"] < 1  # fewer pixels than asked for
    assert _worker({"work": str(tmp_path)}, scanned, ready=lambda: False) == []


@pytest.mark.parametrize("error", [pdfium.PdfiumError, ValueError])
def test_worker_reports_what_pdfium_cannot_do(tmp_path, monkeypatch, error):
    assert _worker({}, b"not a pdf")[0]["error"].startswith("Could not open PDF")

    def broken(self, index):
        raise error("broken page")

    monkeypatch.setattr(pdfium.PdfDocument, "__getitem__", broken)
    assert _worker({"pages": [2]}, typed(tmp_path / "t.pdf").read_bytes()) == [
        {"error": "Could not render page 3: broken page"}]


def test_worker_main_reads_its_request_and_the_pdf(tmp_path, monkeypatch):
    data = scan(tmp_path / "scan.pdf").read_bytes()
    request = {"pages": [0], "dpi": 72, "skip": True, "max_pixels": 10**6,
               "work": str(tmp_path), "size": len(data), "cpu_seconds": 60,
               "memory_bytes": 2**32}
    proc = subprocess.run([sys.executable, "-I", "-m", "paperboy.engine.ocr_worker"],
                          input=json.dumps(request).encode() + b"\n" + data + b"\n",
                          capture_output=True, timeout=60)
    assert json.loads(proc.stdout.splitlines()[-1])["png"] == str(tmp_path / "p0.png")
    limits = []
    monkeypatch.setattr(ocr_worker, "_limit", lambda *a: limits.append(a))
    monkeypatch.setattr(ocr_worker, "run", lambda req, got, ready: limits.append(
        (got == data, ready())))
    monkeypatch.setattr(ocr_worker.sys, "stdin", io.TextIOWrapper(io.BytesIO(
        json.dumps(request).encode() + b"\n" + data + b"\n")))
    ocr_worker.main()
    assert limits == [(60, 2**32), (True, True)]


def test_temporary_files_are_removed(tmp_path, ocr_settings, monkeypatch):
    made = []
    real = ocr.tempfile.mkdtemp
    monkeypatch.setattr(ocr.tempfile, "mkdtemp",
                        lambda **kw: made.append(real(dir=tmp_path, **kw)) or made[-1])
    E.ocr_pdf(scan(tmp_path / "scan.pdf", pages=3), tmp_path / "out.pdf", ocr_settings)
    monkeypatch.setenv("FAKE_TESSERACT_MODE", "fail")
    with pytest.raises(PdfUserError):
        E.ocr_pdf(scan(tmp_path / "scan2.pdf"), tmp_path / "out2.pdf", ocr_settings)
    assert len(made) == 2
    assert not any(shutil.os.path.exists(d) for d in made)


def test_words_from_tsv():
    head = "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\t"\
           "conf\ttext\n"
    rows = ["5\t1\t1\t1\t1\t1\t0\t0\t1\t1\t80\tgood",
            "5\t1\t1\t1\t1\t2\t0\t0\t1\t1\t90.5\tbetter",
            "5\t1\t1\t1\t1\t3\t0\t0\t1\t1\t-1\tunscored",
            "5\t1\t1\t1\t1\t4\t0\t0\t1\t1\t95\t  ",
            "5\t1\t1\t1\t1\t5\t0\t0\t1\t1\tbad\tword",
            "4\t1\t1\t1\t1\t0\t0\t0\t1\t1\t-1\t",
            "short\trow"]
    assert ocr._words(head + "\n".join(rows)) == (2, 85.2)
    assert ocr._words(head) == (0, None)
    assert ocr._words("") == (0, None)


# --- protection and signatures -----------------------------------------------------------


def test_password_protected_input_stays_protected(tmp_path, ocr_settings):
    src = scan(tmp_path / "scan.pdf")
    locked = tmp_path / "locked.pdf"
    with pikepdf.open(src) as pdf:
        pdf.save(locked, encryption=pikepdf.Encryption(user="pw", owner="own", R=6))
    out = tmp_path / "out.pdf"
    with pytest.raises(PdfUserError, match="password protected"):
        E.ocr_pdf(locked, out, ocr_settings)
    res = E.ocr_pdf(locked, out, ocr_settings, password="pw")
    assert res["password_protected"] is True and res["words"] == 1
    assert "W" in text_of(out, password="pw")
    with pytest.raises(pikepdf.PasswordError):
        pikepdf.open(out)


def test_a_pdf_that_forbids_changes_needs_its_owner_password(tmp_path, ocr_settings):
    src = scan(tmp_path / "scan.pdf")
    locked = tmp_path / "locked.pdf"
    with pikepdf.open(src) as pdf:
        pdf.save(locked, encryption=pikepdf.Encryption(
            user="", owner="own", R=6, allow=pikepdf.Permissions(modify_other=False)))
    with pytest.raises(PdfUserError, match="owner password"):
        E.ocr_pdf(locked, tmp_path / "out.pdf", ocr_settings)
    res = E.ocr_pdf(locked, tmp_path / "out.pdf", ocr_settings, password="own")
    assert res["words"] == 1
    with pikepdf.open(tmp_path / "out.pdf", password="own") as pdf:
        assert pdf.is_encrypted and not pdf.allow.modify_other


def test_signed_pdfs_are_refused(tmp_path, ocr_settings):
    src = scan(tmp_path / "scan.pdf")
    with pikepdf.open(src, allow_overwriting_input=True) as pdf:
        pdf.make_indirect(pikepdf.Dictionary(Type=pikepdf.Name.Sig,
                                             ByteRange=pikepdf.Array([0, 1, 2, 3])))
        pdf.Root.Perms = pikepdf.Dictionary(DocMDP=pdf.make_indirect(pikepdf.Dictionary()))
        pdf.save(src)
    with pytest.raises(PdfUserError, match="digitally signed"):
        E.ocr_pdf(src, tmp_path / "out.pdf", ocr_settings)


def test_reader_usage_rights_are_dropped(tmp_path, ocr_settings, monkeypatch):
    """The file is rewritten even when no words are found, which breaks them anyway."""
    src = scan(tmp_path / "scan.pdf")
    with pikepdf.open(src, allow_overwriting_input=True) as pdf:
        pdf.Root.Perms = pikepdf.Dictionary(UR3=pdf.make_indirect(pikepdf.Dictionary(
            Type=pikepdf.Name.Sig, ByteRange=[0, 1, 2, 3])))
        pdf.save(src)
    E.ocr_pdf(src, tmp_path / "out.pdf", ocr_settings)
    monkeypatch.setenv("FAKE_TESSERACT_MODE", "empty")
    E.ocr_pdf(src, tmp_path / "same.pdf", ocr_settings)
    for name in ("out.pdf", "same.pdf"):
        with pikepdf.open(tmp_path / name) as pdf:
            assert "/Perms" not in pdf.Root


def test_a_pdf_that_forbids_copying_needs_its_owner_password(tmp_path, ocr_settings):
    """The text layer would make the words in its pictures copyable."""
    locked = tmp_path / "locked.pdf"
    with pikepdf.open(scan(tmp_path / "scan.pdf")) as pdf:
        pdf.save(locked, encryption=pikepdf.Encryption(
            user="", owner="own", R=6, allow=pikepdf.Permissions(extract=False)))
    with pytest.raises(PdfUserError, match="does not allow copying"):
        E.ocr_pdf(locked, tmp_path / "out.pdf", ocr_settings)
    assert E.ocr_pdf(locked, tmp_path / "out.pdf", ocr_settings, password="own")["words"]


def test_tesseract_runs_are_limited_across_calls(tmp_path, tesseract):
    """Parallel tool calls share WORKERS Tesseract slots; a page that can't get one
    before the deadline stops."""
    for _ in range(ocr.WORKERS):
        ocr._SLOTS.acquire()
    try:
        run = ocr._Run(tesseract[0], "eng", time.monotonic() + 0.3)
        with pytest.raises(ocr._OutOfTime):
            run.page(1, str(tmp_path / "p0.png"), 300)
    finally:
        for _ in range(ocr.WORKERS):
            ocr._SLOTS.release()
    assert _calls(tesseract[1]) == []


def test_a_wrapper_scripts_tesseract_is_killed_too(tmp_path, fake_binary, monkeypatch):
    """Killing only a wrapper would leave its child holding the output pipes open."""
    exe = fake_binary("tesseract-wrapper", "sleep 30\necho done\n")
    monkeypatch.setattr(ocr, "PAGE_TIME_LIMIT_S", 1)
    run = ocr._Run(exe, "eng", time.monotonic() + 60)
    started = time.monotonic()
    with pytest.raises(PdfUserError, match="took over 1 s"):
        run.page(1, str(tmp_path / "p0.png"), 300)
    assert time.monotonic() - started < 5


def test_osd_is_not_a_language(tmp_path, ocr_settings):
    with pytest.raises(PdfUserError, match=r"no language data for osd \(installed: deu, eng\)"):
        E.ocr_pdf(scan(tmp_path / "scan.pdf"), tmp_path / "out.pdf", ocr_settings,
                  language="osd")


def test_pages_are_reported_in_page_order(tmp_path, ocr_settings):
    res = E.ocr_pdf(scan(tmp_path / "scan.pdf", pages=3), tmp_path / "out.pdf",
                    ocr_settings, pages="3,1")
    assert [p["page"] for p in res["pages"]] == [1, 3]


def test_xmp_of_the_input_is_left_alone(tmp_path, ocr_settings):
    src = scan(tmp_path / "scan.pdf")
    with pikepdf.open(src, allow_overwriting_input=True) as pdf:
        with pdf.open_metadata(set_pikepdf_as_editor=False) as meta:
            meta["dc:title"] = "Scan"
        pdf.save(src, fix_metadata_version=False)
    with pikepdf.open(src) as pdf:
        before = pdf.Root.Metadata.read_bytes()
        assert ocr._plain(pdf)
        assert pdf.Root.Metadata.read_bytes() == before


# --- the real Tesseract ------------------------------------------------------------------


REAL = shutil.which("tesseract")
real = pytest.mark.skipif(REAL is None, reason="tesseract is not installed")
LINES = ["Invoice 2041 for Acme Corporation", "Total due: 1,250.00 by 12 November"]


@pytest.fixture
def real_settings(settings):
    ocr._probe_cached.cache_clear()
    return dataclasses.replace(settings, tesseract_path=REAL)


@real
def test_real_tesseract_reads_a_scan(tmp_path, real_settings):
    out = tmp_path / "out.pdf"
    res = E.ocr_pdf(scan(tmp_path / "scan.pdf", text=LINES), out, real_settings)
    assert res["pages"][0]["words"] >= 10
    assert res["pages"][0]["mean_confidence"] > 80
    text = text_of(out)
    assert "Acme Corporation" in text and "12 November" in text


@real
@pytest.mark.parametrize("rotate", [0, 90, 180, 270])
def test_real_tesseract_text_lies_over_the_printed_words(tmp_path, real_settings, rotate):
    """Pages with printed text, OCR'd again: the hidden words land on the printed ones."""
    path = tmp_path / "typed.pdf"
    c = canvas.Canvas(str(path), pagesize=(612, 792))
    c.setFont("Helvetica", 18)
    c.saveState()
    # Draw so the line reads upright once the page is turned by /Rotate.
    turn = {90: (612, 0, 90), 180: (612, 792, 180), 270: (0, 792, -90)}.get(rotate)
    if turn:
        c.translate(turn[0], turn[1])
        c.rotate(turn[2])
    c.drawString(150, 300, "Quarterly revenue grew strongly")
    c.restoreState()
    c.showPage()
    c.save()
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        pdf.pages[0].Rotate = rotate
        pdf.pages[0].CropBox = [20, 30, 600, 780]
        pdf.save(path)
    out = tmp_path / "out.pdf"
    res = E.ocr_pdf(path, out, real_settings, skip_pages_with_text=False)
    assert res["pages"][0]["mean_confidence"] > 80
    doc = pdfium.PdfDocument(str(out))
    textpage = doc[0].get_textpage()
    search = textpage.search("revenue")
    boxes = []
    while (found := search.get_next()):
        start, count = found
        chars = [textpage.get_charbox(k) for k in range(start, start + count)]
        boxes.append([sum(b[0] + b[2] for b in chars) / (2 * count),
                      sum(b[1] + b[3] for b in chars) / (2 * count)])
    doc.close()
    assert len(boxes) == 2  # the printed word and the OCR'd one
    printed, recognised = boxes
    assert recognised == pytest.approx(printed, abs=5)


@real
def test_real_ocr_then_redact_removes_the_word_from_the_picture(tmp_path, real_settings):
    searchable = tmp_path / "searchable.pdf"
    E.ocr_pdf(scan(tmp_path / "scan.pdf", text=LINES), searchable, real_settings)
    redacted = tmp_path / "redacted.pdf"
    res = E.redact_pdf(searchable, redacted, real_settings, texts=["Acme"])
    assert res["match_count"] == 1 and res["pages"][0]["images_edited"] == 1
    assert "Acme" not in text_of(redacted)
    again = tmp_path / "again.pdf"
    E.ocr_pdf(redacted, again, real_settings, skip_pages_with_text=False)
    assert "Acme" not in text_of(again)
    assert "Corporation" in text_of(again)


def test_stopping_kills_running_pages(tmp_path, tesseract, monkeypatch):
    import threading
    import time

    monkeypatch.setenv("FAKE_TESSERACT_MODE", "hang")
    run = ocr._Run(tesseract[0], "eng", time.monotonic() + 60)
    png = tmp_path / "p0.png"
    Image.new("L", (10, 10), 255).save(png)
    errors = []

    def read():
        try:
            run.page(1, str(png), 300)
        except BaseException as exc:  # noqa: B036  the thread reports what stopped it
            errors.append(exc)

    worker = threading.Thread(target=read)
    started = time.monotonic()
    worker.start()
    while not run.running and time.monotonic() - started < 10:
        time.sleep(0.01)
    run.stop()
    worker.join(10)
    assert not worker.is_alive() and time.monotonic() - started < 10
    assert isinstance(errors[0], ocr._OutOfTime)
    assert not run.running
    with pytest.raises(ocr._OutOfTime):  # nothing new starts once stopped
        run.page(2, str(png), 300)


def test_tesseract_removed_after_it_was_checked(tmp_path):
    import time

    run = ocr._Run(str(tmp_path / "gone"), "eng", time.monotonic() + 60)
    with pytest.raises(DependencyError, match="Could not run Tesseract"):
        run.page(1, str(tmp_path / "p0.png"), 300)


def test_the_deadline_covers_placing_the_text_too(tmp_path, ocr_settings, monkeypatch):
    monkeypatch.setattr(ocr, "OCR_TIME_LIMIT_S", 3)
    real = ocr._recognise

    def slow(*args):
        found = real(*args)
        time.sleep(3.1)
        return found

    monkeypatch.setattr(ocr, "_recognise", slow)
    out = tmp_path / "out.pdf"
    with pytest.raises(PdfUserError, match="OCR took over 3 s"):
        E.ocr_pdf(scan(tmp_path / "scan.pdf"), out, ocr_settings)
    assert not out.exists()


def test_a_stopped_call_stops_waiting_for_a_tesseract_slot(tmp_path, tesseract):
    """Other calls may hold every slot for minutes; a call that failed meanwhile must
    not wait for them."""
    for _ in range(ocr.WORKERS):
        ocr._SLOTS.acquire()
    try:
        run = ocr._Run(tesseract[0], "eng", time.monotonic() + 60)
        errors = []
        waiting = threading.Thread(target=lambda: errors.append(pytest.raises(
            ocr._OutOfTime, run.page, 1, str(tmp_path / "p0.png"), 300)))
        waiting.start()
        time.sleep(0.3)
        started = time.monotonic()
        run.stop()
        waiting.join(5)
        assert not waiting.is_alive() and time.monotonic() - started < 2
    finally:
        for _ in range(ocr.WORKERS):
            ocr._SLOTS.release()


def test_stop_leaves_finished_processes_alone(tesseract, monkeypatch):
    """A finished process's pid may already belong to someone else."""
    done = subprocess.Popen([sys.executable, "-c", "pass"])
    done.wait()
    killed = []
    monkeypatch.setattr(ocr, "_kill", killed.append)
    run = ocr._Run(tesseract[0], "eng", time.monotonic() + 60)
    run.running.add(done)
    run.stop()
    assert killed == [] and run.deadline == 0
