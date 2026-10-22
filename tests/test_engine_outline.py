"""get_outline / set_outline: bookmarks as a flat list of (level, title, page)."""
from __future__ import annotations

import pikepdf
import pytest

from paperboy import engine as E
from paperboy.errors import PdfUserError


def _rows(result) -> list[tuple]:
    return [(e["level"], e["title"], e["page"]) for e in result["entries"]]


def _item(pdf, title, **fields):
    return pdf.make_indirect(pikepdf.Dictionary(Title=pikepdf.String(title), **fields))


def _link(items):
    """Chain sibling outline items with /Next."""
    for a, b in zip(items, items[1:], strict=False):
        a.Next = b
    return items[0]


@pytest.fixture
def out(tmp_path):
    return tmp_path / "out.pdf"


# --- reading -------------------------------------------------------------------------------


def test_no_outline_is_an_empty_list(doc, settings):
    res = E.get_outline(doc, settings)
    assert res == {"path": str(doc), "page_count": 5, "entries": [], "truncated": False}


def test_nested_outline_is_flattened_in_reading_order(doc, settings, out):
    E.set_outline(doc, out, [
        {"title": "Introduction", "page": 1},
        {"title": "Background", "page": 2, "level": 2},
        {"title": "Prior work", "page": 2, "level": 3},
        {"title": "Goals", "page": 3, "level": 2},
        {"title": "Results", "page": 4},
        {"title": "Appendix", "page": 5, "level": 1},
    ], settings)
    assert _rows(E.get_outline(out, settings)) == [
        (1, "Introduction", 1), (2, "Background", 2), (3, "Prior work", 2), (2, "Goals", 3),
        (1, "Results", 4), (1, "Appendix", 5)]


def test_every_kind_of_destination_resolves_to_a_page(doc, settings):
    with pikepdf.open(doc, allow_overwriting_input=True) as pdf:
        p = [page.obj for page in pdf.pages]
        pdf.Root.Dests = pikepdf.Dictionary(Old=pikepdf.Array([p[1], pikepdf.Name.Fit]))
        pdf.Root.Names = pikepdf.Dictionary(Dests=pikepdf.Dictionary(Names=pikepdf.Array([
            pikepdf.String("chap3"), pikepdf.Dictionary(D=pikepdf.Array([p[2],
                                                                          pikepdf.Name.Fit])),
        ])))
        items = [
            _item(pdf, "array", Dest=pikepdf.Array([p[0], pikepdf.Name.XYZ, 0, 700, 0])),
            _item(pdf, "old name", Dest=pikepdf.Name("/Old")),
            _item(pdf, "tree name", Dest=pikepdf.String("chap3")),
            _item(pdf, "goto", A=pikepdf.Dictionary(S=pikepdf.Name.GoTo,
                                                    D=pikepdf.Array([p[3], pikepdf.Name.Fit]))),
            _item(pdf, "missing name", Dest=pikepdf.String("nope")),
            _item(pdf, "page number", Dest=pikepdf.Array([4, pikepdf.Name.Fit])),  # remote style
            _item(pdf, "no target"),
        ]
        pdf.Root.Outlines = pdf.make_indirect(pikepdf.Dictionary(First=_link(items)))
        pdf.save(doc)
    assert [(e["title"], e["page"]) for e in E.get_outline(doc, settings)["entries"]] == [
        ("array", 1), ("old name", 2), ("tree name", 3), ("goto", 4), ("missing name", None),
        ("page number", None), ("no target", None)]


def test_links_and_other_actions_are_reported(doc, settings):
    with pikepdf.open(doc, allow_overwriting_input=True) as pdf:
        items = [
            _item(pdf, "web", A=pikepdf.Dictionary(S=pikepdf.Name.URI,
                                                   URI=pikepdf.String("https://example.com"))),
            _item(pdf, "script", A=pikepdf.Dictionary(S=pikepdf.Name.JavaScript,
                                                      JS=pikepdf.String("x()"))),
        ]
        pdf.Root.Outlines = pdf.make_indirect(pikepdf.Dictionary(First=_link(items)))
        pdf.save(doc)
    web, script = E.get_outline(doc, settings)["entries"]
    assert (web["link"], web["action"], web["page"]) == ("https://example.com", None, None)
    assert (script["link"], script["action"]) == (None, "JavaScript")


