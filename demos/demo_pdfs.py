"""Build the sample PDFs the demo scenarios operate on.

Kept deterministic and dependency-free beyond reportlab (already a runtime
dependency) so every recording starts from identical input.
"""
from __future__ import annotations

from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

INK = colors.HexColor("#1f2933")
MUTED = colors.HexColor("#616e7c")
ACCENT = colors.HexColor("#2f6fde")
RULE = colors.HexColor("#d9e2ec")

REPORT_PAGES = [
    ("Q3 Field Report", "Summary", [
        "Deliveries rose 18% quarter over quarter across all regions.",
        "Average route time fell from 41 to 36 minutes.",
        "Two new depots opened in Leeds and Porto.",
    ]),
    ("Regional Breakdown", "North, South, East, West", [
        "North: 12,400 deliveries, 98.1% on time.",
        "South: 10,950 deliveries, 97.4% on time.",
        "East: 8,210 deliveries, 96.9% on time.",
        "West: 9,775 deliveries, 98.6% on time.",
    ]),
    ("Next Quarter", "Priorities", [
        "Pilot electric vans on three urban routes.",
        "Cut failed first-attempt deliveries below 2%.",
        "Roll out live tracking to all customers.",
    ]),
]


def _bars(c: canvas.Canvas, x: float, y: float, values: list[int]) -> None:
    top = max(values)
    for i, v in enumerate(values):
        h = 120 * v / top
        c.setFillColor(ACCENT if i == len(values) - 1 else RULE)
        c.rect(x + i * 46, y, 30, h, stroke=0, fill=1)


def build_report(path: Path) -> Path:
    """A three-page, letter-size report with a heading, bullets and a chart."""
    c = canvas.Canvas(str(path), pagesize=letter)
    c.setTitle("Q3 Field Report")
    c.setAuthor("Paperboy Demo Co.")
    width, height = letter
    for n, (title, subtitle, bullets) in enumerate(REPORT_PAGES, start=1):
        c.setFillColor(ACCENT)
        c.rect(0, height - 14, width, 14, stroke=0, fill=1)
        c.setFillColor(INK)
        c.setFont("Helvetica-Bold", 30)
        c.drawString(72, height - 110, title)
        c.setFillColor(MUTED)
        c.setFont("Helvetica", 15)
        c.drawString(72, height - 138, subtitle)
        c.setStrokeColor(RULE)
        c.setLineWidth(1.5)
        c.line(72, height - 158, width - 72, height - 158)
        c.setFillColor(INK)
        c.setFont("Helvetica", 14)
        for i, line in enumerate(bullets):
            c.drawString(90, height - 200 - i * 28, f"•  {line}")
        _bars(c, 90, 180, [42, 55, 61, 58, 72][: 3 + n - 1] + [80])
        c.setFillColor(MUTED)
        c.setFont("Helvetica", 10)
        c.drawString(72, 48, "Paperboy Demo Co. · Internal")
        c.drawRightString(width - 72, 48, f"Page {n} of {len(REPORT_PAGES)}")
        c.showPage()
    c.save()
    return path


SCAN_PAGES = [
    ("Field notes", ["Route 14: two failed first attempts.", "Depot B: van 7 in for service."]),
    None,                                   # a page the scanner fed through empty
    ("Field notes (cont.)", ["Customer feedback: 4.6 / 5 average.", "Next audit: 3 Nov."]),
    "dust",                                 # an empty page with a few specks of dust
    ("Sign-off", ["Reviewed by the regional lead."]),
]


