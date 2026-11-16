"""markdown_to_pdf: Markdown, written by the assistant or read from a file, as a PDF.

markdown-it-py parses it (CommonMark, plus tables and strikethrough); ReportLab lays it
out. Raw HTML is never interpreted: with ``html`` off, the parser hands it over as
text. Links are clickable only for http(s), mailto and headings in the document, so a
Markdown file can't plant a script or a file link. Pictures are used only from files on
this computer inside the allowed root, decoded and re-encoded (never passed through),
and scaled down; remote ones are never fetched. Text that fits the built-in fonts
(Latin) uses them; anything else switches the whole document to the bundled DejaVu
fonts, and characters those lack are replaced by "?" and reported.
"""
from __future__ import annotations

import html
import io
import re
import time
import unicodedata
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from urllib.parse import unquote

from ..config import Settings
from ..errors import PdfUserError
from ..paths import resolve
from ..results import MarkdownPdfResult

MAX_CHARS = 1_000_000          # Markdown one call may lay out
MAX_IMAGES = 100               # pictures one document may embed
MAX_COLUMNS = 30               # table columns
MAX_NESTING = 40               # parser levels: a quote takes one, a list two
MAX_TABLE_ROWS = 200           # rows per laid-out table; longer ones continue in another
MAX_LABEL = 200                # characters of a heading kept for its bookmark
MAX_IMAGE_SIDE = 2400          # px: enough for a full-width picture at ~300 dpi
BUILD_TIME_LIMIT_S = 120
MAX_REPORTED = 50              # missing characters / skipped pictures listed
FONTS = Path(__file__).resolve().parent.parent / "fonts"
_SCHEME = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")
_LINKS = ("http://", "https://", "mailto:")


def _page_size(name: str):
    from reportlab.lib.pagesizes import A4, LETTER

    sizes = {"letter": LETTER, "a4": A4}
    if name not in sizes:
        raise PdfUserError("page_size must be \"letter\" or \"a4\"")
    return sizes[name]


@cache
def _dejavu() -> tuple[frozenset, frozenset]:
    """Register the bundled fonts (once); the characters the text and code fonts have."""
    from reportlab.lib.fonts import addMapping
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    faces = {"PaperboySans": "DejaVuSans.ttf", "PaperboySans-Bold": "DejaVuSans-Bold.ttf",
             "PaperboySans-Oblique": "DejaVuSans-Oblique.ttf",
             "PaperboyMono": "DejaVuSansMono.ttf"}
    fonts = {}
    for name, file in faces.items():
        fonts[name] = TTFont(name, str(FONTS / file))
        pdfmetrics.registerFont(fonts[name])
    # <b>, <i> and both inside paragraphs pick these; bold italic is drawn bold.
    for bold, italic, face in ((0, 0, "PaperboySans"), (1, 0, "PaperboySans-Bold"),
                               (0, 1, "PaperboySans-Oblique"), (1, 1, "PaperboySans-Bold")):
        addMapping("PaperboySans", bold, italic, face)
    for bold in (0, 1):
        for italic in (0, 1):
            addMapping("PaperboyMono", bold, italic, "PaperboyMono")
    return (frozenset(fonts["PaperboySans"].face.charToGlyph),
            frozenset(fonts["PaperboyMono"].face.charToGlyph))


@dataclass
class _Fonts:
    body: str = "Helvetica"
    bold: str = "Helvetica-Bold"
    mono: str = "Courier"
    unicode: bool = False
    text_chars: frozenset | None = None   # None: the built-in fonts, Latin only
    code_chars: frozenset | None = None
    missing: dict = field(default_factory=dict)

    def clean(self, text: str, code: bool = False) -> str:
        """Text as it can be drawn: control characters dropped, characters the font
        lacks replaced by "?" (and noted)."""
        have = self.code_chars if code else self.text_chars
        out = []
        for ch in text:
            if ch in "\n\t":
                out.append(ch)
            elif unicodedata.category(ch) in ("Cc", "Cf", "Cs", "Co", "Cn"):
                continue
            elif have is not None and ord(ch) not in have and not ch.isspace():
                self.missing.setdefault(ch, None)
                out.append("?")
            else:
                out.append(ch)
        return "".join(out)


