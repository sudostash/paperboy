"""Page specification parsing: 1-based human specs -> 0-based indices."""
from __future__ import annotations

import pytest

from paperboy.errors import PdfUserError
from paperboy.pagespec import parse_pages


@pytest.mark.parametrize(("spec", "expected"), [
    ("1", [0]),
    ("1,3,5", [0, 2, 4]),
    ("2-4", [1, 2, 3]),
    ("1,3,5-8", [0, 2, 4, 5, 6, 7]),
    ("4-", [3, 4, 5, 6, 7, 8, 9]),          # open end runs to the last page
    ("-3", [0, 1, 2]),                      # open start begins at page 1
    ("5-1", [4, 3, 2, 1, 0]),               # descending range reverses
    ("last", [9]),
    ("end", [9]),
    ("first", [0]),
    ("LAST,1", [9, 0]),                     # keywords are case-insensitive
    ("last-1", [9, 8, 7, 6, 5, 4, 3, 2, 1, 0]),
    ("8-last", [7, 8, 9]),
    (" 1 , 2 ", [0, 1]),                    # whitespace is ignored
    ("1,,2,", [0, 1]),                      # empty parts are skipped
    ("2,2,2", [1, 1, 1]),                   # repeats duplicate pages
    ("3-3", [2]),
    ("10", [9]),                            # upper boundary
])
def test_parses_valid_specs(spec, expected):
    assert parse_pages(spec, 10) == expected


def test_dedupe_keeps_first_occurrence_order():
    assert parse_pages("3,1,3,2,1", 5, dedupe=True) == [2, 0, 1]


def test_without_dedupe_order_and_repeats_are_kept():
    assert parse_pages("3,1,3", 5) == [2, 0, 2]


@pytest.mark.parametrize("spec", ["", "   ", None])
def test_rejects_empty_spec(spec):
    with pytest.raises(PdfUserError, match="Empty page specification"):
        parse_pages(spec, 5)


@pytest.mark.parametrize("spec", [",", " , ,"])
def test_rejects_spec_selecting_nothing(spec):
    with pytest.raises(PdfUserError, match="selected no pages"):
        parse_pages(spec, 5)


@pytest.mark.parametrize("spec", ["0", "6", "1-6", "0-2", "6-"])
def test_rejects_out_of_range_pages(spec):
    with pytest.raises(PdfUserError, match="out of range"):
        parse_pages(spec, 5)


@pytest.mark.parametrize("spec", [
    "abc", "1.5", "1-2-3", "--1", "1;2", "+1", "²", "١", "1e3", "0x2",
])
def test_rejects_invalid_tokens_as_user_errors(spec):
    # Every bad spec must surface as a PdfUserError (shown to the caller),
    # never an internal crash such as ValueError.
    with pytest.raises(PdfUserError):
        parse_pages(spec, 5)


def test_last_on_empty_document_is_out_of_range():
    with pytest.raises(PdfUserError, match="out of range"):
        parse_pages("last", 0)
