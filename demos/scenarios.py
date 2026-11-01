"""Demo scenarios: one per feature, each a scripted conversation.

A scenario is the user's prompt plus the steps the assistant takes. ``record.py``
executes ``call`` steps against the real MCP server, so every result shown in a
video is genuine output, never mocked.

Step types:
    {"type": "tools"}                      list the server's tools and their hints
    {"type": "say", "text": ...}           an assistant message
    {"type": "call", "tool": ..., "args": {...},
     "show": {"before": file, "after": file,   call a tool, optionally render PDFs
              "highlight": result_field,        (outlining pages listed in the result
              "review": result_field,           in red, or amber for "look first";
              "layout": "side-by-side"},        before/after next to each other),
     "showSchema": True,                       or show its typed result vs. its schema,
     "showImages": True,                       or the images it returned / files it saved,
     "showTable": result_field}                or a result object as a findings table

File names in ``args`` and ``show`` are relative to the scenario workspace,
which is also the server's ``PAPERBOY_ROOT``.
"""
from __future__ import annotations

SCENARIOS: dict[str, dict] = {
    "tool-annotations": {
        "title": "Safe-by-default tool hints",
        "subtitle": "Read-only tools run instantly. Anything that writes a file asks first.",
        "files": ["report.pdf"],
        "prompt": "What can you do with my PDFs without asking me first? "
                  "Then check report.pdf and rotate its last page.",
        "steps": [
            {"type": "tools"},
            {"type": "say", "text": "Four inspection tools are marked read-only, so your "
                                    "client can run them straight away. Every tool that "
                                    "writes a file is marked destructive and asks first."},
            {"type": "call", "tool": "pdf_info", "args": {"input_path": "report.pdf"},
             "show": {"after": "report.pdf"}},
            {"type": "call", "tool": "rotate_pages",
             "args": {"input_path": "report.pdf", "degrees": 90, "pages": "last"},
             "show": {"before": "report.pdf", "after": "report_rotated.pdf"}},
            {"type": "say", "text": "Done. The last page of report.pdf is rotated 90° in "
                                    "report_rotated.pdf, and the original is untouched."},
        ],
    },
    "structured-output": {
        "title": "Typed, predictable results",
        "subtitle": "Every tool publishes an output schema, so assistants read exact fields "
                    "instead of parsing text.",
        "files": ["report.pdf"],
        "prompt": "Check report.pdf, then split it into one file per page.",
        "steps": [
            {"type": "call", "tool": "pdf_info", "args": {"input_path": "report.pdf"},
             "showSchema": True},
            {"type": "say", "text": "report.pdf has 3 pages, is not encrypted, and its first "
                                    "page is 612 × 792 pt (US Letter)."},
            {"type": "call", "tool": "split_pdf", "args": {"input_path": "report.pdf"},
             "showSchema": True},
            {"type": "say", "text": "Split into 3 files in report_split/, report_part001.pdf "
                                    "to report_part003.pdf. None of them needs a password."},
        ],
    },
    "render-pages": {
        "title": "See and export pages as images",
        "subtitle": "The assistant can look at a page, and save pages as PNG or JPEG.",
        "files": ["report.pdf"],
        "prompt": "What's on page 2 of report.pdf? Then save every page as a PNG.",
        "steps": [
            {"type": "call", "tool": "view_pages",
             "args": {"input_path": "report.pdf", "pages": "2"}, "showImages": True},
            {"type": "say", "text": "Page 2 is the Regional Breakdown: North 12,400 deliveries "
                                    "(98.1% on time), South 10,950, East 8,210 and West 9,775, "
                                    "with a bar chart underneath."},
            {"type": "call", "tool": "render_pages", "args": {"input_path": "report.pdf"},
             "showImages": True},
            {"type": "say", "text": "Saved 3 PNGs at 150 dpi in report_pages/: report_p001.png "
                                    "to report_p003.png."},
        ],
    },
    "blank-pages": {
        "title": "Find and remove blank pages",
        "subtitle": "Empty pages and scanner dust are found by text and ink, then removed.",
        "files": ["scan.pdf"],
        "prompt": "scan.pdf came out of the scanner with blank pages in it. "
                  "Find them and take them out.",
        "steps": [
            {"type": "call", "tool": "find_blank_pages", "args": {"input_path": "scan.pdf"},
             "show": {"after": "scan.pdf", "highlight": "blank_pages",
                      "review": "nearly_blank_pages"}},
            {"type": "say", "text": "Page 2 is blank. Page 4 is nearly blank: it has a few "
                                    "marks, so I'll look at it before deleting anything."},
            {"type": "call", "tool": "view_pages",
             "args": {"input_path": "scan.pdf", "pages": "4"}, "showImages": True},
            {"type": "say", "text": "Page 4 is only scanner dust. Removing pages 2 and 4."},
            {"type": "call", "tool": "delete_pages",
             "args": {"input_path": "scan.pdf", "pages": "2,4"},
             "show": {"before": "scan.pdf", "after": "scan_trimmed.pdf"}},
            {"type": "say", "text": "Done. scan_trimmed.pdf has the 3 pages with content, "
                                    "and scan.pdf is unchanged."},
        ],
    },
    "fill-form": {
        "title": "Fill in PDF forms",
        "subtitle": "List a form's fields, then fill them in. Every value is checked first.",
        "files": ["delivery-form.pdf"],
        "prompt": "Fill in delivery-form.pdf for Ada Lovelace, 12 Analytical Way, Leeds. "
                  "Express, large parcel, Leeds depot.",
        "steps": [
            {"type": "call", "tool": "list_form_fields",
             "args": {"input_path": "delivery-form.pdf"},
             "show": {"after": "delivery-form.pdf"}},
            {"type": "say", "text": "The form has five fields: name and address (text), express "
                                    "(a checkbox), size (small, medium or large) and depot "
                                    "(Leeds, Porto or Lyon). Filling them in."},
            {"type": "call", "tool": "fill_form",
             "args": {"input_path": "delivery-form.pdf",
                      "values": {"name": "Ada Lovelace", "address": "12 Analytical Way, Leeds",
                                 "express": True, "size": "large", "depot": "Leeds"}},
             "show": {"before": "delivery-form.pdf", "after": "delivery-form_filled.pdf",
                      "layout": "side-by-side"}},
            {"type": "say", "text": "Done: delivery-form_filled.pdf has every field filled in, "
                                    "and it's still editable. Ask me to flatten it if you want "
                                    "it locked."},
        ],
    },
    "watermark-numbers": {
        "title": "Watermarks, stamps and page numbers",
        "subtitle": "Mark a document CONFIDENTIAL and number its pages, on any page layout.",
        "files": ["report.pdf"],
        "prompt": "Mark report.pdf as confidential, then Bates-number it Q3-0001 onwards "
                  "in the top right corner.",
        "steps": [
            {"type": "call", "tool": "add_watermark",
             "args": {"input_path": "report.pdf", "text": "CONFIDENTIAL", "color": "red",
                      "opacity": 0.15},
             "show": {"before": "report.pdf", "after": "report_watermarked.pdf"}},
            {"type": "say", "text": "Added a faint diagonal CONFIDENTIAL across every page. "
                                    "Now the Bates numbers."},
            {"type": "call", "tool": "add_page_numbers",
             "args": {"input_path": "report_watermarked.pdf", "format": "Q3-{n:04}",
                      "position": "top-right", "margin": 30, "font_size": 11},
             "show": {"after": "report_watermarked_numbered.pdf"}},
            {"type": "say", "text": "Done: report_watermarked_numbered.pdf has the watermark "
                                    "and Bates numbers Q3-0001 to Q3-0003 in the top right."},
        ],
    },
    "sanitize": {
        "title": "Remove hidden data before sharing",
        "subtitle": "Find what a PDF carries besides its pages, then send a clean copy.",
        "files": ["proposal.pdf"],
        "prompt": "I'm about to send proposal.pdf to a client. Is anything hidden in it? "
                  "If so, clean it up.",
        "steps": [
            {"type": "call", "tool": "inspect_hidden_data",
             "args": {"input_path": "proposal.pdf"}, "showTable": "found"},
            {"type": "say", "text": "Yes: the author's name and editing software, a script "
                                    "that runs when it opens, an attached internal pricing "
                                    "spreadsheet, a reviewer's comment, and an earlier saved "
                                    "version. Removing all of it, comments included."},
            {"type": "call", "tool": "sanitize_pdf",
             "args": {"input_path": "proposal.pdf", "remove_comments": True},
             "showTable": "removed"},
            {"type": "call", "tool": "inspect_hidden_data",
             "args": {"input_path": "proposal_sanitized.pdf"}, "showTable": "found"},
            {"type": "say", "text": "Done. proposal_sanitized.pdf has nothing hidden left and "
                                    "its pages are unchanged; proposal.pdf is untouched."},
        ],
    },
    "scan": {
        "title": "Check a PDF for malicious content",
        "subtitle": "Look for what attackers hide in PDFs, then make a defused copy.",
        "files": ["invoice.pdf"],
        "prompt": "I got invoice.pdf from a sender I don't know. Is it safe to open?",
        "steps": [
            {"type": "call", "tool": "scan_pdf", "args": {"input_path": "invoice.pdf"},
             "showTable": "indicators", "headline": "verdict",
             "columns": ["code", "severity", "detail"]},
            {"type": "say", "text": "Don't open it. It runs a script the moment it opens, "
                                    "and that script launches an attached Windows program "
                                    "named statement.pdf.exe to look like a PDF. It also "
                                    "links to an unfamiliar payment site. (No antivirus is "
                                    "installed here, so this is a structural check.) "
                                    "Making a copy with the script and attachment removed."},
            {"type": "call", "tool": "sanitize_pdf", "args": {"input_path": "invoice.pdf"},
             "showTable": "removed"},
            {"type": "call", "tool": "scan_pdf",
             "args": {"input_path": "invoice_sanitized.pdf"},
             "showTable": "indicators", "headline": "verdict",
             "columns": ["code", "severity", "detail"]},
            {"type": "say", "text": "No active content found in invoice_sanitized.pdf; the "
                                    "remaining link is listed for you to judge. Still treat "
                                    "the sender as suspect: a scan can't prove a file is safe."},
        ],
    },
    "bookmarks": {
        "title": "Add bookmarks to a PDF",
        "subtitle": "Read the section headings, then give the file a clickable outline.",
        "files": ["report.pdf"],
        "prompt": "report.pdf has no bookmarks, so it's hard to navigate. Add one for each "
                  "section.",
        "steps": [
            {"type": "call", "tool": "get_outline", "args": {"input_path": "report.pdf"}},
            {"type": "say", "text": "No bookmarks yet. Reading the headings on each page."},
            {"type": "call", "tool": "extract_text", "args": {"input_path": "report.pdf"}},
            {"type": "say", "text": "Three sections, one per page, each with a subheading. "
                                    "Adding them as bookmarks, with the subheadings nested."},
            {"type": "call", "tool": "set_outline", "args": {
                "input_path": "report.pdf",
                "entries": [
                    {"title": "Q3 Field Report", "page": 1},
                    {"title": "Summary", "page": 1, "level": 2},
                    {"title": "Regional Breakdown", "page": 2},
                    {"title": "North, South, East, West", "page": 2, "level": 2},
                    {"title": "Next Quarter", "page": 3},
                    {"title": "Priorities", "page": 3, "level": 2},
                ]},
             "show": {"after": "report_bookmarked.pdf"}},
            {"type": "call", "tool": "get_outline",
             "args": {"input_path": "report_bookmarked.pdf"},
             "showTable": "entries", "columns": ["title", "page"], "indent": "level"},
            {"type": "say", "text": "Done. report_bookmarked.pdf opens with the bookmarks "
                                    "panel showing; report.pdf is unchanged."},
        ],
    },
    "extract": {
        "title": "Get attachments and pictures out of a PDF",
        "subtitle": "List what's embedded, save the files, and pull out the product photos.",
        "files": ["brochure.pdf"],
        "prompt": "brochure.pdf has some files attached and product photos I need for our "
                  "website. Can you get them out?",
        "steps": [
            {"type": "call", "tool": "list_attachments", "args": {"input_path": "brochure.pdf"},
             "showTable": "attachments", "columns": ["name", "description", "detected_type"]},
            {"type": "say", "text": "Two attached files, both plain text: a spec sheet and a "
                                    "price list. Saving them."},
            {"type": "call", "tool": "extract_attachments",
             "args": {"input_path": "brochure.pdf"},
             "showTable": "files", "columns": ["name", "size"]},
            {"type": "say", "text": "Now the photos, leaving out the small logo."},
            {"type": "call", "tool": "extract_images",
             "args": {"input_path": "brochure.pdf", "min_size": 100}, "showImages": True},
            {"type": "say", "text": "Done. The two files are in brochure_attachments and the "
                                    "two product photos (saved as the original JPEGs) are in "
                                    "brochure_images."},
        ],
    },
    "search": {
        "title": "Search a PDF",
        "subtitle": "Find every mention of a word, with the page and the text around it.",
        "files": ["report.pdf"],
        "prompt": "Where does report.pdf talk about deliveries? And which regions were "
                  "below 98% on time?",
        "steps": [
            {"type": "call", "tool": "search_text",
             "args": {"input_path": "report.pdf", "query": "deliveries"},
             "showTable": "matches", "columns": ["page", "context"]},
            {"type": "say", "text": "Deliveries come up 6 times: the 18% rise on page 1, "
                                    "each region's total on page 2, and the failed-delivery "
                                    "target on page 3. Checking the on-time rates on page 2."},
            {"type": "call", "tool": "search_text",
             "args": {"input_path": "report.pdf", "query": "on time", "pages": "2"},
             "showTable": "matches", "columns": ["page", "context"]},
            {"type": "say", "text": "Two regions were below 98% on time: South (97.4%) and "
                                    "East (96.9%). North (98.1%) and West (98.6%) were above."},
        ],
    },
    "page-layout": {
        "title": "Resize pages and print 2 per sheet",
        "subtitle": "Convert a Letter report to A4, then make a 2-up handout.",
        "files": ["report.pdf"],
        "prompt": "report.pdf is US Letter; our printers use A4. Convert it, then make a "
                  "handout with two pages per sheet.",
        "steps": [
            {"type": "call", "tool": "resize_pages",
             "args": {"input_path": "report.pdf", "size": "a4"},
             "show": {"before": "report.pdf", "after": "report_resized.pdf"}},
            {"type": "say", "text": "report_resized.pdf is A4, with each page's content "
                                    "scaled slightly to fit. Now the handout."},
            {"type": "call", "tool": "n_up",
             "args": {"input_path": "report_resized.pdf", "per_sheet": 2, "size": "a4"},
             "show": {"after": "report_resized_2up.pdf"}},
            {"type": "say", "text": "Done: report_resized_2up.pdf has the 3 pages on 2 "
                                    "landscape A4 sheets, ready to print."},
        ],
    },
    "interleave": {
        "title": "Combine double-sided scans",
        "subtitle": "Fronts and backs from a single-sided scanner, back in page order.",
        "files": ["fronts.pdf", "backs.pdf"],
        "prompt": "I scanned a double-sided lease on a single-sided scanner: fronts.pdf, "
                  "then I flipped the stack for backs.pdf. Put it back together.",
        "steps": [
            {"type": "call", "tool": "interleave_pdfs",
             "args": {"front_path": "fronts.pdf", "back_path": "backs.pdf",
                      "output_path": "lease.pdf"},
             "show": {"after": "lease.pdf"}},
            {"type": "say", "text": "lease.pdf has all 6 pages in order: the backs came out "
                                    "last page first from the flipped stack, so I reversed "
                                    "them while interleaving."},
        ],
    },
    "tables": {
        "title": "Get tables out of a PDF",
        "subtitle": "Read the tables in a statement, then save them as CSV for a spreadsheet.",
        "files": ["statement.pdf"],
        "prompt": "Which region had the most failed deliveries in statement.pdf? And give me "
                  "the tables as spreadsheets.",
        "steps": [
            {"type": "call", "tool": "extract_tables", "args": {"input_path": "statement.pdf"},
             "showGrid": True},
            {"type": "say", "text": "South had the most failed deliveries (285), then East "
                                    "(254). Saving both tables as CSV files."},
            {"type": "call", "tool": "export_tables", "args": {"input_path": "statement.pdf"},
             "showTable": "files", "columns": ["file", "row_count", "column_count"]},
            {"type": "say", "text": "Done: statement_tables has page001-table01.csv (regions) "
                                    "and page002-table01.csv (charges), ready for Excel."},
        ],
    },
    "compare": {
        "title": "Compare two versions of a PDF",
        "subtitle": "See what changed in the text, page by page, and how the pages look.",
        "files": ["contract-v1.pdf", "contract-v2.pdf"],
        "prompt": "What changed between contract-v1.pdf and contract-v2.pdf?",
        "steps": [
            {"type": "call", "tool": "compare_pdfs",
             "args": {"path_a": "contract-v1.pdf", "path_b": "contract-v2.pdf"},
             "showTable": "differences", "columns": ["status", "page_b", "removed", "added"]},
            {"type": "say", "text": "Two text changes: the monthly fee went from EUR 4,800 to "
                                    "EUR 5,250, and a new page 3, Schedule B, charges weekend "
                                    "deliveries at 1.5x. Checking the layout too."},
            {"type": "call", "tool": "compare_pdfs",
             "args": {"path_a": "contract-v1.pdf", "path_b": "contract-v2.pdf",
                      "mode": "both"},
             "showImages": True},
            {"type": "say", "text": "One more change the text doesn't show: the approval "
                                    "stamp on page 1 moved from the left (red: where it "
                                    "was) to the right (green: where it is now)."},
        ],
    },
    "pipeline": {
        "title": "Run several steps in one go",
        "subtitle": "Chain tools in a single call; only the finished file is saved.",
        "files": ["report.pdf"],
        "prompt": "Make a review copy of report.pdf: drop the last page, stamp DRAFT on "
                  "it, number the pages, and make it smaller.",
        "steps": [
            {"type": "call", "tool": "run_pipeline",
             "args": {"input_path": "report.pdf", "output_path": "report_review.pdf",
                      "steps": [
                          {"tool": "delete_pages", "args": {"pages": "3"}},
                          {"tool": "add_watermark", "args": {"text": "DRAFT"}},
                          {"tool": "add_page_numbers"},
                          {"tool": "optimize_pdf"}]},
             "show": {"before": "report.pdf", "after": "report_review.pdf"}},
            {"type": "say", "text": "Done in one call: report_review.pdf has pages 1-2 with "
                                    "the DRAFT watermark and page numbers, optimized. No "
                                    "in-between files were left behind, and report.pdf is "
                                    "unchanged."},
        ],
    },
    "list-pdfs": {
        "title": "See which PDFs you have",
        "subtitle": "List the PDFs Paperboy may use, then pick one to work on.",
        "files": ["report.pdf", "proposal.pdf", "statement.pdf", "contract-v1.pdf",
                  "contract-v2.pdf"],
        "prompt": "Which PDFs do I have here?",
        "steps": [
            {"type": "call", "tool": "list_pdfs", "args": {},
             "showTable": "files", "columns": ["relative_path", "size", "modified"]},
            {"type": "say", "text": "Five PDFs: two versions of a contract, a proposal, a "
                                    "delivery statement and the Q3 report. Want me to compare "
                                    "the two contract versions, or get one ready to share?"},
        ],
    },
    "ask-password": {
        "title": "Type passwords yourself",
        "subtitle": "The password goes from you to Paperboy, never into the chat.",
        "files": ["taxes.pdf"],
        "prompt": "taxes.pdf is password-protected. Make an unlocked copy - I'll type the "
                  "password myself, don't ask me for it here.",
        "steps": [
            {"type": "call", "tool": "decrypt_pdf",
             "args": {"input_path": "taxes.pdf", "password": "ask"},
             "answers": [{"password": "Spring2026!"}],
             "show": {"after": "taxes_decrypted.pdf"}},
            {"type": "say", "text": "Done: taxes_decrypted.pdf opens without a password. You "
                                    "typed the password on Paperboy's own page on this "
                                    "computer, so it never appeared in our conversation."},
        ],
    },
    "target-size": {
        "title": "Shrink a PDF to a size",
        "subtitle": "Lower picture resolution only as far as needed to fit an email.",
        "files": ["album.pdf"],
        "prompt": "album.pdf is too big to email. Get it under 2 MB.",
        "steps": [
            {"type": "call", "tool": "pdf_info", "args": {"input_path": "album.pdf"}},
            {"type": "say", "text": "It's 3 pages of large photos. Email encoding adds about "
                                    "a third, so I'll aim for 1.5 MB on disk."},
            {"type": "call", "tool": "optimize_pdf",
             "args": {"input_path": "album.pdf", "linearize": False,
                      "target_size_mb": 1.5},
             "show": {"before": "album.pdf", "after": "album_optimized.pdf"}},
            {"type": "say", "text": "Done: album_optimized.pdf fits, with the photos at a "
                                    "resolution that still looks sharp on screen. The text "
                                    "is untouched."},
        ],
    },
    "redaction": {
        "title": "Redact for real",
        "subtitle": "Remove names, contact details and a signature from the file, not "
                    "just cover them.",
        "files": ["case-file.pdf"],
        "prompt": "I need to send case-file.pdf to our supplier. Take out everything that "
                  "identifies the customer: her name, email, phone and signature.",
        "steps": [
            {"type": "call", "tool": "search_text",
             "args": {"input_path": "case-file.pdf", "query": "Customer signature"}},
            {"type": "say", "text": "The signature sits just above its caption, so I'll "
                                    "redact that area along with her name, email address "
                                    "and phone number."},
            {"type": "call", "tool": "redact_pdf",
             "args": {"input_path": "case-file.pdf", "texts": ["Maria Gonzalez"],
                      "patterns": [r"[\w.+-]+@[\w-]+\.[\w.]+", r"\(\d{3}\) \d{3}-\d{4}"],
                      "areas": [{"page": 1, "rect": [68, 378, 302, 444]}]},
             "show": {"before": "case-file.pdf", "after": "case-file_redacted.pdf"}},
            {"type": "call", "tool": "search_text",
             "args": {"input_path": "case-file_redacted.pdf", "query": "Gonzalez"}},
            {"type": "say", "text": "Done. case-file_redacted.pdf no longer contains her "
                                    "name, email, phone number or signature: they were "
                                    "removed from the page and the document title, not "
                                    "just painted over, and a search finds nothing."},
        ],
    },
    "ocr": {
        "title": "Make a scan searchable",
        "subtitle": "OCR adds an invisible text layer, so a scanned letter can be searched "
                    "and copied.",
        "files": ["scanned-letter.pdf"],
        "prompt": "scanned-letter.pdf came from the post room scanner. What's the policy "
                  "number in it?",
        "steps": [
            {"type": "call", "tool": "search_text",
             "args": {"input_path": "scanned-letter.pdf", "query": "Policy number"}},
            {"type": "say", "text": "Nothing found: both pages are scans, so their words are "
                                    "pictures. I'll run OCR to give them a text layer."},
            {"type": "call", "tool": "ocr_pdf",
             "args": {"input_path": "scanned-letter.pdf"},
             "show": {"before": "scanned-letter.pdf", "after": "scanned-letter_ocr.pdf"}},
            {"type": "call", "tool": "search_text",
             "args": {"input_path": "scanned-letter_ocr.pdf", "query": "Policy number"}},
            {"type": "say", "text": "The policy number is NW-7731-0442 (claim reference "
                                    "CL-20931). scanned-letter_ocr.pdf looks exactly like the "
                                    "scan, but its text can now be searched, selected and "
                                    "copied."},
        ],
    },
}