def _fonts_for(tree, *extra: str | None) -> _Fonts:
    """The fonts for the text as it will be drawn: taken from the parsed document, where
    entities like "&#x4e2d;" are already characters."""
    parts = [node.content for node in tree.walk()
             if node.type in ("text", "code_inline", "fence", "code_block")]
    text = "".join(parts) + "".join(e for e in extra if e)
    printable = "".join(ch for ch in text if unicodedata.category(ch)[0] != "C")
    try:
        printable.encode("cp1252")
        return _Fonts()
    except UnicodeEncodeError:
        text_chars, code_chars = _dejavu()
        return _Fonts("PaperboySans", "PaperboySans-Bold", "PaperboyMono", True,
                      text_chars, code_chars)


def _slug(text: str, taken: set, counts: dict) -> str:
    """A heading's anchor, as GitHub makes them: "#my-heading", "-1" etc. if repeated.
    *counts* remembers each base's next number, so many repeats stay linear."""
    base = re.sub(r"[^\w\- ]", "", text.strip().lower()).replace(" ", "-") or "section"
    n = counts.get(base, 0)
    slug = base if n == 0 else f"{base}-{n}"
    while slug in taken:
        n += 1
        slug = f"{base}-{n}"
    counts[base] = n + 1
    taken.add(slug)
    return slug


def _plain(node) -> str:
    """The text of an inline node, without markup."""
    if node.type in ("text", "code_inline"):
        return node.content
    if node.type in ("softbreak", "hardbreak"):
        return " "
    return "".join(_plain(child) for child in node.children)