def build_scan(path: Path) -> Path:
    """A five-page 'scan' with a truly blank page 2 and a dusty but empty page 4."""
    import random

    c = canvas.Canvas(str(path), pagesize=letter)
    width, height = letter
    for page in SCAN_PAGES:
        c.setFillColor(colors.HexColor("#fbfaf6"))  # off-white scanner paper
        c.rect(0, 0, width, height, stroke=0, fill=1)
        if page == "dust":
            rnd = random.Random(7)
            c.setFillGray(0.35)
            for _ in range(20):
                c.circle(rnd.uniform(40, width - 40), rnd.uniform(40, height - 40), 0.7,
                         stroke=0, fill=1)
        elif page:
            title, lines = page
            c.setFillColor(INK)
            c.setFont("Helvetica-Bold", 24)
            c.drawString(72, height - 110, title)
            c.setFont("Helvetica", 14)
            for i, line in enumerate(lines):
                c.drawString(72, height - 160 - i * 26, line)
        c.showPage()
    c.save()
    return path


def build_delivery_form(path: Path) -> Path:
    """A one-page delivery request form: text fields, a checkbox, radio buttons, a drop-down."""
    c = canvas.Canvas(str(path), pagesize=letter)
    width, height = letter
    form = c.acroForm
    c.setFillColor(ACCENT)
    c.rect(0, height - 14, width, 14, stroke=0, fill=1)
    c.setFillColor(INK)
    c.setFont("Helvetica-Bold", 26)
    c.drawString(72, height - 100, "Delivery request")
    c.setFont("Helvetica", 12)
    c.setFillColor(MUTED)
    c.drawString(72, height - 124, "Paperboy Demo Co. \u00b7 please complete every field")

    def label(y, text):
        c.setFillColor(INK)
        c.setFont("Helvetica-Bold", 12)
        c.drawString(72, y + 6, text)

    common = {"x": 200, "width": 300, "height": 24, "fontSize": 12, "borderColor": RULE,
              "fillColor": colors.white, "textColor": INK, "borderWidth": 1}
    label(height - 190, "Full name")
    form.textfield(name="name", y=height - 194, **common)
    label(height - 240, "Address")
    form.textfield(name="address", y=height - 244, **common)
    label(height - 290, "Express")
    form.checkbox(name="express", x=200, y=height - 294, size=20, buttonStyle="check",
                  borderColor=RULE, fillColor=colors.white, textColor=ACCENT, checked=False)
    label(height - 340, "Parcel size")
    for i, size in enumerate(["small", "medium", "large"]):
        form.radio(name="size", value=size, x=200 + i * 100, y=height - 344, size=20,
                   borderColor=RULE, fillColor=colors.white, textColor=ACCENT, selected=False)
        c.setFillColor(MUTED)
        c.setFont("Helvetica", 11)
        c.drawString(226 + i * 100, height - 338, size)
    label(height - 390, "Depot")
    form.choice(name="depot", value="Choose...",
                options=["Choose...", "Leeds", "Porto", "Lyon"],
                **{**common, "y": height - 394, "width": 160})
    c.showPage()
    c.save()
    return path


