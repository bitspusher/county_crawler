#!/usr/bin/env python3
"""capture_fixtures.py — replace the synthetic test fixtures with real markup.

MVP.md §6.2 requires both parsers to be tested against CAPTURED fixtures. The
committed ones are hand-authored (see tests/fixtures/README.md), which makes the
parser tests a regression lock rather than a correctness check. This script
closes that gap: it takes one page of live search results and two detail views,
writes them into tests/fixtures/, and rewrites the provenance table.

Expect parser tests to FAIL after the first real capture. That failure is the
point — it is the first genuine signal about whether parse_results and
parse_detail work against San Joaquin County's actual markup.

    python scripts/capture_fixtures.py --headed

Costs at most four requests. Stores nothing in the database.
"""

import argparse
import re
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import collect_sjc
from collect_sjc import DOCTYPES, HOST, KNOWN_IDS, SEARCH_ID, parse_detail, parse_results

FIXTURES = Path(__file__).resolve().parent.parent / "tests" / "fixtures"

BANNER = "<!-- CAPTURED from the live portal on {stamp}. Do not hand-edit. -->\n"


def say(m=""):
    print(m, flush=True)


def write(name, html, stamp):
    path = FIXTURES / name
    path.write_text(BANNER.format(stamp=stamp) + html, encoding="utf-8")
    say(f"  wrote {path.relative_to(FIXTURES.parent.parent)}  ({len(html):,} bytes)")
    return name


def update_readme(captured, stamp):
    """Rewrite the provenance table so a reader can never be misled about it."""
    readme = FIXTURES / "README.md"
    text = readme.read_text(encoding="utf-8")

    def row(m):
        name = m.group(1)
        if name in captured:
            return f"| `{name}` | **CAPTURED** — live portal, {stamp} |"
        return m.group(0)

    text = re.sub(r"\| `([^`]+)` \| \*\*SYNTHETIC\*\*[^|]*\|", row, text)
    if set(captured) >= {"search_results.html", "detail_tdus.html"}:
        text = text.replace(
            "Every fixture in this directory is currently synthetic.",
            f"Some fixtures were captured live on {stamp}; the table above is authoritative per file.",
        )
    readme.write_text(text, encoding="utf-8")
    say(f"  updated {readme.relative_to(FIXTURES.parent.parent)}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--delay", type=float, default=1.5)
    ap.add_argument("--types", default="TDUS")
    # A 3-day default window, NOT a month: the portal ignores the doc-type
    # filter, so every search is an unfiltered sweep and a full month is ~130
    # pages — it trips the result cap every time. 3 days is ~14 pages.
    ap.add_argument("--start", help="window start YYYY-MM-DD (default: last 3 days of the prior month)")
    ap.add_argument("--end", help="window end YYYY-MM-DD")
    args = ap.parse_args()

    if collect_sjc.sync_playwright is None:
        return say("pip install playwright && python -m playwright install chromium") or 2

    label = DOCTYPES.get(args.types.strip(), DOCTYPES["TDUS"])
    end = date.fromisoformat(args.end) if args.end else date.today().replace(day=1) - timedelta(days=1)
    start = date.fromisoformat(args.start) if args.start else end - timedelta(days=2)
    stamp = date.today().isoformat()
    captured = []

    say(f"Capturing {label} for {start}..{end}")

    with collect_sjc.sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(collect_sjc.PROFILE), headless=not args.headed, viewport={"width": 1400, "height": 900}
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(f"{HOST}/Web/", wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(2500)
        if "I Accept" in page.content():
            if not args.headed:
                ctx.close()
                say("Disclaimer/CAPTCHA gate — re-run with --headed.")
                return 2
            # Capture the gate itself while it is on screen: it is the shape an
            # unauthenticated session produces, and the zero-row guard's reason
            # for existing.
            captured.append(write("disclaimer.html", page.content(), stamp))
            input("  >>> Clear the disclaimer/CAPTCHA, then press Enter...\n")

        portal = collect_sjc.Portal(ctx, page, delay=args.delay, debug=False)

        vocab = {label: KNOWN_IDS[label]} if label in KNOWN_IDS else None
        try:
            portal.search(label, start, end, mode="id" if vocab else "label", vocab=vocab)
        except collect_sjc.ResultCapExceeded as e:
            ctx.close()
            say(f"Result cap: {e}")
            say("Narrow the window with --start/--end (a 1-2 day range always fits).")
            return 1

        grid = portal.xhr(f"{HOST}/Web/searchResults/{SEARCH_ID}?page=1")
        all_rows = parse_results(grid)
        # The sweep is unfiltered, so page 1 carries every doc type recorded in
        # the window; details must come from rows of the TARGET type only.
        rows = [r for r in all_rows if r.get("doc_type") == label]
        say(f"  page 1: {len(all_rows)} rows parsed, {len(rows)} of type '{label}'")
        say("  NOTE: captured fixtures carry real party names — redact individual")
        say("  names per AI_CONTEXT.md rule 11 before committing.")
        captured.append(write("search_results.html", grid, stamp))

        # Two details: one with tax, one with $0.00 if the month contains one.
        # The pair is what makes the §11926 split testable at all.
        with_tax, zero_tax = None, None
        for r in [r for r in rows if r.get("detail_id")][:8]:
            html = portal.detail(r["detail_id"])
            tax = parse_detail(html).get("tax_amount")
            if tax and with_tax is None:
                with_tax = html
            elif tax == 0.0 and zero_tax is None:
                zero_tax = html
            if with_tax and zero_tax:
                break

        if with_tax:
            captured.append(write("detail_tdus.html", with_tax, stamp))
        else:
            say("  !! no detail with a non-zero tax amount found — detail_tdus.html left synthetic")
        if zero_tax:
            captured.append(write("detail_tdus_zero_tax.html", zero_tax, stamp))
        else:
            say("  !! no $0.00-tax detail found in this month.")
            say("     That is itself a Phase 2 finding: if NO TDUS carries zero tax,")
            say("     the §11926 split may not exist in this county's data and")
            say("     sale_class needs replacing (MVP.md §6.5).")

        ctx.close()

    if captured:
        update_readme(captured, stamp)

    say("\nNow run the parser tests. Failures here are real signal:")
    say("  pytest tests/test_parsers.py -v")
    return 0


if __name__ == "__main__":
    sys.exit(main())