class _Writer:
    """Turns the parsed Markdown into ReportLab flowables."""

    def __init__(self, settings: Settings, fonts: _Fonts, base: Path | None, width: float,
                 height: float):
        from reportlab.lib import colors
        from reportlab.lib.styles import ParagraphStyle

        self.settings, self.fonts, self.base = settings, fonts, base
        self.width, self.height = width, height
        self.anchors: set = set()
        self.images, self.skipped, self.headings, self.title = 0, [], 0, None
        self.pixels = self.encoded = 0
        self.outline: list = []   # (heading level, outline depth) of the open entries
        self.Quote, self.Code, self.Heading = _flowables()
        f = fonts
        body = ParagraphStyle("body", fontName=f.body, fontSize=10.5, leading=14.5,
                              spaceAfter=7, splitLongWords=1)
        self.styles = {
            "body": body,
            "quote": ParagraphStyle("quote", parent=body, textColor=colors.HexColor("#4b5563"),
                                    spaceBefore=4),
            "cell": ParagraphStyle("cell", parent=body, fontSize=9.5, leading=12,
                                   spaceAfter=0),
            "head": ParagraphStyle("head", parent=body, fontName=f.bold, fontSize=9.5,
                                   leading=12, spaceAfter=0),
            "code": ParagraphStyle("code", parent=body, fontName=f.mono, fontSize=8.8,
                                   leading=11.5, leftIndent=8, spaceBefore=6, spaceAfter=12,
                                   textColor=colors.HexColor("#1f2937")),
        }
        for level, size in enumerate((22, 17, 14, 12, 11, 10.5), 1):
            self.styles[f"h{level}"] = ParagraphStyle(
                f"h{level}", parent=body, fontName=f.bold, fontSize=size,
                leading=size * 1.25, spaceBefore=size * (0.9 if level > 1 else 0.3),
                spaceAfter=size * 0.45, keepWithNext=1)
            # A heading too long to keep with what follows (it could never fit).
            self.styles[f"h{level}-long"] = ParagraphStyle(
                f"h{level}-long", parent=self.styles[f"h{level}"], keepWithNext=0)

    # --- inline ---------------------------------------------------------------------

    def inline(self, node) -> str:
        """An inline node as ReportLab paragraph markup, every bit of text escaped."""
        t = node.type
        if t == "text":
            return html.escape(self.fonts.clean(node.content), quote=False)
        if t == "softbreak":
            return " "
        if t == "hardbreak":
            return "<br/>"
        if t == "code_inline":
            code = html.escape(self.fonts.clean(node.content, code=True), quote=False)
            return f'<font face="{self.fonts.mono}" backColor="#eef0f3">{code}</font>'
        inner = "".join(self.inline(child) for child in node.children)
        if t == "strong":
            return f"<b>{inner}</b>"
        if t == "em":
            return f"<i>{inner}</i>"
        if t == "s":
            return f"<strike>{inner}</strike>"
        if t == "link":
            href = node.attrs.get("href", "")
            if href.startswith("#") and unquote(href[1:]) in self.anchors:
                target = "#" + unquote(href[1:])
            elif href.lower().startswith(_LINKS):
                target = href
            else:  # other schemes (javascript:, file:) and unknown anchors aren't links
                return inner
            target = html.escape(target, quote=True)
            return f'<a href="{target}" color="#1d4ed8"><u>{inner}</u></a>'
        if t == "image":  # in running text: its description stands in
            return f"<i>[{html.escape(self.fonts.clean(_plain(node)), quote=False)}]</i>"
        return inner

    # --- blocks ---------------------------------------------------------------------

    def blocks(self, nodes, quote: bool = False, inset: float = 0) -> list:
        """*inset*: how far lists and quotes around these blocks indent them."""
        out = []
        for node in nodes:
            out.extend(self.block(node, quote, inset))
        return out

    def indent(self, inset: float, want: float) -> float:
        """An indent for one more list or quote: none once they've taken 60% of the
        page's width, so deep nesting never runs off the page."""
        return want if inset + want <= self.width * 0.6 else 0

    def block(self, node, quote: bool, inset: float) -> list:
        from reportlab.lib import colors
        from reportlab.platypus import HRFlowable, Indenter

        t = node.type
        if t == "heading":
            return [self.heading(node)]
        if t == "paragraph":
            return self.paragraph(node, "quote" if quote else "body", inset)
        if t in ("bullet_list", "ordered_list"):
            return [self.listing(node, quote, inset)]
        if t == "blockquote":
            step = self.indent(inset, 16)
            return [Indenter(left=step), *self.blocks(node.children, True, inset + step),
                    Indenter(left=-step)]
        if t in ("fence", "code_block"):
            text = self.fonts.clean(node.content.rstrip("\n"), code=True).expandtabs(4)
            style = self.styles["code"]
            from reportlab.pdfbase.pdfmetrics import stringWidth

            per_line = max(20, int((self.width - inset - style.leftIndent)
                                   // stringWidth("M", style.fontName, style.fontSize)))
            return [self.Code(text, style, maxLineLength=per_line, newLineChars="")]
        if t == "table":
            return self.table(node, inset)
        if t == "hr":
            return [HRFlowable(width="100%", thickness=0.8, color=colors.HexColor("#d1d5db"),
                               spaceBefore=6, spaceAfter=10)]
        return self.blocks(node.children, quote, inset)

    def heading(self, node):
        level = int(node.tag[1])
        text = " ".join(self.fonts.clean(_plain(node.children[0])).split())
        if self.title is None and text:
            self.title = text[:MAX_LABEL]
        style = f"h{level}" if len(text) <= MAX_LABEL else f"h{level}-long"
        para = self.Heading(self.inline(node.children[0]), self.styles[style])
        para._paperboy_mark = (self, level, text[:MAX_LABEL] or "Untitled",
                               node.meta["slug"])
        self.headings += 1
        return para

    def mark(self, canv, level: int, label: str, key: str) -> None:
        """Where a heading is drawn: its anchor, and its bookmark under the nearest
        heading of a higher level (one level deeper at most)."""
        while self.outline and self.outline[-1][0] >= level:
            self.outline.pop()
        depth = len(self.outline)
        self.outline.append((level, depth))
        canv.bookmarkPage(key)
        canv.addOutlineEntry(label, key, level=depth, closed=depth > 0)
        canv.showOutline()  # open with the bookmarks showing

    def paragraph(self, node, style: str, inset: float) -> list:
        """A paragraph; pictures in it become pictures of their own, between its text."""
        from reportlab.platypus import Paragraph

        out, run = [], []

        def flush():
            markup = "".join(run).strip()
            if markup and markup != "<br/>":
                kind = self.Quote if style == "quote" else Paragraph
                out.append(kind(markup, self.styles[style]))
            run.clear()

        for child in node.children[0].children if node.children else []:
            if child.type == "image":
                picture = self.image(child, inset)
                if picture is not None:
                    flush()
                    out.append(picture)
                    continue
            run.append(self.inline(child))
        flush()
        return out

    def listing(self, node, quote: bool, inset: float):
        from reportlab.platypus import ListFlowable, ListItem

        ordered = node.type == "ordered_list"
        step = self.indent(inset, 18)
        items = []
        for item in node.children:
            content = self.blocks(item.children, quote, inset + step)
            if content:
                items.append(ListItem(content))
        kwargs = {"bulletFontName": self.fonts.body, "bulletFontSize": 10.5,
                  "leftIndent": step}
        if ordered:
            kwargs.update(bulletType="1", start=str(node.attrs.get("start", 1)),
                          bulletFormat="%s.")
        else:
            kwargs.update(bulletType="bullet", start="\u2022")
        return ListFlowable(items, **kwargs)

    def table(self, node, inset: float) -> list:
        """A table, laid out as consecutive tables of up to MAX_TABLE_ROWS rows, each
        with the header: ReportLab's time to split a table grows with its rows squared."""
        from reportlab.lib import colors
        from reportlab.lib.styles import ParagraphStyle
        from reportlab.platypus import Paragraph, Table, TableStyle

        rows, header = [], 0       # each cell as (markup, style)
        for section in node.children:
            for tr in section.children:
                cells = []
                for cell in tr.children:
                    style = self.styles["head" if cell.type == "th" else "cell"]
                    align = (cell.attrs.get("style") or "").replace("text-align:", "")
                    if align in ("center", "right"):
                        style = ParagraphStyle(f"{style.name}-{align}", parent=style,
                                               alignment=1 if align == "center" else 2)
                    markup = "".join(self.inline(c) for c in cell.children[0].children) \
                        if cell.children else ""
                    cells.append((markup, style))
                rows.append(cells)
            if section.type == "thead":
                header = len(rows)
        columns = max(len(r) for r in rows)
        if columns > MAX_COLUMNS:
            raise PdfUserError(f"A table has {columns} columns; at most {MAX_COLUMNS} fit")
        for r in rows:  # GFM pads short rows, but be sure
            r.extend(("", self.styles["cell"]) for _ in range(columns - len(r)))
        # Widths in proportion to each column's longest text, within limits.
        longest = [max(min(len(r[i][0]), 60) for r in rows) + 4 for i in range(columns)]
        total = sum(longest)
        widths = [(self.width - inset) * n / total for n in longest]
        style = TableStyle([
            ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#d1d5db")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            *([("BACKGROUND", (0, 0), (-1, header - 1), colors.HexColor("#f3f4f6"))]
              if header else []),
        ])
        body = rows[header:] or [[]]
        tables = []
        for start in range(0, len(body), MAX_TABLE_ROWS):
            part = [r for r in rows[:header] + body[start:start + MAX_TABLE_ROWS] if r]
            table = Table([[Paragraph(m, st) for m, st in r] for r in part], colWidths=widths,
                          repeatRows=header, hAlign="LEFT")
            table.setStyle(style)
            tables.append(table)
        tables[-1].spaceAfter = 9
        return tables

    def image(self, node, inset: float = 0):
        """A picture from a file on this computer, fitted to the page; None (and noted)
        if it can't be used."""
        from PIL import Image
        from reportlab.platypus import Image as Picture

        from .create import IMAGE_EXTS, IMAGE_FORMATS, MAX_TOTAL_PIXELS, _open_image

        src = node.attrs.get("src", "")
        shown = src if len(src) <= 200 else src[:200] + "..."

        def skip(reason: str):
            if len(self.skipped) < MAX_REPORTED:
                self.skipped.append({"source": self.fonts.clean(shown), "reason": reason})
            return None

        if self.images >= MAX_IMAGES:
            return skip(f"over {MAX_IMAGES} pictures in one document")
        if _SCHEME.match(src):  # the parser already refuses file: and javascript:
            return skip("only pictures on this computer are used; links aren't fetched")
        path = unquote(src)
        if "\x00" in path:
            return skip("not found")
        path = Path(path)
        if not path.is_absolute():
            if self.base is None:
                return skip("a relative path needs input_path or PAPERBOY_ROOT to start from")
            path = self.base / path
        try:
            path = resolve(str(path), self.settings)
            if not path.is_file():
                return skip("not found")
            if path.stat().st_size > self.settings.max_file_bytes:
                return skip("over the file-size limit")
            if path.suffix.lower() not in IMAGE_EXTS:
                return skip("not a picture file")
            with Image.open(path, formats=IMAGE_FORMATS) as probe:  # the header only
                if self.pixels + probe.width * probe.height > MAX_TOTAL_PIXELS:
                    return skip("the pictures add up to too many pixels")
            img = _open_image(path)
        except (PdfUserError, OSError, ValueError, Image.DecompressionBombError) as exc:
            reason = str(exc) if isinstance(exc, PdfUserError) else "not a readable picture"
            return skip(reason)
        try:
            pixels = img.width * img.height  # what was decoded, not what the header said
            if self.pixels + pixels > MAX_TOTAL_PIXELS:
                return skip("the pictures add up to too many pixels")
            self.pixels += pixels
            img.thumbnail((MAX_IMAGE_SIDE, MAX_IMAGE_SIDE))
            alpha = img.mode in ("RGBA", "LA", "PA") or "transparency" in img.info
            img = img.convert("RGBA" if alpha else "RGB")
            data = io.BytesIO()
            img.save(data, "PNG" if alpha else "JPEG", quality=90)
            w, h = img.size
        finally:
            img.close()
        if self.encoded + data.tell() > self.settings.max_file_bytes:
            return skip("the pictures add up to more than the file-size limit")
        self.encoded += data.tell()
        # Drawn at 96 dpi, or smaller to fit the page (inside any list or quote).
        scale = min(0.75, (self.width - inset) / w, self.height * 0.85 / h)
        data.seek(0)
        picture = Picture(data, width=w * scale, height=h * scale)
        picture.hAlign = "LEFT"
        picture.spaceAfter = 8
        self.images += 1
        return picture


def _flowables():
    """Paragraphs that draw a quote's bar, and code with a shaded background; both keep
    it on every page they're split across."""
    from reportlab.lib import colors
    from reportlab.platypus import Paragraph, Preformatted

    class Quote(Paragraph):
        def draw(self):
            self.canv.saveState()
            self.canv.setStrokeColor(colors.HexColor("#cbd5e1"))
            self.canv.setLineWidth(2.5)
            bottom = -self.style.spaceAfter if self.style.spaceAfter else 0
            self.canv.line(-10, bottom, -10, self.height)
            self.canv.restoreState()
            super().draw()

    class Code(Preformatted):
        def wrap(self, avail_width, avail_height):
            self._paperboy_width = avail_width
            return super().wrap(avail_width, avail_height)

        def split(self, avail_width, avail_height):
            parts = super().split(avail_width, avail_height)
            for part in parts:
                part.__class__ = Code
            return parts

        def draw(self):
            self.canv.saveState()
            self.canv.setFillColor(colors.HexColor("#f3f4f6"))
            self.canv.rect(0, -4, getattr(self, "_paperboy_width", self.width), self.height + 2,
                           stroke=0, fill=1)
            self.canv.restoreState()
            super().draw()

    class Heading(Paragraph):
        """Marks its anchor and bookmark where it is drawn: at the top level, in a list,
        or (for the first part) split across pages."""
        _paperboy_mark = None

        def split(self, avail_width, avail_height):
            parts = super().split(avail_width, avail_height)
            if parts:
                parts[0]._paperboy_mark = self._paperboy_mark
            return parts

        def draw(self):
            if self._paperboy_mark:
                writer, level, label, key = self._paperboy_mark
                writer.mark(self.canv, level, label, key)
                self._paperboy_mark = None
            super().draw()

    return Quote, Code, Heading


def _parse(text: str):
    """The Markdown's syntax tree. Past its nesting limit markdown-it silently drops what
    is nested deeper, so a document that parses differently with one more level allowed
    is refused rather than laid out with text missing."""
    from markdown_it import MarkdownIt
    from markdown_it.tree import SyntaxTreeNode

    def tokens(nesting: int):
        parser = MarkdownIt("commonmark", {"html": False, "maxNesting": nesting})
        return parser.enable(["table", "strikethrough"]).parse(text)

    def shape(stream) -> list:
        return [(t.type, t.content, len(t.children or ())) for t in stream]

    allowed = tokens(MAX_NESTING)
    if shape(allowed) != shape(tokens(MAX_NESTING + 1)):
        raise PdfUserError("The Markdown nests lists or quotes too deeply (about 20 lists, "
                           "or 40 quotes, inside each other); flatten it")
    return SyntaxTreeNode(allowed)


def _annotate(tree, taken: set) -> None:
    """Give every heading its anchor before laying out, so links to later headings work."""
    counts: dict = {}
    for node in tree.walk():
        if node.type == "heading":
            node.meta["slug"] = _slug(_plain(node.children[0]), taken, counts)


def markdown_to_pdf(text: str, out: Path, settings: Settings, base: Path | None = None,
                    title: str | None = None, author: str | None = None,
                    page_size: str = "letter") -> MarkdownPdfResult:
    """Lay out *text* as a PDF at *out* (written there through a private folder)."""
    from reportlab.platypus import SimpleDocTemplate
    from reportlab.platypus.doctemplate import LayoutError

    from .create import _made

    if len(text) > MAX_CHARS:
        raise PdfUserError(f"The Markdown is {len(text):,} characters, over the "
                           f"{MAX_CHARS:,} limit")
    size = _page_size(page_size)
    # NUL would become U+FFFD in the parser, after the fonts were chosen.
    text = text.lstrip("\ufeff").replace("\x00", "")
    tree = _parse(text)
    fonts = _fonts_for(tree, title, author)
    margin = 54
    pad = 12   # ReportLab's frames keep 6 pt clear on each side
    writer = _Writer(settings, fonts, base, size[0] - 2 * margin - pad,
                     size[1] - 2 * margin - pad)
    _annotate(tree, writer.anchors)
    story = writer.blocks(tree.children)
    given = " ".join(fonts.clean(title).split())[:500] if title else ""
    doc_title = given or writer.title

    def make(made: Path) -> None:
        deadline = time.monotonic() + BUILD_TIME_LIMIT_S

        class Doc(SimpleDocTemplate):
            def handle_flowable(self, flowables):
                # Each turn of the layout loop, including retries that make no progress.
                if time.monotonic() > deadline:
                    raise PdfUserError(f"Laying out the PDF took over {BUILD_TIME_LIMIT_S} s")
                super().handle_flowable(flowables)

            def afterPage(self):
                if self.page >= settings.max_pages:
                    raise PdfUserError(f"The PDF would have more than {settings.max_pages} "
                                       "pages, over the page limit")

        doc = Doc(str(made), pagesize=size, leftMargin=margin, rightMargin=margin,
                  topMargin=margin, bottomMargin=margin, title=doc_title or "",
                  author=" ".join(fonts.clean(author).split())[:500] if author else "",
                  creator="Paperboy")
        try:
            doc.build(story or [_empty(writer)])
        except LayoutError:
            raise PdfUserError("Something in the Markdown is too tall to fit on a page "
                               "(a table row or a picture); split it up") from None

    count = _made(out, settings, make)
    return {"output": str(out), "pages": count, "title": doc_title or None,
            "headings": writer.headings, "images": writer.images,
            "images_skipped": writer.skipped, "unicode_font": fonts.unicode,
            "missing_characters": list(fonts.missing)[:MAX_REPORTED]}


def _empty(writer: _Writer):
    from reportlab.platypus import Spacer

    return Spacer(writer.width, 1)
