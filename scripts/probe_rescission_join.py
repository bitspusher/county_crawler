#!/usr/bin/env python3
"""probe_rescission_join.py — can a rescission be joined to the notice it kills?

ROADMAP Phase 3's open design question, and the last thing standing between the
project and an honest Section A. June 2026 returned 43 Notice Of Trustees Sale
against 47 Rescission Of Default, so an unjoined upcoming list is publishing
sales that will not happen (§3 calls this a correctness requirement, not a
feature).

Scope narrowed 2026-07-30. This probe originally covered `Cancellation/Termination`
too, on the 130-document count. That half is now answered and the answer was zero:
all 83 June cancellations are municipal lien releases, rooftop-solar UCC
terminations and homebuilder filings, with no foreclosure trustee firm on any of
them. The type has been dropped from `KEEP_DOCTYPES`, and spending half this
probe's requests on documents that cannot kill a NOTS would measure nothing. The
83 already in the database are left alone — the tables are append-only.

The rescissions are ALREADY collected; `DETAIL_DOCTYPES` just never fetched their
detail views. The only unknown is what they can be joined ON:

  * If a rescission's detail references the ORIGINAL document number, the join is
    exact and Phase 3 is a small change.
  * If it does not, the only available key is party name plus recording
    sequence — fuzzy, and it will need a measured error rate rather than a
    hopeful one.

This reads candidates out of the existing sjc.db and fetches a handful of detail
views, so it costs about six requests and stores nothing.

    python scripts/probe_rescission_join.py --headed

Exit 0 if an exact join looks possible, 1 if it does not, 2 if the probe could
not run.
"""

import argparse
import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import collect_sjc
from collect_sjc import DOCNUM, HOST, _lines, parse_detail

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "debug" / "rescission_probe"

# Rescissions only. See the module docstring for why Cancellation/Termination
# was dropped from this probe on 2026-07-30.
CANCELLING_TYPES = ("Rescission Of Default",)

# Label text that would indicate an explicit back-reference. Collected from the
# vocabulary Tyler Eagle tends to use; the probe reports ANY label it finds near
# a document number, so this list only drives the summary, never the search.
REFERENCE_HINTS = re.compile(
    r"related|reference|rescind|cancel|terminat|original|prior|affect|"
    r"instrument|book|page|re-?record",
    re.I,
)
BOOK_PAGE = re.compile(r"\bbook\s+(\d+)\s*(?:,|\s)\s*page\s+(\d+)", re.I)


def say(m=""):
    print(m, flush=True)


def dump(name, text):
    OUT.mkdir(parents=True, exist_ok=True)
    p = OUT / name
    p.write_text(text[:400_000], encoding="utf-8")
    return p


def candidates(db_path, per_type):
    """Pull already-collected cancelling documents that have a detail link."""
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        out = []
        for doc_type in CANCELLING_TYPES:
            rows = con.execute(
                "SELECT doc_number, detail_id, recording_date FROM v_latest_index"
                " WHERE doc_type = ? AND detail_id IS NOT NULL"
                " ORDER BY recording_date DESC LIMIT ?",
                (doc_type, per_type),
            ).fetchall()
            out.extend((doc_type, *r) for r in rows)
        return out
    finally:
        con.close()


def known_doc_numbers(db_path):
    """Every document number in the database, with its type — the join target."""
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        return dict(con.execute("SELECT doc_number, doc_type FROM v_latest_index"))
    finally:
        con.close()


