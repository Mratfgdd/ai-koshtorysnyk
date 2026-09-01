"""Run the cheap extraction pass over sample PDFs and report the triage.

Usage:
    python scripts/triage_pdfs.py <pdf-or-directory> [...]
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.services.docs.pdf_extract import PAGE_TYPES, extract_document, triage_summary  # noqa: E402


def targets(args: list[str]) -> list[Path]:
    out: list[Path] = []
    for a in args:
        p = Path(a)
        if p.is_dir():
            out.extend(sorted(p.rglob("*.pdf")))
        elif p.suffix.lower() == ".pdf":
            out.append(p)
    return out


def main() -> int:
    files = targets(sys.argv[1:])
    if not files:
        print(__doc__)
        return 2

    for f in files:
        t0 = time.perf_counter()
        try:
            doc = extract_document(f)
        except Exception as exc:
            print(f"!! {f.name}: {exc}")
            continue
        elapsed = time.perf_counter() - t0
        summary = triage_summary(doc)

        print("=" * 100)
        print(f"{f.name}")
        print(f"  size {f.stat().st_size / 1e6:7.1f} MB | {doc.page_count:3d} pages "
              f"| kind={doc.kind} | cheap pass {elapsed:.2f}s")
        print(f"  types: {json.dumps(summary['by_type'], ensure_ascii=False)}")
        print(f"  vision needed on {len(summary['vision_pages'])} pages: {summary['vision_pages']}")
        print(f"  skipped {len(summary['skipped_pages'])} pages")
        for p in doc.pages:
            if p.page_type in ("plant_schedule", "spec_table", "master_plan", "layout_plan"):
                print(f"    p{p.page_number:<3d} {PAGE_TYPES[p.page_type]:36s} "
                      f"text={p.text_length:5d} tables={len(p.tables)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
