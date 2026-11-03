"""markdown_worker, run in this process: what the pdf_to_markdown child process does."""
from __future__ import annotations

import io
import json
import sys

from reportlab.pdfgen import canvas
from test_engine_tables import REGIONS, _doc, _grid, _table

from paperboy.engine import markdown_worker as W
from paperboy.engine.table_worker import MAX_ERROR, MAX_INTERSECTIONS


def _request(path, **overrides):
    request = {"path": str(path), "page_count": 1, "pages": [0], "tables": True,
               "max_tables": 10, "max_cells": 1000, "max_cell": 50, "max_lines": 1000,
               "max_text": 100_000,
               "max_intersections": MAX_INTERSECTIONS}
    return {**request, **overrides}


def _records(capsys) -> list[dict]:
    return [json.loads(line) for line in capsys.readouterr().out.splitlines()]


def _page(path, *rows):
    c = canvas.Canvas(str(path))
    for x, y, text, font, size in rows:
        c.setFont(font, size)
        c.drawString(x, y, text)
    c.save()
    return path


def test_lines_carry_position_size_boldness_and_box(tmp_path, capsys):
    pdf = _page(tmp_path / "p.pdf", (72, 700, "Title", "Helvetica-Bold", 20),
                (72, 600, "Body (cid:9) text", "Helvetica", 10))
    W.run(_request(pdf))
    (record,) = _records(capsys)
    assert record["tables"] == []
    title, body = record["lines"]
    assert title[0] == "Title" and title[5] == 20 and title[6] is True
    assert body[0] == "Body (cid:9) text" and body[6] is False
    assert title[7] != body[7]
    x0, top, x1, bottom = title[1:5]
    assert x0 == 72 and x1 > x0 and 842 - 700 - 25 < top < bottom <= 842 - 700 + 6


def test_unmapped_characters_and_long_lines(monkeypatch):
    from pdfminer.layout import LTAnno, LTChar

    class Char(LTChar):
        def __init__(self, text, size=10, fontname="Arial"):
            self.text, self.size, self.fontname = text, size, fontname

        def get_text(self):
            return self.text

    class Line(list):
        bbox = (10, 20, 30, 40)

        def get_text(self):
            return "".join(c.get_text() for c in self)

    bold = [Char("(cid:3)", 10, "ABCDEF+Arial-BoldMT"), LTAnno(" "),
            Char("x", 10, "Arial-BoldMT"), Char("y", 12)]
    assert W._line_record(Line(bold), 4, 100) == ["\ufffd xy", 10, 60, 30, 80, 10, True, 4]
    monkeypatch.setattr(W, "MAX_LINE", 5)
    assert W._line_record(Line(Char(c) for c in "abcdefgh"), 1, 100)[0] == "abcd\u2026"
    assert W._line_record(Line([Char(" "), LTAnno("\n")]), 1, 100) is None
    assert W._line_record(Line([LTAnno("x")]), 1, 100) is None
    infinite = Line([Char("x")])
    infinite.bbox = (10, 20, float("inf"), 40)
    assert W._line_record(infinite, 1, 100) is None  # left out, not sent as Infinity
    assert W._line_record(Line([Char("x", float("nan"))]), 1, 100) is None


def test_the_text_budget_counts_lines_and_cells(tmp_path, capsys):
    doc = _doc(tmp_path / "d.pdf", [_table(REGIONS)])
    W.run(_request(doc, max_text=10))
    (record,) = _records(capsys)
    assert record["truncated"] is True and sum(len(line[0]) for line in record["lines"]) <= 10
    W.run(_request(doc))
    text = sum(len(line[0]) for line in _records(capsys)[0]["lines"])
    W.run(_request(doc, max_text=text + 5))  # the lines fit; the table's cells don't
    (record,) = _records(capsys)
    assert record["truncated"] is True and record["tables"] == [] and record["lines"]


def test_tables_have_their_place_and_rows(tmp_path, capsys):
    W.run(_request(_doc(tmp_path / "d.pdf", [_table(REGIONS)])))
    (record,) = _records(capsys)
    (table,) = record["tables"]
    assert table["rows"] == REGIONS and len(table["bbox"]) == 4
    assert table["bbox"][0] < table["bbox"][2] and table["bbox"][1] < table["bbox"][3]
    W.run(_request(_doc(tmp_path / "d.pdf", [_table(REGIONS)]), tables=False))
    assert _records(capsys)[0]["tables"] == []