def build_proposal(path: Path) -> Path:
    """A client proposal still carrying what it shouldn't: an author and software in
    its metadata, a script that runs on open, an internal attachment, a reviewer's
    comment, and an earlier saved version with a revealing title."""
    import re

    import pikepdf

    c = canvas.Canvas(str(path), pagesize=letter)
    c.setTitle("Delivery services proposal")
    c.setAuthor("Jane Smith (Sales)")
    c.setCreator("Microsoft Word for Mac")
    width, height = letter
    for heading, line in [("Delivery services proposal", "Prepared for Northwind Traders"),
                          ("Pricing", "Fixed monthly fee, see schedule A.")]:
        c.setFillColor(ACCENT)
        c.rect(0, height - 14, width, 14, stroke=0, fill=1)
        c.setFillColor(INK)
        c.setFont("Helvetica-Bold", 26)
        c.drawString(72, height - 110, heading)
        c.setFont("Helvetica", 14)
        c.setFillColor(MUTED)
        c.drawString(72, height - 140, line)
        c.showPage()
    c.save()
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        page = pdf.pages[0]
        pdf.Root.OpenAction = pdf.make_indirect(pikepdf.Dictionary(
            Type=pikepdf.Name.Action, S=pikepdf.Name.JavaScript,
            JS=pikepdf.String("app.alert('Tracking open');")))
        spreadsheet = pdf.make_indirect(pikepdf.Stream(pdf, b"cost,margin\n100,40%"))
        filespec = pdf.make_indirect(pikepdf.Dictionary(
            Type=pikepdf.Name.Filespec, F=pikepdf.String("pricing-internal.xlsx"),
            EF=pikepdf.Dictionary(F=spreadsheet)))
        pdf.Root.Names = pikepdf.Dictionary(EmbeddedFiles=pikepdf.Dictionary(
            Names=pikepdf.Array([pikepdf.String("pricing-internal.xlsx"), filespec])))
        page.obj.Annots = pdf.make_indirect(pikepdf.Array([pdf.make_indirect(
            pikepdf.Dictionary(Type=pikepdf.Name.Annot, Subtype=pikepdf.Name.Text,
                               Rect=pikepdf.Array([500, 700, 520, 720]),
                               T=pikepdf.String("Jane Smith"),
                               Contents=pikepdf.String("Don't mention the 40% margin")))]))
        pdf.save(path)
    # Make the first save's title revealing, then append an incremental update with
    # the final metadata: the revealing title stays in the file as an earlier version.
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        final = {k: v for k, v in pdf.trailer.Info.items()}
        pdf.trailer.Info = pdf.make_indirect(pikepdf.Dictionary(
            Title=pikepdf.String("Proposal v1 - internal margin 40%")))
        pdf.save(path, object_stream_mode=pikepdf.ObjectStreamMode.disable,
                 compress_streams=False)
    data = path.read_bytes()
    with pikepdf.open(path) as pdf:
        root, size = pdf.Root.objgen[0], int(pdf.trailer.Size)
        info = pikepdf.Dictionary(final).unparse()
    prev = int(re.findall(rb"startxref\s+(\d+)", data)[-1])
    body = b"\n%d 0 obj\n%s\nendobj\n" % (size, info)
    xref_at = len(data) + len(body)
    path.write_bytes(data + body + (
        b"xref\n%d 1\n%010d 00000 n \ntrailer\n<< /Size %d /Root %d 0 R /Info %d 0 R "
        b"/Prev %d >>\nstartxref\n%d\n%%%%EOF\n" % (size, len(data) + 1, size + 1, root, size,
                                                     prev, xref_at)))
    return path


def build_invoice(path: Path) -> Path:
    """An "invoice" from an unknown sender that does what phishing PDFs do: runs a
    script as soon as it opens, carries a program disguised as a PDF attachment, and
    links out to a look-alike payment site. The attachment is a harmless stub that
    only starts like a Windows program; nothing here can run."""
    import pikepdf

    c = canvas.Canvas(str(path), pagesize=letter)
    c.setTitle("Invoice 20931")
    width, height = letter
    c.setFillColor(ACCENT)
    c.rect(0, height - 14, width, 14, stroke=0, fill=1)
    c.setFillColor(INK)
    c.setFont("Helvetica-Bold", 26)
    c.drawString(72, height - 110, "Invoice #20931 - payment overdue")
    c.setFont("Helvetica", 14)
    c.setFillColor(MUTED)
    c.drawString(72, height - 140, "Open the attached statement to avoid a late fee.")
    c.setFillColor(ACCENT)
    c.drawString(72, height - 180, "Pay now: secure-billing-portal.example")
    c.showPage()
    c.save()
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        page = pdf.pages[0]
        pdf.Root.OpenAction = pdf.make_indirect(pikepdf.Dictionary(
            Type=pikepdf.Name.Action, S=pikepdf.Name.JavaScript,
            JS=pikepdf.String("this.exportDataObject({cName: 'statement.pdf.exe', nLaunch: 2});")))
        stub = pdf.make_indirect(pikepdf.Stream(pdf, b"MZ" + b"\0" * 62 + b"demo stub"))
        filespec = pdf.make_indirect(pikepdf.Dictionary(
            Type=pikepdf.Name.Filespec, F=pikepdf.String("statement.pdf.exe"),
            EF=pikepdf.Dictionary(F=stub)))
        pdf.Root.Names = pikepdf.Dictionary(EmbeddedFiles=pikepdf.Dictionary(
            Names=pikepdf.Array([pikepdf.String("statement.pdf.exe"), filespec])))
        page.obj.Annots = pdf.make_indirect(pikepdf.Array([pdf.make_indirect(
            pikepdf.Dictionary(Type=pikepdf.Name.Annot, Subtype=pikepdf.Name.Link,
                               Rect=pikepdf.Array([72, height - 186, 360, height - 166]),
                               Border=pikepdf.Array([0, 0, 0]),
                               A=pikepdf.Dictionary(
                                   S=pikepdf.Name.URI,
                                   URI=pikepdf.String("https://secure-billing-portal.example/pay"))))]))
        pdf.save(path)
    return path


