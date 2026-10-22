"""extract_tables / export_tables: tables as rows, and as CSV files that are safe to open."""
from __future__ import annotations

import csv
import dataclasses
import io
import zlib

import pikepdf
import pytest
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from paperboy import engine as E
from paperboy.engine import tables as T
from paperboy.errors import PdfUserError

REGIONS = [["Region", "Deliveries", "On time"], ["North", "12,400", "98.1%"],
           ["South", "10,950", "97.4%"], ["East", "8,210", "96.9%"]]


def _table(rows, grid=True):
    t = Table(rows)
    if grid:
        t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, colors.black)]))
    return t


def _doc(path, *pages):
    """Each argument is a page: a list of flowables."""
    story = []
    for n, flowables in enumerate(pages):
        if n:
            story.append(PageBreak())
        story += flowables or [Paragraph("No tables here.", getSampleStyleSheet()["Normal"])]
    SimpleDocTemplate(str(path), pagesize=letter).build(story)
    return path


@pytest.fixture
def report(tmp_path):
    return _doc(tmp_path / "report.pdf",
                [_table(REGIONS), Spacer(1, 40), _table([["Item", "Cost"], ["Vans", "1,200"]])],
                [],
                [_table([["Q", "Target"], ["Q4", "2%"]])])


def _plain(result):
    return [(t["page"], t["index"], t["rows"]) for t in result["tables"]]


# --- extract_tables ---------------------------------------------------------------------------


def test_tables_with_lines_are_found_in_page_order(report, settings):
    res = E.extract_tables(report, settings)
    assert _plain(res) == [(1, 1, REGIONS), (1, 2, [["Item", "Cost"], ["Vans", "1,200"]]),
                           (3, 1, [["Q", "Target"], ["Q4", "2%"]])]
    first = res["tables"][0]
    assert (first["row_count"], first["column_count"]) == (4, 3)
    assert res["pages_searched"] == 3 and res["complete"] is True
    assert res["truncated"] is False and res["pages_skipped"] == []


def test_pages_and_max_tables(report, settings):
    assert [t["page"] for t in E.extract_tables(report, settings, pages="3")["tables"]] == [3]
    res = E.extract_tables(report, settings, max_tables=1)
    assert len(res["tables"]) == 1 and res["truncated"] is True and res["complete"] is True


def test_tables_without_lines_need_the_text_strategy(tmp_path, settings):
    doc = _doc(tmp_path / "plain.pdf", [_table(REGIONS, grid=False)])
    assert E.extract_tables(doc, settings)["tables"] == []
    rows = E.extract_tables(doc, settings, strategy="text")["tables"][0]["rows"]
    assert ["North", "12,400", "98.1%"] in rows


def _turn(path, degrees):
    """Store the page turned, with /Rotate turning it back: it looks the same, but its
    content runs sideways or upside down, as in landscape tables on portrait paper."""
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        page = pdf.pages[0]
        w, h = float(page.mediabox[2]), float(page.mediabox[3])
        W, H = (h, w) if degrees in (90, 270) else (w, h)
        matrix = {90: (0, 1, -1, 0, W, 0), 180: (-1, 0, 0, -1, W, H),
                  270: (0, -1, 1, 0, 0, H)}[degrees]
        page.contents_add(pikepdf.Stream(pdf, ("q " + " ".join(map(str, matrix)) + " cm\n")
                                         .encode()), prepend=True)
        page.contents_add(pikepdf.Stream(pdf, b"\nQ\n"))
        page.obj.MediaBox = pikepdf.Array([0, 0, W, H])
        page.obj.Rotate = degrees
        pdf.save(path)
    return path


@pytest.mark.parametrize("degrees", [90, 180, 270])
def test_tables_on_rotated_pages_read_as_seen(tmp_path, settings, degrees):
    doc = _turn(_doc(tmp_path / "r.pdf", [_table(REGIONS)]), degrees)
    assert E.extract_tables(doc, settings)["tables"][0]["rows"] == REGIONS


def test_cells_are_cleaned_and_capped(tmp_path, settings, monkeypatch):
    monkeypatch.setattr(T, "MAX_CELL", 10)
    doc = _doc(tmp_path / "c.pdf", [_table([["a\n  b", "x" * 50], ["", "y"]])])
    rows = E.extract_tables(doc, settings)["tables"][0]["rows"]
    assert rows == [["a b", "x" * 9 + "…"], ["", "y"]]