def test_limits_stop_with_a_truncated_record(tmp_path, capsys):
    doc = _doc(tmp_path / "d.pdf", [_table(REGIONS), _table([["a", "b"]])], [])
    W.run(_request(doc, page_count=2, pages=[0, 1], max_tables=1))
    (record,) = _records(capsys)
    assert record["truncated"] is True and record["tables"][0]["rows"] == REGIONS
    W.run(_request(doc, page_count=2, pages=[0, 1], max_cells=5))
    assert _records(capsys)[0]["tables"] == []
    W.run(_request(doc, page_count=2, pages=[0, 1], max_lines=2))
    (record,) = _records(capsys)
    assert record["truncated"] is True and len(record["lines"]) == 2


def test_empty_rows_and_tables_are_left_out(tmp_path, capsys):
    W.run(_request(_doc(tmp_path / "d.pdf", [_table([["a", "b"], ["", ""], ["c", "d"]]),
                                              _table([["", ""]])])))
    (table,) = _records(capsys)[0]["tables"]
    # (how many columns pdfplumber sees in so small a grid varies)
    assert [[cell for cell in row if cell] for row in table["rows"]] == [["a", "b"], ["c", "d"]]


def test_tables_with_no_text_are_left_out(tmp_path, capsys, monkeypatch):
    import pdfplumber.page

    class Empty:
        bbox = (0, 0, 1, 1)

        def extract(self):
            return [["", None]]
    monkeypatch.setattr(pdfplumber.page.Page, "find_tables", lambda self: [Empty()])
    W.run(_request(_doc(tmp_path / "d.pdf", [_table(REGIONS)])))
    assert _records(capsys)[0]["tables"] == []


def test_tables_at_impossible_places_are_left_out(tmp_path, capsys, monkeypatch):
    import pdfplumber.page

    class Far:
        bbox = (0, 0, 1, float("inf"))

        def extract(self):
            return [["a"]]
    monkeypatch.setattr(pdfplumber.page.Page, "find_tables", lambda self: [Far()])
    W.run(_request(_doc(tmp_path / "d.pdf", [_table(REGIONS)])))
    assert _records(capsys)[0]["tables"] == []


def test_dense_grids_get_no_tables(tmp_path, capsys):
    W.run(_request(_grid(tmp_path / "g.pdf", 50), max_intersections=100))
    assert _records(capsys)[0]["tables"] == []


def test_a_file_that_cannot_be_opened(tmp_path, capsys):
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"%PDF-1.7\nnothing")
    W.run(_request(bad))
    (record,) = _records(capsys)
    assert set(record) == {"error"} and len(record["error"]) <= MAX_ERROR


def test_a_page_count_that_disagrees_stops_everything(tmp_path, capsys):
    W.run(_request(_doc(tmp_path / "d.pdf", [_table(REGIONS)]), page_count=2))
    assert _records(capsys) == [{"error": "the page tree can't be read consistently"}]


def test_a_failing_page_is_skipped(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(W, "_lines", lambda *a: (_ for _ in ()).throw(ValueError("bad")))
    W.run(_request(_doc(tmp_path / "d.pdf", [_table(REGIONS)])))
    assert _records(capsys) == [{"page": 0, "skipped": "it can't be read: bad"}]


def test_text_in_figures_and_loose_lines(tmp_path):
    from pdfminer.layout import LTFigure, LTTextLineHorizontal

    class Figure(LTFigure):
        def __init__(self, items):
            self.items = items

        def __iter__(self):
            return iter(self.items)

    class Loose(LTTextLineHorizontal):
        def __init__(self):
            pass

    calls = []
    real = W._line_record
    empty = Loose()
    W._line_record = lambda line, box, height: calls.append(box) or (
        None if line is empty else ["x"])
    try:
        found, more = W._lines([Figure([Loose(), "not text"]), empty, Loose()], 100.0,
                               {"lines": 10, "text": 10})
    finally:
        W._line_record = real
    assert (found, more, calls) == ([["x"], ["x"]], False, [1, 2, 3])


def test_main_reads_the_request_from_stdin(tmp_path, capsys, monkeypatch):
    pdf = _page(tmp_path / "p.pdf", (72, 700, "Hi", "Helvetica", 10))
    request = {**_request(pdf), "cpu_seconds": 30, "memory_bytes": 2**31}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(request)))
    limits = []
    monkeypatch.setattr(W, "_limit", lambda cpu, memory: limits.append((cpu, memory)))
    W.main()
    assert limits == [(30, 2**31)]
    assert [line[0] for line in _records(capsys)[0]["lines"]] == ["Hi"]


def test_the_worker_runs_as_a_child_process(tmp_path):
    from paperboy.engine.tables import _run
    pdf = _page(tmp_path / "p.pdf", (72, 700, "Hi", "Helvetica", 10))
    records, finished = _run("paperboy.engine.markdown_worker", _request(pdf), 30)
    assert finished and [line[0] for line in records[0]["lines"]] == ["Hi"]
