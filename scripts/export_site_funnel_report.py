#!/usr/bin/env python3
"""One-off: TG site funnel (afisha_besplat + quick_booking) → Word report.

Usage (from repo root, with DATABASE_URL in .env):
    python scripts/export_site_funnel_report.py
    python scripts/export_site_funnel_report.py -o data/reports/site_funnel.docx
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> int:
    parser = argparse.ArgumentParser(description="Export TG site funnel report to Word")
    parser.add_argument(
        "-o",
        "--output",
        default=str(ROOT / "data" / "reports" / "site_funnel_tg.docx"),
        help="Output .docx path",
    )
    args = parser.parse_args()

    from bot.reports.site_funnel import build_site_funnel_docx_bytes, fetch_site_funnel_report

    report = fetch_site_funnel_report()
    if not report.get("available"):
        print("Report unavailable: need postgres DATABASE_URL", file=sys.stderr)
        return 1

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(build_site_funnel_docx_bytes(report))
    print(f"Report written: {out.resolve()}")
    print(f"Site cohort (unique): {report['site_total']}")
    for step in report["steps"]:
        print(f"  {step['label']}: {step['uniques']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