def analyse(html, own_doc_number, known):
    """Look for a back-reference in one detail view.

    Returns a dict describing what was found. Deliberately reports the raw
    evidence as well as the conclusion: if the join turns out to be fuzzy, the
    label vocabulary is what the next attempt will need.
    """
    lines = _lines(html)
    text = "\n".join(lines)

    # Any document number that is not this document's own is a candidate
    # reference. The recorder's format (YYYY-NNNNNN) makes these easy to spot.
    found = []
    for m in DOCNUM.finditer(text):
        dn = m.group(1)
        if dn != own_doc_number and dn not in found:
            found.append(dn)

    # Which line each candidate sits on, and the line before it — the label.
    contexts = []
    for i, line in enumerate(lines):
        for dn in found:
            if dn in line:
                contexts.append(
                    {
                        "doc": dn,
                        "label": lines[i - 1] if i else "",
                        "line": line,
                        "looks_like_reference": bool(
                            REFERENCE_HINTS.search(lines[i - 1] if i else "") or REFERENCE_HINTS.search(line)
                        ),
                    }
                )

    return {
        "referenced_docs": found,
        "contexts": contexts,
        "in_our_db": {dn: known.get(dn) for dn in found if dn in known},
        "book_page": BOOK_PAGE.findall(text),
        "reference_labels": [line for line in lines if REFERENCE_HINTS.search(line) and len(line) < 60],
        "parsed": parse_detail(html),
        "lines": lines,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--delay", type=float, default=1.5)
    ap.add_argument("--db", default=str(collect_sjc.DB))
    ap.add_argument("--per-type", type=int, default=3, help="details to fetch per type")
    ap.add_argument(
        "--show-all-lines",
        action="store_true",
        help="print every text line of the first detail — use this to see the full "
        "field vocabulary when no reference is found",
    )
    args = ap.parse_args()

    if collect_sjc.sync_playwright is None:
        say("pip install playwright && python -m playwright install chromium")
        return 2

    db_path = Path(args.db)
    if not db_path.exists():
        say(f"No database at {db_path}. Collect first:")
        say("  python collect_sjc.py --start 2026-06-01 --end 2026-06-30 --headed")
        return 2

    todo = candidates(db_path, args.per_type)
    if not todo:
        say(f"No {' or '.join(CANCELLING_TYPES)} documents with a detail link in {db_path}.")
        say("They arrive with the unfiltered sweep, so collect a month first.")
        return 2

    known = known_doc_numbers(db_path)

    say("=" * 74)
    say("RESCISSION JOIN PROBE — what can a cancelling document be joined ON?")
    say(f"  database : {db_path}  ({len(known)} documents known)")
    say(f"  fetching : {len(todo)} detail views")
    say("=" * 74)

    results = []

    with collect_sjc.sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(collect_sjc.PROFILE),
            headless=not args.headed,
            viewport={"width": 1400, "height": 900},
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(f"{HOST}/Web/", wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(2500)
        if "I Accept" in page.content():
            if not args.headed:
                ctx.close()
                say("Disclaimer/CAPTCHA gate — re-run with --headed.")
                return 2
            input("  >>> Clear the disclaimer/CAPTCHA, then press Enter...\n")

        portal = collect_sjc.Portal(ctx, page, delay=args.delay, debug=False)
        # The detail view is keyed to a search context, so be on the search page.
        portal.on_search_page()

        for doc_type, doc_number, detail_id, rec_date in todo:
            say(f"\n{'-' * 74}\n{doc_type}  {doc_number}  recorded {rec_date}")
            try:
                html = portal.detail(detail_id)
            except Exception as e:
                say(f"  FETCH FAILED: {e}")
                continue
            path = dump(f"{doc_number}.html", html)
            info = analyse(html, doc_number, known)
            results.append((doc_type, doc_number, info))

            say(f"  dump: {path}")
            if info["referenced_docs"]:
                say(f"  OTHER document numbers in this detail: {info['referenced_docs']}")
                for c in info["contexts"]:
                    flag = "  <-- looks like a reference" if c["looks_like_reference"] else ""
                    say(f"    {c['doc']}  label={c['label']!r}{flag}")
                    say(f"      line: {c['line'][:100]!r}")
                if info["in_our_db"]:
                    say("  AND those documents are in our database:")
                    for dn, dt in info["in_our_db"].items():
                        say(f"    {dn} -> {dt}")
                    say("  ^ that is an EXACT JOIN KEY.")
                else:
                    say("  None of them are in our database — they may reference a")
                    say("  Notice of Default (not collected) rather than a NOTS, or a")
                    say("  document outside the collected window.")
            else:
                say("  No other document number anywhere in this detail.")

            if info["book_page"]:
                say(f"  book/page references: {info['book_page']}")
            if info["reference_labels"]:
                say(f"  reference-ish labels present: {info['reference_labels'][:8]}")
            say(f"  parties: grantor={info['parsed']['grantor']} grantee={info['parsed']['grantee']}")

        if args.show_all_lines and results:
            say(f"\n{'=' * 74}\nEVERY TEXT LINE OF THE FIRST DETAIL (field vocabulary)\n{'=' * 74}")
            for line in results[0][2]["lines"]:
                say(f"  {line!r}")

        ctx.close()

    # ── Verdict ─────────────────────────────────────────────────────────
    say("\n" + "=" * 74)
    with_refs = [r for r in results if r[2]["referenced_docs"]]
    with_joinable = [r for r in results if r[2]["in_our_db"]]

    if not results:
        say("VERDICT: nothing fetched — see the errors above.")
        return 2

    say(f"{len(with_refs)}/{len(results)} details carry another document number.")
    say(f"{len(with_joinable)}/{len(results)} reference a document already in our DB.")
    say("")

    if with_joinable:
        say("VERDICT: an EXACT JOIN is possible.")
        say("  Cancelling documents reference the document they act on, and that")
        say("  document is in our dataset. Phase 3 becomes:")
        say("    1. Widen DETAIL_DOCTYPES to include Rescission Of Default")
        say("       (~47 extra detail fetches/month).")
        say("    2. Store the referenced document number — a new column on")
        say("       detail_obs, holding a RAW value, not a derived one.")
        say("    3. Exclude from v_upcoming any NOTS referenced by a later")
        say("       rescission.")
        say("  No fuzzy matching and no error rate to apologise for.")
        return 0

    if with_refs:
        say("VERDICT: references exist but point OUTSIDE the collected data.")
        say("  Most likely they reference the Notice of Default, which is not in")
        say("  KEEP_DOCTYPES (§3 puts it out of scope). Two options:")
        say("    (a) Add 'Default' to KEEP_DOCTYPES — it costs nothing extra, since")
        say("        the sweep already retrieves it (81/mo per §4), and it would")
        say("        make the reference chain resolvable. Note §3 currently lists")
        say("        Default as out of scope, so this is a scope decision, not a")
        say("        mechanical one.")
        say("    (b) Backfill further so the referenced documents fall inside the")
        say("        window — a rescission can post months after the notice.")
        return 1

    say("VERDICT: NO exact join key. The details carry no document reference.")
    say("  Phase 3 has to fall back to party name plus recording sequence:")
    say("  match a cancelling document to the most recent earlier NOTS sharing a")
    say("  grantor, and treat that NOTS as dead.")
    say("  That is FUZZY, so it needs a measured error rate before Section A")
    say("  relies on it — and the party lists mix the homeowner with the")
    say("  foreclosure trustee (gap 10), which will inflate false matches, since")
    say("  one trustee handles many unrelated foreclosures.")
    say("  Re-run with --show-all-lines first: if the detail exposes a field this")
    say("  probe did not recognise, an exact join may still be available.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