def _photo(color: str, accent: str, size=(480, 320)) -> bytes:
    """A simple product "photo": a backdrop, a shape and a floor shadow."""
    import io

    from PIL import Image, ImageDraw

    img = Image.new("RGB", size, color)
    draw = ImageDraw.Draw(img)
    w, h = size
    draw.ellipse([w * 0.25, h * 0.78, w * 0.75, h * 0.9], fill="#c9d1db")
    draw.rounded_rectangle([w * 0.3, h * 0.2, w * 0.7, h * 0.8], radius=30, fill=accent)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=88)
    return buf.getvalue()


def build_brochure(path: Path) -> Path:
    """A two-page product brochure with two photos, a small logo, and two attached
    files: a spec sheet and a price list."""
    import io

    import pikepdf
    from reportlab.lib.utils import ImageReader

    c = canvas.Canvas(str(path), pagesize=letter)
    c.setTitle("Courier bags 2026")
    width, height = letter
    logo = io.BytesIO()
    from PIL import Image
    Image.new("RGB", (24, 24), "#2f6fde").save(logo, "PNG")
    for heading, line, photo in [
        ("Metro courier bag", "Waterproof, 28 L, reflective strap.", _photo("#eef2f7", "#2f6fde")),
        ("Cargo messenger", "Expands to 40 L for bulk rounds.", _photo("#f7f1ea", "#d9822b")),
    ]:
        c.setFillColor(ACCENT)
        c.rect(0, height - 14, width, 14, stroke=0, fill=1)
        c.drawImage(ImageReader(io.BytesIO(logo.getvalue())), width - 96, height - 70, 24, 24)
        c.setFillColor(INK)
        c.setFont("Helvetica-Bold", 26)
        c.drawString(72, height - 110, heading)
        c.setFont("Helvetica", 14)
        c.setFillColor(MUTED)
        c.drawString(72, height - 140, line)
        c.drawImage(ImageReader(io.BytesIO(photo)), 72, height - 480, 360, 240)
        c.showPage()
    c.save()
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        pdf.attachments["spec-sheet.csv"] = pikepdf.AttachedFileSpec(
            pdf, b"model,volume_l,weight_kg\nMetro,28,1.1\nCargo,40,1.6\n",
            description="Technical specifications", mime_type="text/csv")
        pdf.attachments["price-list.txt"] = pikepdf.AttachedFileSpec(
            pdf, b"Metro courier bag  EUR 129\nCargo messenger   EUR 159\n",
            description="2026 prices", mime_type="text/plain")
        pdf.save(path)
    return path


def _lease_page(c: canvas.Canvas, n: int, back: bool) -> None:
    """One side of a two-page-per-sheet lease, as an off-white scan."""
    width, height = letter
    c.setFillColor(colors.HexColor("#fbfaf6"))
    c.rect(0, 0, width, height, stroke=0, fill=1)
    c.setFillColor(INK)
    c.setFont("Helvetica-Bold", 22)
    c.drawString(72, height - 100, "Lease agreement")
    c.setFont("Helvetica", 14)
    c.setFillColor(MUTED)
    c.drawString(72, height - 130, f"Section {n}: " + ("terms continued" if back else "terms"))
    c.setFont("Helvetica-Bold", 60)
    c.setFillColor(ACCENT)
    c.drawCentredString(width / 2, height / 2, f"Page {n}")
    c.setFont("Helvetica", 11)
    c.setFillColor(MUTED)
    c.drawCentredString(width / 2, 40, ("back of sheet" if back else "front of sheet")
                        + f" · page {n} of 6")


