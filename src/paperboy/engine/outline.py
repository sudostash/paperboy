"""Bookmarks (the PDF outline): read them as a flat list, or replace them.

Both tools use the same shape, one entry per bookmark in reading order with its
nesting level, so an outline can be read, edited and written back. Writing keeps
titles, nesting, target pages and web links; anything else a bookmark did (scripts,
other actions), its colour and style, open/closed state and exact scroll position
are not carried over.
"""
from __future__ import annotations

from pathlib import Path
from urllib.parse import urlsplit

import pikepdf
from pikepdf import OutlineItem

from ..config import Settings
from ..errors import PdfUserError
from ..results import OutlineResult, SetOutlineResult
from .base import inherit_protection, open_pdf, signature_state

MAX_ENTRIES = 10_000     # bookmarks read or written per file
MAX_LEVEL = 20           # nesting depth
MAX_TITLE = 500          # characters per title (longer titles are cut when read)
MAX_LINK = 2_000         # characters per link
MAX_TREE_NODES = 100_000  # name-tree nodes visited per file
LINK_SCHEMES = {"http", "https", "mailto"}


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit - 1] + "…"


# --- reading -----------------------------------------------------------------------------


def _name_tree(tree) -> dict[str, object]:
    """Every name in a /Names /Dests tree, read once (cycle-safe, within a node budget).

    Looking names up one by one would walk the tree once per bookmark, which a deep
    hostile tree turns into minutes; reading it whole also finds names in unsorted trees.
    """
    found: dict[str, object] = {}
    seen, stack, visited = set(), [tree], 0
    while stack and visited < MAX_TREE_NODES:
        node = stack.pop()
        if not isinstance(node, pikepdf.Dictionary):
            continue
        if node.is_indirect:  # only indirect objects can form loops
            if node.objgen in seen:
                continue
            seen.add(node.objgen)
        visited += 1
        names = node.get("/Names")
        if isinstance(names, pikepdf.Array):
            for i in range(0, len(names) - 1, 2):
                if isinstance(names[i], pikepdf.String):
                    found.setdefault(str(names[i]), names[i + 1])
        kids = node.get("/Kids")
        if isinstance(kids, pikepdf.Array):
            stack.extend(reversed(list(kids)))
    return found


def _named_destinations(pdf: pikepdf.Pdf) -> tuple[pikepdf.Dictionary | None, dict]:
    """The old-style /Dests dictionary (names) and the /Names /Dests tree (strings)."""
    old = pdf.Root.get("/Dests")
    names = pdf.Root.get("/Names")
    tree = names.get("/Dests") if isinstance(names, pikepdf.Dictionary) else None
    return (old if isinstance(old, pikepdf.Dictionary) else None, _name_tree(tree))


def _page_of(dest, pages: dict, named) -> int | None:
    """The 1-based page a destination points to, or None if it isn't a page here."""
    old, tree = named
    if isinstance(dest, pikepdf.Name) and old is not None:
        dest = old.get(dest)
    elif isinstance(dest, pikepdf.String):
        dest = tree.get(str(dest))
    if isinstance(dest, pikepdf.Dictionary):  # a named destination may be wrapped: << /D [...] >>
        dest = dest.get("/D")
    if isinstance(dest, pikepdf.Array) and len(dest) > 0:
        target = dest[0]
        if isinstance(target, pikepdf.Dictionary) and target.is_indirect:
            index = pages.get(target.objgen)
            return None if index is None else index + 1
    return None


def _entry(item: pikepdf.Dictionary, level: int, pages: dict, named) -> dict:
    title = item.get("/Title")
    entry = {"level": level,
             "title": _cut(str(title), MAX_TITLE) if isinstance(title, pikepdf.String) else "",
             "page": None, "link": None, "action": None}
    if "/Dest" in item:
        entry["page"] = _page_of(item.Dest, pages, named)
        return entry
    action = item.get("/A")
    kind = action.get("/S") if isinstance(action, pikepdf.Dictionary) else None
    if kind == pikepdf.Name.GoTo:
        entry["page"] = _page_of(action.get("/D"), pages, named)
    elif kind == pikepdf.Name.URI and _is_link(action.get("/URI")):
        entry["link"] = _cut(str(action.URI), MAX_LINK)
    elif isinstance(kind, pikepdf.Name):
        # e.g. JavaScript, Launch, GoToR, or URI for an address that isn't a web or email
        # one (javascript:, file:): those aren't links set_outline would write back.
        entry["action"] = str(kind).lstrip("/")
    return entry


def _is_link(uri) -> bool:
    if not isinstance(uri, pikepdf.String):
        return False
    try:
        return urlsplit(str(uri)).scheme.lower() in LINK_SCHEMES
    except ValueError:
        return False


def _walk(pdf: pikepdf.Pdf, max_level: int = MAX_LEVEL
          ) -> tuple[list[tuple[pikepdf.Dictionary, int]], bool]:
    """(bookmark, level) in reading order: a bookmark, its children, then its next
    sibling. Loops and repeats (damaged files) are followed once. Also whether any
    were left out: past MAX_ENTRIES, or nested deeper than *max_level*."""
    outlines = pdf.Root.get("/Outlines")
    if not isinstance(outlines, pikepdf.Dictionary):
        return [], False
    # The outline root itself is never a bookmark, even if an item links back to it.
    seen = {outlines.objgen} if outlines.is_indirect else set()
    found, stack, skipped = [], [(outlines.get("/First"), 1)], False
    while stack:
        item, level = stack.pop()
        if not isinstance(item, pikepdf.Dictionary):
            continue
        if item.is_indirect:
            if item.objgen in seen:
                continue
            seen.add(item.objgen)
        if level > max_level:
            skipped = True
            continue
        if len(found) >= MAX_ENTRIES:
            return found, True
        found.append((item, level))
        stack.append((item.get("/Next"), level))
        stack.append((item.get("/First"), level + 1))
    return found, skipped