def test_the_cell_limit_stops_early(report, settings, monkeypatch):
    monkeypatch.setattr(T, "MAX_CELLS", 13)
    res = E.extract_tables(report, settings)
    assert len(res["tables"]) == 1 and res["truncated"] is True


def test_pages_with_too_much_content_are_skipped(report, settings, monkeypatch):
    monkeypatch.setattr(T, "MAX_PAGE_CONTENT", 1)
    res = E.extract_tables(report, settings)
    assert res["tables"] == [] and res["pages_skipped"] == [1, 2, 3]


def test_font_data_counts_toward_the_budget(report, settings, monkeypatch):
    with pikepdf.open(report, allow_overwriting_input=True) as pdf:
        fonts = pdf.pages[0].obj.Resources.Font
        name, font = next(iter(fonts.items()))
        cmap = pikepdf.Stream(pdf, b"")
        cmap.write(zlib.compress(b"%" * 50_000), filter=pikepdf.Name.FlateDecode)
        font.ToUnicode = pdf.make_indirect(cmap)
        pdf.save(report)
    monkeypatch.setattr(T, "MAX_FONT_DATA", 10_000)
    # reportlab shares one font object between pages, so every page carries the map.
    assert E.extract_tables(report, settings)["pages_skipped"] == [1, 2, 3]


def test_font_data_that_cannot_be_measured_is_skipped(report, settings):
    with pikepdf.open(report, allow_overwriting_input=True) as pdf:
        _, font = next(iter(pdf.pages[2].obj.Resources.Font.items()))
        descriptor = pikepdf.Dictionary(Type=pikepdf.Name.FontDescriptor)
        program = pikepdf.Stream(pdf, b"")
        program.write(b"\x80\x0b", filter=pikepdf.Array([pikepdf.Name.LZWDecode,
                                                          pikepdf.Name.FlateDecode]))
        descriptor.FontFile2 = pdf.make_indirect(program)
        font.FontDescriptor = pdf.make_indirect(descriptor)
        font.DescendantFonts = pikepdf.Array([pikepdf.Dictionary(
            FontDescriptor=font.FontDescriptor)])
        pdf.save(report, compress_streams=False,
                 stream_decode_level=pikepdf.StreamDecodeLevel.none)
    assert E.extract_tables(report, settings)["pages_skipped"] == [1, 2, 3]   # shared font


def test_fonts_inside_forms_are_counted_once(report, settings, monkeypatch):
    with pikepdf.open(report, allow_overwriting_input=True) as pdf:
        page = pdf.pages[0].obj
        form = pikepdf.Stream(pdf, b"")
        form.Subtype, form.BBox = pikepdf.Name.Form, pikepdf.Array([0, 0, 1, 1])
        form = pdf.make_indirect(form)
        form.Resources = pikepdf.Dictionary(Font=page.Resources.Font,
                                            XObject=pikepdf.Dictionary(Me=form))  # a loop
        page.Resources.XObject = pikepdf.Dictionary(Fm=form, Junk=pikepdf.Dictionary())
        page.Resources.Font.Bad = pikepdf.String("not a font")
        pdf.save(report)
    measured = []
    real = T.decoded_size
    monkeypatch.setattr(T, "decoded_size", lambda s, c: measured.append(s.objgen)
                        or real(s, c))
    assert E.extract_tables(report, settings, pages="1")["tables"]
    assert len(measured) == len(set(measured))


def test_the_time_limit_returns_a_partial_result(report, settings, monkeypatch):
    monkeypatch.setattr(T, "TABLE_TIME_LIMIT_S", -1)
    res = E.extract_tables(report, settings)
    assert res["complete"] is False and res["pages_searched"] == 1
    assert len(res["tables"]) == 2


@pytest.mark.parametrize(("kwargs", "message"), [
    ({"strategy": "grid"}, "strategy must be one of"),
    ({"max_tables": 0}, "max_tables must be 1 to 200"),
    ({"max_tables": 201}, "max_tables must be 1 to 200"),
    ({"max_tables": True}, "max_tables must be"),
])
def test_bad_arguments(report, settings, kwargs, message):
    with pytest.raises(PdfUserError, match=message):
        E.extract_tables(report, settings, **kwargs)