def test_damaged_outlines_are_read_once(doc, settings):
    with pikepdf.open(doc, allow_overwriting_input=True) as pdf:
        a, b = _item(pdf, "A"), _item(pdf, "B")
        a.Next, b.Next = b, a               # a loop between siblings
        b.First = b                         # and a bookmark that is its own child
        pdf.Root.Outlines = pdf.make_indirect(pikepdf.Dictionary(
            First=a, Extra=pikepdf.Dictionary(Title=pikepdf.String("t"))))
        a.Title = pikepdf.Name.NotAString   # a title that isn't text
        pdf.save(doc)
    assert [(e["level"], e["title"]) for e in E.get_outline(doc, settings)["entries"]] == \
        [(1, ""), (1, "B")]


def test_very_large_outlines_are_cut(doc, settings, monkeypatch):
    from paperboy.engine import outline
    monkeypatch.setattr(outline, "MAX_ENTRIES", 3)
    with pikepdf.open(doc, allow_overwriting_input=True) as pdf:
        items = [_item(pdf, str(n)) for n in range(5)]
        pdf.Root.Outlines = pdf.make_indirect(pikepdf.Dictionary(First=_link(items)))
        pdf.save(doc)
    res = E.get_outline(doc, settings)
    assert len(res["entries"]) == 3 and res["truncated"] is True


def test_reading_works_with_the_open_password(doc, settings, out, tmp_path):
    # Bookmarks are shown by any viewer, so copy restrictions don't hide them.
    E.set_outline(doc, out, [{"title": "One", "page": 1}], settings)
    locked = tmp_path / "locked.pdf"
    with pikepdf.open(out) as pdf:
        pdf.save(locked, encryption=pikepdf.Encryption(
            user="pw", owner="owner", R=6, allow=pikepdf.Permissions(extract=False)))
    assert _rows(E.get_outline(locked, settings, password="pw")) == [(1, "One", 1)]


# --- writing ---------------------------------------------------------------------------------


def test_writing_replaces_the_outline(doc, settings, out, tmp_path):
    first = tmp_path / "first.pdf"
    E.set_outline(doc, first, [{"title": "Old A", "page": 1}, {"title": "Old B", "page": 2}],
                  settings)
    res = E.set_outline(first, out, [{"title": "  New  ", "page": 3}], settings)
    assert res == {"output": str(out), "entries": 1, "replaced": 2, "password_protected": False,
                   "permissions_restricted": False, "user_password_changed": False}
    assert _rows(E.get_outline(out, settings)) == [(1, "New", 3)]


def test_viewers_open_with_the_bookmarks_showing(doc, settings, out, tmp_path):
    E.set_outline(doc, out, [{"title": "A", "page": 1}], settings)
    with pikepdf.open(out) as pdf:
        assert pdf.Root.PageMode == pikepdf.Name.UseOutlines
    cleared = tmp_path / "cleared.pdf"
    res = E.set_outline(out, cleared, [], settings)  # an empty list removes them
    assert (res["entries"], res["replaced"]) == (0, 1)
    with pikepdf.open(cleared) as pdf:
        assert "/Outlines" not in pdf.Root and "/PageMode" not in pdf.Root


def test_an_existing_page_mode_is_kept(doc, settings, out, tmp_path):
    with pikepdf.open(doc, allow_overwriting_input=True) as pdf:
        pdf.Root.PageMode = pikepdf.Name.FullScreen
        pdf.save(doc)
    E.set_outline(doc, out, [{"title": "A", "page": 1}], settings)
    E.set_outline(out, tmp_path / "cleared.pdf", [], settings)
    for path in (out, tmp_path / "cleared.pdf"):
        with pikepdf.open(path) as pdf:
            assert pdf.Root.PageMode == pikepdf.Name.FullScreen


def test_titles_keep_any_language(doc, settings, out):
    titles = ["Résumé", "第一章", "Глава 2", "🙂 emoji"]
    E.set_outline(doc, out, [{"title": t, "page": 1} for t in titles], settings)
    assert [e["title"] for e in E.get_outline(out, settings)["entries"]] == titles


def test_a_damaged_old_outline_does_not_block_writing(doc, settings, out):
    with pikepdf.open(doc, allow_overwriting_input=True) as pdf:
        a = _item(pdf, "loop")
        a.Next = a
        pdf.Root.Outlines = pdf.make_indirect(pikepdf.Dictionary(First=a, Last=a,
                                                                 Count=pikepdf.Name.Bad))
        pdf.save(doc)
    E.set_outline(doc, out, [{"title": "A", "page": 2}], settings)
    assert _rows(E.get_outline(out, settings)) == [(1, "A", 2)]