def build_fronts(path: Path) -> Path:
    """The fronts of a 3-sheet lease scanned on a single-sided scanner: pages 1, 3, 5."""
    c = canvas.Canvas(str(path), pagesize=letter)
    for n in (1, 3, 5):
        _lease_page(c, n, back=False)
        c.showPage()
    c.save()
    return path


def build_backs(path: Path) -> Path:
    """The backs, scanned by turning the stack over: pages 6, 4, 2 (last first)."""
    c = canvas.Canvas(str(path), pagesize=letter)
    for n in (6, 4, 2):
        _lease_page(c, n, back=True)
        c.showPage()
    c.save()
    return path


def build_statement(path: Path) -> Path:
    """A monthly delivery statement: a ruled table of regions on page 1, and a
    table of charges on page 2."""
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import (
        PageBreak,
        Paragraph,
        SimpleDocTemplate,
        Spacer,
        Table,
        TableStyle,
    )

    styles = getSampleStyleSheet()
    style = TableStyle([("GRID", (0, 0), (-1, -1), 0.6, RULE),
                        ("BACKGROUND", (0, 0), (-1, 0), ACCENT),
                        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                        ("ALIGN", (1, 1), (-1, -1), "RIGHT"),
                        ("TOPPADDING", (0, 0), (-1, -1), 6),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), 6)])
    regions = Table([["Region", "Deliveries", "On time", "Failed"],
                     ["North", "12,400", "98.1%", "236"], ["South", "10,950", "97.4%", "285"],
                     ["East", "8,210", "96.9%", "254"], ["West", "9,775", "98.6%", "137"]],
                    colWidths=[120, 100, 90, 80])
    regions.setStyle(style)
    charges = Table([["Item", "Quantity", "Unit price", "Amount"],
                     ["Courier routes", "41,335", "EUR 2.10", "EUR 86,803.50"],
                     ["Failed-delivery returns", "912", "EUR 4.00", "EUR 3,648.00"],
                     ["Fuel surcharge", "1", "EUR 1,250.00", "EUR 1,250.00"]],
                    colWidths=[150, 80, 90, 110])
    charges.setStyle(style)
    SimpleDocTemplate(str(path), pagesize=letter, title="Delivery statement, September").build([
        Paragraph("Delivery statement, September", styles["Title"]), Spacer(1, 12),
        Paragraph("Deliveries by region", styles["Heading2"]), regions, PageBreak(),
        Paragraph("Charges", styles["Heading2"]), charges])
    return path


def _contract(path: Path, fee: str, extra_page: bool, stamp_x: float) -> Path:
    """A three-page service agreement; versions differ in the fee, an added schedule
    and where the approval stamp sits."""
    c = canvas.Canvas(str(path), pagesize=letter)
    width, height = letter
    pages = [("Service agreement", ["Between Paperboy Demo Co. and Northwind Traders.",
                                    "Effective 1 November 2026."]),
             ("Fees", [f"Monthly fee: {fee}.", "Invoices are due within 30 days."]),
             ("Termination", ["Either party may end this agreement with 60 days' notice."])]
    if extra_page:
        pages.insert(2, ("Schedule B", ["Weekend deliveries are charged at 1.5x."]))
    for n, (title, lines) in enumerate(pages):
        c.setFillColor(ACCENT)
        c.rect(0, height - 14, width, 14, stroke=0, fill=1)
        c.setFillColor(INK)
        c.setFont("Helvetica-Bold", 24)
        c.drawString(72, height - 110, title)
        c.setFont("Helvetica", 14)
        c.setFillColor(MUTED)
        for k, line in enumerate(lines):
            c.drawString(72, height - 150 - 24 * k, line)
        if n == 0:  # an approval stamp
            c.setStrokeColor(colors.HexColor("#c0392b"))
            c.setLineWidth(3)
            c.roundRect(stamp_x, height - 330, 150, 60, 8)
            c.setFillColor(colors.HexColor("#c0392b"))
            c.setFont("Helvetica-Bold", 18)
            c.drawCentredString(stamp_x + 75, height - 307, "APPROVED")
        c.showPage()
    c.save()
    return path