def get_outline(path: Path, settings: Settings, password: str | None = None) -> OutlineResult:
    """The bookmarks in reading order, each with its level (1 = top) and target page."""
    with open_pdf(path, settings, password) as pdf:
        pages = {page.objgen: i for i, page in enumerate(pdf.pages)}
        named = _named_destinations(pdf)
        items, truncated = _walk(pdf)
        return {"path": str(path), "page_count": len(pdf.pages),
                "entries": [_entry(item, level, pages, named) for item, level in items],
                "truncated": truncated}


# --- writing -----------------------------------------------------------------------------


def _text(value, n: int, what: str, limit: int, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise PdfUserError(f"Bookmark {n}{label}: {what} must be 1 to {limit} characters")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise PdfUserError(f"Bookmark {n}{label}: {what} has an invalid character") from None
    return value.strip()


def _check_entries(entries: list, page_count: int) -> list[tuple[int, str, int | None, str | None]]:
    """(level, title, 0-based page or None, link or None) for each entry, after checking
    all of them."""
    if len(entries) > MAX_ENTRIES:
        raise PdfUserError(f"Too many bookmarks: at most {MAX_ENTRIES}")
    checked, previous = [], 0
    for n, entry in enumerate(entries, 1):
        if not isinstance(entry, dict):
            raise PdfUserError(f"Bookmark {n} must be an object with a title and a page")
        title = _text(entry.get("title"), n, "title", MAX_TITLE, "")
        label = f" ({title!r})"
        page, link, level = entry.get("page"), entry.get("link"), entry.get("level", 1)
        if page is not None and (not isinstance(page, int) or isinstance(page, bool)
                                 or not 1 <= page <= page_count):
            raise PdfUserError(f"Bookmark {n}{label}: page must be 1 to {page_count}")
        if link is not None:
            link = _text(link, n, "link", MAX_LINK, label)
            if urlsplit(link).scheme.lower() not in LINK_SCHEMES:
                raise PdfUserError(f"Bookmark {n}{label}: link must be a web or email "
                                   "address (http, https or mailto)")
            if page is not None:
                raise PdfUserError(f"Bookmark {n}{label}: give a page or a link, not both")
        if not isinstance(level, int) or isinstance(level, bool) or not 1 <= level <= MAX_LEVEL:
            raise PdfUserError(f"Bookmark {n}{label}: level must be 1 to {MAX_LEVEL}")
        if level > previous + 1:
            raise PdfUserError(
                f"Bookmark {n}{label} is at level {level} but the one before it is at "
                f"level {previous}; a bookmark can only be one level deeper than the one "
                "before it" if previous else
                f"Bookmark {n}{label}: the first bookmark must be at level 1")
        checked.append((level, title, None if page is None else page - 1, link))
        previous = level
    return checked


def _item(title: str, page: int | None, link: str | None) -> OutlineItem:
    if link is not None:
        return OutlineItem(title, action=pikepdf.Dictionary(
            S=pikepdf.Name.URI, URI=pikepdf.String(link)))
    return OutlineItem(title, page)  # no page: a heading that goes nowhere


def set_outline(path: Path, out: Path, entries: list, settings: Settings,
                password: str | None = None) -> SetOutlineResult:
    """Replace the bookmarks with *entries*; an empty list removes them all."""
    with open_pdf(path, settings, password) as pdf:
        if pdf.is_encrypted and not pdf.owner_password_matched and not pdf.allow.modify_assembly:
            raise PdfUserError(
                "This PDF does not allow changing its bookmarks; that needs its owner password"
            )
        signed, usage_rights = signature_state(pdf)
        if signed:
            raise PdfUserError(
                "This PDF is digitally signed; changing its bookmarks would invalidate the "
                "signature"
            )
        checked = _check_entries(entries, len(pdf.pages))
        replaced = len(_walk(pdf, max_level=MAX_ENTRIES)[0])
        if "/Outlines" in pdf.Root:  # replaced whole, so a damaged outline can't get in the way
            del pdf.Root.Outlines
        if checked:
            # pikepdf writes 15 levels unless told otherwise.
            with pdf.open_outline(max_depth=MAX_LEVEL + 1) as outline:
                parents = [outline.root]   # parents[level - 1] holds that level's siblings
                for level, title, page, link in checked:
                    item = _item(title, page, link)
                    del parents[level:]
                    parents[-1].append(item)
                    parents.append(item.children)
            # Open with the bookmarks panel showing, unless the file asks for another view.
            if pdf.Root.get("/PageMode", pikepdf.Name.UseNone) == pikepdf.Name.UseNone:
                pdf.Root.PageMode = pikepdf.Name.UseOutlines
        elif pdf.Root.get("/PageMode") == pikepdf.Name.UseOutlines:
            del pdf.Root.PageMode  # no bookmarks panel to show
        if usage_rights:  # Reader extensions break once the file changes; drop them
            del pdf.Root.Perms
        protection = inherit_protection(pdf, password)
        pdf.save(str(out), encryption=protection.encryption)
    return protection.report({"output": str(out), "entries": len(checked),
                              "replaced": replaced})