@pytest.mark.parametrize(("entries", "message"), [
    ([{"title": "", "page": 1}], "title must be 1 to 500"),
    ([{"title": "   ", "page": 1}], "title must be 1 to 500"),
    ([{"title": "x" * 501, "page": 1}], "title must be 1 to 500"),
    ([{"title": 5, "page": 1}], "title must be"),
    ([{"page": 1}], "title must be"),
    ([{"title": "A", "page": 0}], "page must be 1 to 5"),
    ([{"title": "A", "page": 6}], "page must be 1 to 5"),
    ([{"title": "A", "page": "1"}], "page must be 1 to 5"),
    ([{"title": "A", "page": True}], "page must be 1 to 5"),
    ([{"title": "A", "page": 1, "link": "https://example.com"}], "a page or a link, not both"),
    ([{"title": "A", "link": "javascript:alert(1)"}], "web or email address"),
    ([{"title": "A", "link": "file:///etc/passwd"}], "web or email address"),
    ([{"title": "A", "link": ""}], "link must be 1 to 2000"),
    ([{"title": "bad \ud800", "page": 1}], "invalid character"),
    ([{"title": "A", "page": 1, "level": 0}], "level must be 1 to 20"),
    ([{"title": "A", "page": 1, "level": 21}], "level must be 1 to 20"),
    ([{"title": "A", "page": 1, "level": 1.5}], "level must be 1 to 20"),
    ([{"title": "A", "page": 1, "level": 2}], "first bookmark must be at level 1"),
    ([{"title": "A", "page": 1}, {"title": "B", "page": 1, "level": 3}],
     "level 3 but the one before it is at level 1"),
    (["just a title"], "must be an object"),
])
def test_bad_entries_are_refused_before_writing(doc, settings, out, entries, message):
    with pytest.raises(PdfUserError, match=message):
        E.set_outline(doc, out, entries, settings)
    assert not out.exists()


def test_too_many_entries_are_refused(doc, settings, out, monkeypatch):
    from paperboy.engine import outline
    monkeypatch.setattr(outline, "MAX_ENTRIES", 2)
    with pytest.raises(PdfUserError, match="at most 2"):
        E.set_outline(doc, out, [{"title": "A", "page": 1}] * 3, settings)


def test_protected_output_stays_protected(make_encrypted, settings, out):
    locked = make_encrypted()
    res = E.set_outline(locked, out, [{"title": "A", "page": 2}], settings, password="pw")
    assert res["password_protected"] is True
    with pytest.raises(PdfUserError, match="supply its password"):
        E.get_outline(out, settings)
    assert _rows(E.get_outline(out, settings, password="pw")) == [(1, "A", 2)]


def test_assembly_restriction_needs_the_owner_password(make_encrypted, settings, out):
    locked = make_encrypted(owner="owner", allow=pikepdf.Permissions(modify_assembly=False))
    with pytest.raises(PdfUserError, match="does not allow changing its bookmarks"):
        E.set_outline(locked, out, [{"title": "A", "page": 1}], settings, password="pw")
    E.set_outline(locked, out, [{"title": "A", "page": 1}], settings, password="owner")
    assert out.exists()


def test_signed_pdfs_are_refused(doc, settings, out):
    with pikepdf.open(doc, allow_overwriting_input=True) as pdf:
        pdf.Root.Perms = pikepdf.Dictionary(DocMDP=pdf.make_indirect(pikepdf.Dictionary(
            Type=pikepdf.Name.Sig)))
        pdf.save(doc)
    with pytest.raises(PdfUserError, match="digitally signed"):
        E.set_outline(doc, out, [{"title": "A", "page": 1}], settings)


def test_reader_usage_rights_are_dropped(doc, settings, out):
    with pikepdf.open(doc, allow_overwriting_input=True) as pdf:
        pdf.Root.Perms = pikepdf.Dictionary(UR3=pdf.make_indirect(pikepdf.Dictionary(
            Type=pikepdf.Name.Sig)))
        pdf.save(doc)
    E.set_outline(doc, out, [{"title": "A", "page": 1}], settings)
    with pikepdf.open(out) as pdf:
        assert "/Perms" not in pdf.Root


def test_input_is_untouched(doc, settings, out):
    before = doc.read_bytes()
    E.set_outline(doc, out, [{"title": "A", "page": 1}], settings)
    assert doc.read_bytes() == before



# --- review findings -------------------------------------------------------------------------


def test_all_twenty_levels_are_written(doc, settings, out):
    entries = [{"title": f"L{n}", "page": 1, "level": n} for n in range(1, 21)]
    assert E.set_outline(doc, out, entries, settings)["entries"] == 20
    res = E.get_outline(out, settings)
    assert [e["level"] for e in res["entries"]] == list(range(1, 21))
    assert res["truncated"] is False