def build_contract_v1(path: Path) -> Path:
    return _contract(path, "EUR 4,800", extra_page=False, stamp_x=72)


def build_contract_v2(path: Path) -> Path:
    return _contract(path, "EUR 5,250", extra_page=True, stamp_x=360)


def build_taxes(path: Path) -> Path:
    """A password-protected copy of the report, opened with the password Spring2026!."""
    import pikepdf

    plain = build_report(path.with_name(".plain-" + path.name))
    with pikepdf.open(plain) as pdf:
        pdf.save(path, encryption=pikepdf.Encryption(user="Spring2026!", owner="Spring2026!",
                                                     R=6))
    plain.unlink()
    return path


def build_album(path: Path) -> Path:
    """Three pages of large, finely detailed 'photos' (about 6 MB): too big to email."""
    import io
    import random

    from PIL import Image
    from reportlab.lib.utils import ImageReader

    c = canvas.Canvas(str(path), pagesize=letter)
    width, height = letter
    captions = ["Harbour at dawn", "Old town market", "Hills after rain"]
    for n, caption in enumerate(captions):
        rnd = random.Random(n)
        fractal = Image.effect_mandelbrot((2400, 1600), (-2.2 + n * 0.3, -1.2, 1.0, 1.2), 80)
        tint = Image.new("RGB", fractal.size, (rnd.randrange(80, 200), rnd.randrange(80, 200),
                                               rnd.randrange(80, 200)))
        photo = Image.composite(tint, Image.new("RGB", fractal.size, "#14213d"), fractal)
        grain = Image.frombytes("RGB", fractal.size, rnd.randbytes(2400 * 1600 * 3))
        photo = Image.blend(photo, grain, 0.12)
        buf = io.BytesIO()
        photo.save(buf, "JPEG", quality=92)
        c.drawImage(ImageReader(io.BytesIO(buf.getvalue())), 72, height - 72 - 312,
                    width=width - 144, height=312)
        c.setFillColor(INK)
        c.setFont("Helvetica", 14)
        c.drawString(72, height - 72 - 340, caption)
        c.showPage()
    c.save()
    return path


def build_case_file(path: Path) -> Path:
    """A customer complaint file that names the customer, with their email, phone
    number and a scanned signature: to be redacted before it goes to a supplier."""
    import io
    import math

    from PIL import Image, ImageDraw
    from reportlab.lib.utils import ImageReader

    c = canvas.Canvas(str(path), pagesize=letter)
    c.setTitle("Case 2291 - Maria Gonzalez")
    width, height = letter
    c.setFillColor(ACCENT)
    c.rect(0, height - 14, width, 14, stroke=0, fill=1)
    c.setFillColor(INK)
    c.setFont("Helvetica-Bold", 24)
    c.drawString(72, height - 100, "Complaint case 2291")
    c.setFont("Helvetica", 12)
    lines = [
        "Customer: Maria Gonzalez",
        "Email: maria.gonzalez@example.com    Phone: (555) 014-2290",
        "",
        "On 14 October, Maria Gonzalez reported that parcel NW-48213 arrived",
        "with a crushed corner and a damaged lamp inside. The courier left it",
        "at the door without a signature. Please review the depot handling",
        "and confirm whether the supplier will replace the lamp.",
    ]
    for n, line in enumerate(lines):
        c.drawString(72, height - 150 - n * 20, line)
    pad = Image.new("L", (600, 160), 255)  # a pen signature, scanned
    draw = ImageDraw.Draw(pad)
    points = [(40 + x * 5, 90 + 40 * math.sin(x / 6) * math.cos(x / 17)) for x in range(105)]
    draw.line(points, fill=20, width=5)
    buf = io.BytesIO()
    pad.save(buf, "PNG")
    c.drawImage(ImageReader(io.BytesIO(buf.getvalue())), 72, height - 410, width=225,
                height=60)
    c.setFillColor(MUTED)
    c.drawString(72, height - 425, "Customer signature")
    c.showPage()
    c.save()
    return path


