"""Compile the client's template workbook into `backend/app/data/rules/`.

Usage:
    python scripts/compile_template.py "path/to/Шаблон для ШІ.xlsx"
"""

from __future__ import annotations

import collections
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.services.ingest.template_compiler import compile_template  # noqa: E402

OUT_DIR = ROOT / "backend" / "app" / "data" / "rules"


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    src = Path(sys.argv[1])
    pack = compile_template(src)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / "template_layout.json"
    out.write_text(json.dumps(pack, ensure_ascii=False, indent=2), encoding="utf-8")

    stats = collections.Counter()
    review: list[tuple[str, str, str, str]] = []
    for sec in pack["sections"]:
        for line in sec["lines"]:
            stats[line["qty_status"]] += 1
            if line["qty_status"] == "needs_review":
                review.append((sec["key"], str(line["row"]), line["name"], line["qty_formula"] or ""))

    print(f"sections           : {len(pack['sections'])}")
    print(f"lines total        : {sum(stats.values())}")
    for k, v in stats.most_common():
        print(f"  {k:14s}: {v}")
    print(f"\nwritten -> {out}")

    # Always rewrite the review file, and remove it when there is nothing to
    # review: a stale copy from an earlier run would claim the pack still
    # contains guessed coefficients when it does not.
    rp = OUT_DIR / "needs_review.json"
    if review:
        rp.write_text(
            json.dumps(
                [{"section": s, "row": r, "name": n, "formula": f} for s, r, n, f in review],
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"needs review -> {rp}  ({len(review)} formulas)")
    else:
        rp.unlink(missing_ok=True)
        print("needs review -> none (усі формули перекладено)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