def test_bookmarks_nested_too_deep_are_reported_as_left_out(doc, settings, out):
    with pikepdf.open(doc, allow_overwriting_input=True) as pdf:
        parent = pdf.Root.Outlines = pdf.make_indirect(pikepdf.Dictionary())
        for n in range(25):
            child = _item(pdf, f"L{n + 1}")
            parent.First = child
            parent = child
        pdf.save(doc)
    res = E.get_outline(doc, settings)
    assert len(res["entries"]) == 20 and res["truncated"] is True
    replaced = E.set_outline(doc, out, [{"title": "A", "page": 1}], settings)["replaced"]
    assert replaced == 25                     # counted in full, however deep


def test_deep_name_trees_are_read_once(doc, settings, monkeypatch):
    # One lookup per bookmark would walk the whole chain each time (minutes for a few MB).
    from paperboy.engine import outline
    calls = []
    real = outline._name_tree
    monkeypatch.setattr(outline, "_name_tree", lambda t: calls.append(1) or real(t))
    with pikepdf.open(doc, allow_overwriting_input=True) as pdf:
        leaf = pikepdf.Dictionary(Names=pikepdf.Array([
            pikepdf.String("b"), pikepdf.Array([pdf.pages[1].obj, pikepdf.Name.Fit])]))
        node = pdf.make_indirect(leaf)
        for _ in range(2_000):                # a deep chain of /Kids
            node = pdf.make_indirect(pikepdf.Dictionary(Kids=pikepdf.Array([node])))
        pdf.Root.Names = pikepdf.Dictionary(Dests=node)
        items = [_item(pdf, str(n), Dest=pikepdf.String("b")) for n in range(500)]
        pdf.Root.Outlines = pdf.make_indirect(pikepdf.Dictionary(First=_link(items)))
        pdf.save(doc)
    res = E.get_outline(doc, settings)
    assert {e["page"] for e in res["entries"]} == {2} and len(calls) == 1


def test_name_trees_with_loops_unsorted_keys_or_odd_values(doc, settings, monkeypatch):
    from paperboy.engine import outline
    with pikepdf.open(doc, allow_overwriting_input=True) as pdf:
        p = [page.obj for page in pdf.pages]
        loop = pdf.make_indirect(pikepdf.Dictionary(Names=pikepdf.Array([   # unsorted
            pikepdf.String("z"), pikepdf.Array([p[0]]), pikepdf.String("a"),
            pikepdf.Array([p[2]]), pikepdf.Name.NotAKey, pikepdf.Array([p[3]])])))
        loop.Kids = pikepdf.Array([loop, pikepdf.Dictionary(), 5])
        pdf.Root.Names = pikepdf.Dictionary(Dests=loop)
        items = [_item(pdf, k, Dest=pikepdf.String(k)) for k in ("z", "a")]
        pdf.Root.Outlines = pdf.make_indirect(pikepdf.Dictionary(First=_link(items)))
        pdf.save(doc)
    assert [e["page"] for e in E.get_outline(doc, settings)["entries"]] == [1, 3]
    monkeypatch.setattr(outline, "MAX_TREE_NODES", 0)    # a budget, not a hang
    assert [e["page"] for e in E.get_outline(doc, settings)["entries"]] == [None, None]


def test_an_item_pointing_back_at_the_outline_root_adds_nothing(doc, settings):
    with pikepdf.open(doc, allow_overwriting_input=True) as pdf:
        root = pdf.Root.Outlines = pdf.make_indirect(pikepdf.Dictionary())
        a = _item(pdf, "A")
        a.Next, a.First = root, root
        root.First = a
        pdf.save(doc)
    assert _rows(E.get_outline(doc, settings)) == [(1, "A", None)]


def test_an_outline_that_is_not_a_dictionary_is_empty(doc, settings):
    with pikepdf.open(doc, allow_overwriting_input=True) as pdf:
        pdf.Root.Outlines = pikepdf.Array([1])
        pdf.save(doc)
    assert E.get_outline(doc, settings)["entries"] == []


def test_huge_titles_and_links_are_cut_when_read(doc, settings):
    with pikepdf.open(doc, allow_overwriting_input=True) as pdf:
        item = _item(pdf, "x" * 100_000, A=pikepdf.Dictionary(
            S=pikepdf.Name.URI, URI=pikepdf.String("https://e.com/" + "y" * 100_000)))
        pdf.Root.Outlines = pdf.make_indirect(pikepdf.Dictionary(First=item))
        pdf.save(doc)
    (entry,) = E.get_outline(doc, settings)["entries"]
    assert len(entry["title"]) == 500 and entry["title"].endswith("…")
    assert len(entry["link"]) == 2000