LETTER_PAGES = [
    ("Northwind Insurance", [
        "12 October 2026",
        "",
        "Dear Mr Okafor,",
        "",
        "Thank you for your claim about the water damage at",
        "14 Harbour Lane. We have opened a file for it.",
        "",
        "Policy number: NW-7731-0442",
        "Claim reference: CL-20931",
        "",
        "An assessor will call you within five working days",
        "to arrange a visit. Please keep any receipts for",
        "emergency repairs; we will need copies of them.",
    ]),
    ("Northwind Insurance", [
        "What happens next",
        "",
        "1. The assessor visits and photographs the damage.",
        "2. We send you a settlement offer within 15 days.",
        "3. Once you accept, payment follows in 3 days.",
        "",
        "Questions? Call us on 0800 555 0199 and quote",
        "your claim reference.",
        "",
        "Yours sincerely,",
        "Claims Team",
    ]),
]


def build_scanned_letter(path: Path) -> Path:
    """A two-page letter as a scanner makes it: each page a slightly grey, slightly
    crooked JPEG picture, with no text to search."""
    import io
    import random

    import pypdfium2 as pdfium
    from PIL import Image, ImageFilter
    from reportlab.lib.utils import ImageReader

    width, height = letter
    typed = io.BytesIO()
    t = canvas.Canvas(typed, pagesize=letter)
    for title, lines in LETTER_PAGES:
        t.setFillColor(INK)
        t.setFont("Helvetica-Bold", 22)
        t.drawString(72, height - 100, title)
        t.setFont("Times-Roman", 14)
        for i, line in enumerate(lines):
            t.drawString(72, height - 160 - i * 24, line)
        t.showPage()
    t.save()
    doc = pdfium.PdfDocument(typed.getvalue())
    rnd = random.Random(3)
    c = canvas.Canvas(str(path), pagesize=letter)
    for n in range(len(LETTER_PAGES)):
        page = doc[n].render(scale=150 / 72, grayscale=True).to_pil()
        page = page.rotate(0.6 if n == 0 else -0.4, fillcolor=255, resample=Image.BICUBIC)
        page = page.filter(ImageFilter.GaussianBlur(0.6))
        page = page.point(lambda v: min(255, int(v * 0.93) + 12))  # grey scanner paper
        for _ in range(60):  # specks of dust
            x, y = rnd.randrange(page.width), rnd.randrange(page.height)
            page.putpixel((x, y), 90)
        buf = io.BytesIO()
        page.save(buf, "JPEG", quality=80)
        c.drawImage(ImageReader(io.BytesIO(buf.getvalue())), 0, 0, width, height)
        c.showPage()
    doc.close()
    c.save()
    return path


BUILDERS = {"report.pdf": build_report, "scan.pdf": build_scan,
            "delivery-form.pdf": build_delivery_form, "proposal.pdf": build_proposal,
            "invoice.pdf": build_invoice, "brochure.pdf": build_brochure,
            "fronts.pdf": build_fronts, "backs.pdf": build_backs,
            "statement.pdf": build_statement, "contract-v1.pdf": build_contract_v1,
            "contract-v2.pdf": build_contract_v2, "taxes.pdf": build_taxes,
            "album.pdf": build_album, "case-file.pdf": build_case_file,
            "scanned-letter.pdf": build_scanned_letter}


def build(names: list[str], workspace: Path) -> None:
    """Write each named sample file into *workspace*."""
    for name in names:
        BUILDERS[name](workspace / name)
