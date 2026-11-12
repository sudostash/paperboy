# Paperboy tutorials

Each guide below shows one job you can hand to your AI assistant once Paperboy is
connected. You don't call tools yourself: you ask in plain language, and the assistant
picks the tools. The example requests are the ones used in the demo videos, so they
are known to work.

**Not set up yet?** Follow the [Quickstart](../README.md#quickstart): install Paperboy,
connect it to your AI app with one command, and restart the app.

## Contents

- [Before you start](#before-you-start)
- [Watching the videos](#watching-the-videos)
- Getting started: [See which PDFs you have](#see-which-pdfs-you-have) ·
  [What runs without asking](#what-runs-without-asking) ·
  [Typed, predictable results](#typed-predictable-results)
- Reading and finding: [Look at pages and save them as images](#look-at-pages-and-save-them-as-images) ·
  [Search a PDF](#search-a-pdf) · [Read a PDF as Markdown](#read-a-pdf-as-markdown) ·
  [Get tables out](#get-tables-out) ·
  [Get attachments and pictures out](#get-attachments-and-pictures-out) ·
  [Make a scan searchable](#make-a-scan-searchable) ·
  [Compare two versions](#compare-two-versions)
- Organizing pages: [Merge and reorder documents](#merge-and-reorder-documents) ·
  [Pull out a page and trim it](#pull-out-a-page-and-trim-it) ·
  [Remove blank pages](#remove-blank-pages) ·
  [Combine double-sided scans](#combine-double-sided-scans) ·
  [Resize pages and print 2 per sheet](#resize-pages-and-print-2-per-sheet) ·
  [Add bookmarks](#add-bookmarks)
- Creating PDFs: [Turn photos and notes into one PDF](#turn-photos-and-notes-into-one-pdf)
- Editing and marking up: [Fill in forms](#fill-in-forms) ·
  [Watermarks, stamps and page numbers](#watermarks-stamps-and-page-numbers) ·
  [Review with comments](#review-with-comments) ·
  [Set the title and author](#set-the-title-and-author) ·
  [Several steps in one go](#several-steps-in-one-go)
- Sharing safely: [Remove hidden data](#remove-hidden-data) ·
  [Redact for real](#redact-for-real) · [Lock a finished form](#lock-a-finished-form) ·
  [Type passwords yourself](#type-passwords-yourself) ·
  [When a PDF says no](#when-a-pdf-says-no) ·
  [Check a PDF for malicious content](#check-a-pdf-for-malicious-content)
- File health and size: [Fix a PDF that won't open](#fix-a-pdf-that-wont-open) ·
  [Shrink a PDF to a size](#shrink-a-pdf-to-a-size) · [Archive as PDF/A](#archive-as-pdfa)

## Before you start

- **Name files the way you'd say them.** Paths are relative to the folder you gave
  Paperboy with `--root` (for example `~/Documents`), so "report.pdf" or
  "invoices/march.pdf" is enough.
- **Your originals are never changed.** Every tool that changes a PDF writes a new file
  next to it, named after what it did: `report_rotated.pdf`, `scan_trimmed.pdf`. Ask for a
  different name if you like. An existing file is never overwritten unless you say so.
- **Reading is instant; writing asks first.** Tools that only read are marked read-only,
  so your AI app can run them straight away. Tools that save a file are marked so your app
  asks you before running them.
- **Passwords can stay out of the chat.** Say you'll type the password yourself, and
  Paperboy opens a page on your computer for it. See
  [Type passwords yourself](#type-passwords-yourself).
- **Some jobs need an extra program.** OCR needs Tesseract, Office conversions need
  LibreOffice, flattening forms needs the qpdf command-line tool, and the PDF/A check
  uses veraPDF. Everything else works out of the box. See
  [Optional system binaries](../README.md#optional-system-binaries).

## Watching the videos

Every guide has a short video showing the request, each tool call with its real result,
and the PDF before and after. Nothing in them is mocked: they are recorded from the real
server. The link under each guide downloads its video; all of them, and an 80-second
product video, are on the [demo videos release](https://github.com/sudostash/paperboy/releases/tag/demo-videos). They open in any modern browser.

To record them yourself from a checkout of this repository:

```bash
pip install -e ".[demo]" && playwright install chromium
python demos/record.py           # every guide, into demos/out/videos/
python demos/record.py --promo   # the product video
```

Then open `demos/viewer/index.html` in your browser: it lists every demo, grouped like this
page, with a button to watch each one. Or run `python demos/record.py --gallery`, which also
lets you replay any demo live without recording videos first. See
[demos/README.md](../demos/README.md) for more options.

---

## Getting started

### See which PDFs you have

Ask what's in your folder before picking a file to work on.

> *"Which PDFs do I have here?"*

The assistant calls `list_pdfs`, which lists every PDF in your Paperboy folder with its
size and when it last changed (up to 1,000).

Video: [`list-pdfs.webm`](https://github.com/sudostash/paperboy/releases/download/demo-videos/list-pdfs.webm)

### What runs without asking

> *"What can you do with my PDFs without asking me first? Then check report.pdf and
> rotate its last page."*

The assistant lists the tools: the read-only ones (checking, searching, viewing) can run
straight away; anything that writes a file asks first. Then it reads `report.pdf` with
`pdf_info` and rotates the last page with `rotate_pages`, which asks for your approval.

Video: [`tool-annotations.webm`](https://github.com/sudostash/paperboy/releases/download/demo-videos/tool-annotations.webm)

### Typed, predictable results

> *"Check report.pdf, then split it into one file per page."*

Every tool returns named fields (page count, page size, whether it's encrypted, which
files it wrote) instead of prose, so the assistant reports exact values. This one uses
`pdf_info` and `split_pdf`.

Video: [`structured-output.webm`](https://github.com/sudostash/paperboy/releases/download/demo-videos/structured-output.webm)

## Reading and finding

### Look at pages and save them as images

> *"What's on page 2 of report.pdf? Then save every page as a PNG."*

`view_pages` shows the assistant the page as a picture, so it can describe charts and
layout as well as text. `render_pages` saves pages as PNG or JPEG files at the
resolution you ask for.

Video: [`render-pages.webm`](https://github.com/sudostash/paperboy/releases/download/demo-videos/render-pages.webm)

### Search a PDF

> *"Where does report.pdf talk about deliveries? And which regions were below 98% on
> time?"*

`search_text` finds every match with its page and the words around it, and can be
limited to certain pages.

Video: [`search.webm`](https://github.com/sudostash/paperboy/releases/download/demo-videos/search.webm)

### Read a PDF as Markdown

> *"Turn handbook.pdf into Markdown so I can add it to our team wiki."*

`pdf_to_markdown` reads the text in reading order, column by column, and keeps headings,
lists and tables. `export_markdown` saves it as a `.md` file.

Good to know: scanned pages have no text to read; [make them searchable](#make-a-scan-searchable)
first.

Video: [`markdown.webm`](https://github.com/sudostash/paperboy/releases/download/demo-videos/markdown.webm)

### Get tables out

> *"Which region had the most failed deliveries in statement.pdf? And give me the tables
> as spreadsheets."*

`extract_tables` reads each table into rows and columns, so the assistant can answer
questions about it. `export_tables` saves every table as a CSV file that opens in Excel
or Google Sheets.

Video: [`tables.webm`](https://github.com/sudostash/paperboy/releases/download/demo-videos/tables.webm)

### Get attachments and pictures out

> *"brochure.pdf has some files attached and product photos I need for our website. Can
> you get them out?"*

`list_attachments` shows what's attached, `extract_attachments` saves those files, and
`extract_images` saves the pictures (skipping small ones like logos if you ask).

Video: [`extract.webm`](https://github.com/sudostash/paperboy/releases/download/demo-videos/extract.webm)

### Make a scan searchable

> *"scanned-letter.pdf came from the post room scanner. What's the policy number in it?"*

A scan's words are pictures, so a search finds nothing. `ocr_pdf` adds an invisible
text layer: the file looks exactly the same, but its text can now be searched, selected
and copied.

Good to know: needs Tesseract. English is included; other languages are separate
downloads.

Video: [`ocr.webm`](https://github.com/sudostash/paperboy/releases/download/demo-videos/ocr.webm)

### Compare two versions

> *"What changed between contract-v1.pdf and contract-v2.pdf?"*

`compare_pdfs` lists text changes page by page, and can also compare how the pages look,
which catches changes text can't show (a stamp that moved, a changed picture).

Video: [`compare.webm`](https://github.com/sudostash/paperboy/releases/download/demo-videos/compare.webm)

## Organizing pages

### Merge and reorder documents

> *"Make a board pack from report.pdf and statement.pdf: the report's summary first, then
> the statement, then the rest of the report. It's printed double-sided, so start the
> statement on a new sheet."*

`merge_pdfs` joins the files in order, `rearrange_pages` puts the pages in the order you
want (it can also reverse or duplicate pages), and `insert_blank_pages` adds blank pages
where you need them.

Video: [`organize.webm`](https://github.com/sudostash/paperboy/releases/download/demo-videos/organize.webm)

### Pull out a page and trim it

> *"I need just the regional breakdown from report.pdf for a slide, without the white
> margins and the footer."*

`extract_pages` saves the pages you name as a new PDF. `crop_pages` trims margins in
points, inches or millimetres.

Good to know: cropping hides the margins without deleting them, so it can be undone. To
remove content for good, use [redaction](#redact-for-real).

Video: [`extract-crop.webm`](https://github.com/sudostash/paperboy/releases/download/demo-videos/extract-crop.webm)

### Remove blank pages

> *"scan.pdf came out of the scanner with blank pages in it. Find them and take them out."*

`find_blank_pages` separates truly blank pages from nearly blank ones (a few specks of
scanner dust), so the assistant can look at the doubtful ones before `delete_pages`
removes them.

Video: [`blank-pages.webm`](https://github.com/sudostash/paperboy/releases/download/demo-videos/blank-pages.webm)

### Combine double-sided scans

> *"I scanned a double-sided lease on a single-sided scanner: fronts.pdf, then I flipped
> the stack for backs.pdf. Put it back together."*

`interleave_pdfs` alternates fronts and backs, reversing the backs when they were
scanned last page first.

Video: [`interleave.webm`](https://github.com/sudostash/paperboy/releases/download/demo-videos/interleave.webm)

### Resize pages and print 2 per sheet

> *"report.pdf is US Letter; our printers use A4. Convert it, then make a handout with
> two pages per sheet."*

`resize_pages` changes the paper size and scales the content to fit. `n_up` puts 2
pages side by side, or 4 in a grid, on each sheet.

Video: [`page-layout.webm`](https://github.com/sudostash/paperboy/releases/download/demo-videos/page-layout.webm)

### Add bookmarks

> *"report.pdf has no bookmarks, so it's hard to navigate. Add one for each section."*

`get_outline` reads the existing bookmarks, the assistant finds the headings with
`extract_text`, and `set_outline` writes the new bookmarks, nested if you like.

Video: [`bookmarks.webm`](https://github.com/sudostash/paperboy/releases/download/demo-videos/bookmarks.webm)

## Creating PDFs

### Turn photos and notes into one PDF

> *"Make one PDF for my expense claim: my notes in trip-notes.txt, then the two receipt
> photos."*

`convert_to_pdf` turns a text, Markdown, image or Office file into a PDF, and
`images_to_pdf` turns several photos into one PDF with a page each. `merge_pdfs` then
joins them.

Good to know: Word, Excel, PowerPoint and HTML files need LibreOffice; text and images
don't.

Video: [`convert.webm`](https://github.com/sudostash/paperboy/releases/download/demo-videos/convert.webm)

## Editing and marking up

### Fill in forms

> *"Fill in delivery-form.pdf for Ada Lovelace, 12 Analytical Way, Leeds. Express, large
> parcel, Leeds depot."*

`list_form_fields` reads the form's fields and their allowed values, and `fill_form`
fills them in, checking each value first. The result stays editable unless you ask for it
to be [locked](#lock-a-finished-form).

Video: [`fill-form.webm`](https://github.com/sudostash/paperboy/releases/download/demo-videos/fill-form.webm)

### Watermarks, stamps and page numbers

> *"Mark report.pdf as confidential, then Bates-number it Q3-0001 onwards in the top
> right corner."*

`add_watermark` adds text or an image across the page, or a stamp in a corner, with your
choice of colour, size and opacity. `add_page_numbers` numbers the pages in any format,
such as "Page 3 of 10" or "Q3-0001".

Video: [`watermark-numbers.webm`](https://github.com/sudostash/paperboy/releases/download/demo-videos/watermark-numbers.webm)

### Review with comments

> *"Review handbook.pdf for me: flag the one-minute wait as too long, mark "never after"
> for removal, ask whether Sunday deliveries are coming, and stamp it as a draft."*

`add_comments` adds highlights, underlines, strikeouts, sticky notes, text boxes and
replies, attached to the words you name. `list_comments` reads all the comments in a
file, including who wrote them and the text they mark.

Video: [`comments.webm`](https://github.com/sudostash/paperboy/releases/download/demo-videos/comments.webm)

### Set the title and author

> *"We're putting report.pdf on our website. Check its document properties and fix
> anything that looks wrong."*

`get_metadata` reads the title, author, subject, keywords and the programs that made the
file. `set_metadata` changes the fields you name and keeps the rest.

Video: [`metadata.webm`](https://github.com/sudostash/paperboy/releases/download/demo-videos/metadata.webm)

### Several steps in one go

> *"Make a review copy of report.pdf: drop the last page, stamp DRAFT on it, number the
> pages, and make it smaller."*

`run_pipeline` chains several tools in one call and saves only the finished file, with
no in-between copies left behind.

Video: [`pipeline.webm`](https://github.com/sudostash/paperboy/releases/download/demo-videos/pipeline.webm)

## Sharing safely

### Remove hidden data

> *"I'm about to send proposal.pdf to a client. Is anything hidden in it? If so, clean it
> up."*

`inspect_hidden_data` lists what a PDF carries besides its pages: author and software
names, scripts, attached files, comments, earlier saved versions. `sanitize_pdf` removes
it and leaves the pages as they were.

Video: [`sanitize.webm`](https://github.com/sudostash/paperboy/releases/download/demo-videos/sanitize.webm)

### Redact for real

> *"I need to send case-file.pdf to our supplier. Take out everything that identifies the
> customer: her name, email, phone and signature."*

`redact_pdf` removes text, patterns (such as every email address) and areas (such as a
signature) from the file itself, not just painted over. A search afterwards finds
nothing.

Video: [`redaction.webm`](https://github.com/sudostash/paperboy/releases/download/demo-videos/redaction.webm)

### Lock a finished form

> *"Fill in delivery-form.pdf for Ada Lovelace, 12 Analytical Way, Leeds, standard, small
> parcel, Leeds depot. Then lock it and put a password on it - I'll choose the password
> myself."*

`fill_form` fills it in, `flatten_pdf` makes the answers part of the page so they can't
be edited, and `encrypt_pdf` adds a password (AES-256 unless you ask for another kind).
You choose the password on Paperboy's own page.

Good to know: flattening needs the qpdf command-line tool.

Video: [`protect.webm`](https://github.com/sudostash/paperboy/releases/download/demo-videos/protect.webm)

### Type passwords yourself

> *"taxes.pdf is password-protected. Make an unlocked copy - I'll type the password
> myself, don't ask me for it here."*

Any password can be given as "ask". Paperboy then opens a page on your computer where you
type it, so it never passes through the conversation. Here, `decrypt_pdf` makes the
unlocked copy.

Good to know: if your AI app can't open the page itself, Paperboy gives you a link to
open instead, and the assistant runs the tool again once you've typed the password.
Administrators can make this the only way passwords are accepted.

Video: [`ask-password.webm`](https://github.com/sudostash/paperboy/releases/download/demo-videos/ask-password.webm)

### When a PDF says no

> *"Remove the draft notes page (page 4) from lease.pdf before I send it."*

Some PDFs open freely but forbid changes like removing pages. Paperboy respects that and
says so, and goes ahead only if you type the owner password. Signed PDFs are refused
where a change would break the signature.

Video: [`permissions.webm`](https://github.com/sudostash/paperboy/releases/download/demo-videos/permissions.webm)

### Check a PDF for malicious content

> *"I got invoice.pdf from a sender I don't know. Is it safe to open?"*

`scan_pdf` looks for what attackers hide in PDFs: scripts that run on opening, programs
disguised as attachments, links to unfamiliar sites. `sanitize_pdf` then makes a copy
with the active content removed.

Good to know: with ClamAV installed, it also checks against known malware. No scan can
prove a file is safe.

Video: [`scan.webm`](https://github.com/sudostash/paperboy/releases/download/demo-videos/scan.webm)

## File health and size

### Fix a PDF that won't open

> *"A supplier emailed quote.pdf and my PDF viewer says it's damaged. Can you fix it?"*

`check_pdf` reports what's wrong with the file's structure, and `repair_pdf` rebuilds it,
reporting anything still wrong in the repaired copy. A second check confirms it's sound.

Video: [`repair.webm`](https://github.com/sudostash/paperboy/releases/download/demo-videos/repair.webm)

### Shrink a PDF to a size

> *"album.pdf is too big to email. Get it under 2 MB."*

`optimize_pdf` compresses the file, and with a target size it lowers picture resolution
only as far as needed. Text is never touched.

Video: [`target-size.webm`](https://github.com/sudostash/paperboy/releases/download/demo-videos/target-size.webm)

### Archive as PDF/A

> *"The records office only accepts PDF/A. Can you make minutes.pdf archive-ready?"*

`convert_to_pdfa` makes a PDF/A-2b copy for courts and archives: it adds what the
standard requires and removes what it forbids (scripts, attachments). With veraPDF
installed, the copy is checked and saved only if it passes.

Video: [`pdfa.webm`](https://github.com/sudostash/paperboy/releases/download/demo-videos/pdfa.webm)