def test_destinations_that_are_indirect_or_not_pages(doc, settings):
    with pikepdf.open(doc, allow_overwriting_input=True) as pdf:
        items = [
            _item(pdf, "indirect", Dest=pdf.make_indirect(pikepdf.Array([pdf.pages[2].obj]))),
            _item(pdf, "catalog", Dest=pikepdf.Array([pdf.Root])),
            _item(pdf, "empty", Dest=pikepdf.Array([])),
            _item(pdf, "uri not text", A=pikepdf.Dictionary(S=pikepdf.Name.URI,
                                                            URI=pikepdf.Name.X)),
        ]
        pdf.Root.Outlines = pdf.make_indirect(pikepdf.Dictionary(First=_link(items)))
        pdf.save(doc)
    entries = E.get_outline(doc, settings)["entries"]
    assert [(e["page"], e["action"]) for e in entries] == [
        (3, None), (None, None), (None, None), (None, "URI")]


def test_what_get_outline_returns_can_be_written_back(doc, settings, out, tmp_path):
    with pikepdf.open(doc, allow_overwriting_input=True) as pdf:
        items = [
            _item(pdf, "Chapter", Dest=pikepdf.Array([pdf.pages[1].obj, pikepdf.Name.Fit])),
            _item(pdf, "Website", A=pikepdf.Dictionary(
                S=pikepdf.Name.URI, URI=pikepdf.String("https://example.com"))),
            _item(pdf, "Script", A=pikepdf.Dictionary(S=pikepdf.Name.JavaScript,
                                                      JS=pikepdf.String("x()"))),
            _item(pdf, "Heading"),
        ]
        pdf.Root.Outlines = pdf.make_indirect(pikepdf.Dictionary(First=_link(items)))
        pdf.save(doc)
    entries = E.get_outline(doc, settings)["entries"]
    E.set_outline(doc, out, entries, settings)
    again = E.get_outline(out, settings)["entries"]
    assert [(e["title"], e["page"], e["link"], e["action"]) for e in again] == [
        ("Chapter", 2, None, None), ("Website", None, "https://example.com", None),
        ("Script", None, None, None),        # the script is not carried over
        ("Heading", None, None, None)]
    with pikepdf.open(out) as pdf:
        assert b"x()" not in b"".join(pdf.Root.Outlines.unparse(resolved=True).split())


def test_explicit_use_none_page_mode_opens_the_bookmarks(doc, settings, out):
    with pikepdf.open(doc, allow_overwriting_input=True) as pdf:
        pdf.Root.PageMode = pikepdf.Name.UseNone
        pdf.save(doc)
    E.set_outline(doc, out, [{"title": "A", "page": 1}], settings)
    with pikepdf.open(out) as pdf:
        assert pdf.Root.PageMode == pikepdf.Name.UseOutlines


def test_old_rc4_files_map_assembly_to_the_modify_bit(doc, settings, out, tmp_path):
    old = tmp_path / "old.pdf"
    with pikepdf.open(doc) as pdf:
        pdf.save(old, encryption=pikepdf.Encryption(
            user="pw", owner="owner", R=2, metadata=False, aes=False,
            allow=pikepdf.Permissions(modify_other=False)))
    with pytest.raises(PdfUserError, match="changing its bookmarks"):
        E.set_outline(old, out, [{"title": "A", "page": 1}], settings, password="pw")


def test_only_web_and_email_addresses_count_as_links(doc, settings, out):
    with pikepdf.open(doc, allow_overwriting_input=True) as pdf:
        items = [_item(pdf, url, A=pikepdf.Dictionary(S=pikepdf.Name.URI,
                                                      URI=pikepdf.String(url)))
                 for url in ("mailto:a@example.com", "javascript:alert(1)", "file:///x",
                             "http://[broken")]
        pdf.Root.Outlines = pdf.make_indirect(pikepdf.Dictionary(First=_link(items)))
        pdf.save(doc)
    entries = E.get_outline(doc, settings)["entries"]
    assert [(e["link"], e["action"]) for e in entries] == [
        ("mailto:a@example.com", None), (None, "URI"), (None, "URI"), (None, "URI")]
    E.set_outline(doc, out, entries, settings)       # and they can be written back
