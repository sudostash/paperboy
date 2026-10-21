# Third-party components

This project is MIT-licensed and builds on these libraries, which carry their own licenses:

| Component | License | Role |
|---|---|---|
| [pikepdf](https://github.com/pikepdf/pikepdf) | MPL-2.0 | Core PDF engine (wraps qpdf) |
| [qpdf](https://github.com/qpdf/qpdf) (bundled in pikepdf wheels) | Apache-2.0 | PDF structural operations |
| [pypdf](https://github.com/py-pdf/pypdf) | BSD-3-Clause | Text extraction |
| [reportlab](https://www.reportlab.com/) | BSD-3-Clause | Text/image → PDF creation |
| [Pillow](https://python-pillow.org/) | MIT-CMU | Image handling |
| [mcp](https://github.com/modelcontextprotocol/python-sdk) | MIT | MCP server SDK |

Optional runtime binaries (not bundled, invoked if present on PATH):

| Binary | License | Used for |
|---|---|---|
| LibreOffice (`soffice`) | MPL-2.0 / LGPL-3.0 | Office/HTML document → PDF |
| qpdf CLI | Apache-2.0 | Annotation flattening, deep validation |

MPL-2.0 is a file-level copyleft and does not impose copyleft on this project's
own MIT-licensed source. If you redistribute modified copies of pikepdf or qpdf
themselves, those files remain under MPL-2.0 / Apache-2.0.