def test_protected_pdfs(report, settings, tmp_path):
    allowed, forbidden = tmp_path / "allowed.pdf", tmp_path / "forbidden.pdf"
    with pikepdf.open(report) as pdf:
        pdf.save(allowed, encryption=pikepdf.Encryption(user="pw", owner="owner", R=6))
        pdf.save(forbidden, encryption=pikepdf.Encryption(
            user="pw", owner="owner", R=6, allow=pikepdf.Permissions(extract=False)))
    assert len(E.extract_tables(allowed, settings, password="pw")["tables"]) == 3
    with pytest.raises(PdfUserError, match="does not allow copying.*extracting tables"):
        E.extract_tables(forbidden, settings, password="pw")
    assert len(E.extract_tables(forbidden, settings, password="owner")["tables"]) == 3


# --- export_tables ----------------------------------------------------------------------------


def _read_csv(path):
    data = path.read_bytes()
    assert data.startswith(b"\xef\xbb\xbf")          # the BOM Excel needs for UTF-8
    return list(csv.reader(io.StringIO(data.decode("utf-8-sig"))))


def test_each_table_becomes_a_csv_file(report, settings, tmp_path):
    out = tmp_path / "tables"
    res = E.export_tables(report, out, settings)
    assert [f["file"].rsplit("/", 1)[-1] for f in res["files"]] == [
        "page001-table01.csv", "page001-table02.csv", "page003-table01.csv"]
    assert _read_csv(out / "page001-table01.csv") == REGIONS
    assert res["files"][0]["row_count"] == 4 and res["complete"] is True


def test_formulas_are_escaped_but_numbers_are_not(tmp_path, settings):
    rows = [["=HYPERLINK(\"http://x\")", "+1+cmd", "-5", "-1,200.50", "@SUM(A1)", "ok"]]
    doc = _doc(tmp_path / "f.pdf", [_table(rows)])
    out = tmp_path / "t"
    E.export_tables(doc, out, settings)
    assert _read_csv(out / "page001-table01.csv") == [[
        "'=HYPERLINK(\"http://x\")", "'+1+cmd", "-5", "-1,200.50", "'@SUM(A1)", "ok"]]
    E.export_tables(doc, out, settings, escape_formulas=False, overwrite=True)
    assert _read_csv(out / "page001-table01.csv")[0][0] == "=HYPERLINK(\"http://x\")"


def test_existing_files_stop_everything_unless_overwriting(report, settings, tmp_path):
    out = tmp_path / "t"
    out.mkdir()
    (out / "page003-table01.csv").write_text("old")
    with pytest.raises(PdfUserError, match="already exists"):
        E.export_tables(report, out, settings)
    assert sorted(p.name for p in out.iterdir()) == ["page003-table01.csv"]
    E.export_tables(report, out, settings, overwrite=True)
    assert _read_csv(out / "page003-table01.csv")[0] == ["Q", "Target"]


def test_a_planted_link_is_never_followed(report, settings, tmp_path):
    out = tmp_path / "t"
    out.mkdir()
    victim = tmp_path / "victim.txt"
    victim.write_text("precious")
    (out / "page001-table01.csv").symlink_to(victim)
    with pytest.raises(PdfUserError, match="not a regular file"):
        E.export_tables(report, out, settings, overwrite=True)
    assert victim.read_text() == "precious"


def test_no_tables_writes_nothing(tmp_path, settings):
    doc = _doc(tmp_path / "none.pdf", [])
    out = tmp_path / "t"
    assert E.export_tables(doc, out, settings)["files"] == []
    assert not out.exists()


def test_export_passes_the_limits_through(report, settings, tmp_path, monkeypatch):
    monkeypatch.setattr(T, "MAX_PAGE_CONTENT", 1)
    res = E.export_tables(report, tmp_path / "t", settings)
    assert res["pages_skipped"] == [1, 2, 3] and res["files"] == []


def test_the_page_cap_still_applies(report, settings):
    with pytest.raises(PdfUserError):
        E.extract_tables(report, dataclasses.replace(settings, max_pages=2))
