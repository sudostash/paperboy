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
| [pypdfium2](https://github.com/pypdfium2-team/pypdfium2) | BSD-3-Clause / Apache-2.0 | Page rendering (`view_pages`, `render_pages`) |
| [PDFium](https://pdfium.googlesource.com/pdfium/) (bundled in pypdfium2 wheels, with its permissively licensed dependencies: FreeType, libjpeg-turbo, libpng, OpenJPEG, zlib, ICU, lcms2) | BSD-3-Clause and others | PDF rasterization |
| [typing-extensions](https://github.com/python/typing_extensions) | PSF-2.0 | Typed result shapes |

Optional runtime binaries (not bundled, invoked if present on PATH):

| Binary | License | Used for |
|---|---|---|
| LibreOffice (`soffice`) | MPL-2.0 / LGPL-3.0 | Office/HTML document → PDF |
| qpdf CLI | Apache-2.0 | Annotation flattening, deep validation |

MPL-2.0 is a file-level copyleft and does not impose copyleft on this project's
own MIT-licensed source. If you redistribute modified copies of pikepdf or qpdf
themselves, those files remain under MPL-2.0 / Apache-2.0.
