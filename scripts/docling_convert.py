"""Convert a PDF to docling text items JSON. Runs as an isolated subprocess.

Kept import-light: docling/torch load only inside main(), and this script is
invoked by webapp.services.docling_import with PYTHONUTF8=1 and torch compile
disabled (Windows quirks). Output: JSON list of {"label", "text"}.
"""
import json
import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: docling_convert.py <pdf_path> <out_json>", file=sys.stderr)
        return 2
    pdf_path, out_json = sys.argv[1], Path(sys.argv[2])

    from docling.document_converter import DocumentConverter

    result = DocumentConverter().convert(pdf_path)
    items = []
    for item in getattr(result.document, "texts", []):
        label = getattr(getattr(item, "label", None), "value", "") or ""
        items.append({"label": str(label).lower(), "text": getattr(item, "text", "") or ""})
    out_json.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
