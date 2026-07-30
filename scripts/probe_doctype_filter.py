#!/usr/bin/env python3
"""probe_doctype_filter.py — find the value format the doc-type field accepts.

The 2026-07-29 probe run established that the transport works and the search is
simply unfiltered: `field_selfservice_documentTypes` exists on the form as a
**text input** (not a select) with a hidden `-containsInput` companion, so it is
an autocomplete widget, and the collector was posting the numeric id `22` into it.

The searchPost response makes this cheap to settle. It returns JSON:

    {"validationMessages":{},"totalPages":14,"currentPage":1}

`totalPages` is the size of the result set, reported **before** any page is
fetched. So each candidate value format costs exactly ONE request, and the
success criterion is unambiguous: over 2026-06-01..03 the county recorded 1323
documents in total (14 pages) but only 5 Trustees Deed Under Default (1 page). A
variant that yields 1 page filtered; a variant that yields 14 did not.

Only the doc-type value varies between variants — the date range and every other
field stay fixed at the values already known to produce 14 pages. One variable at
a time.

    python scripts/probe_doctype_filter.py --headed

Roughly a dozen requests at --delay spacing, and it fetches at most one results
page. Stores nothing in the database.
"""

import argparse
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import collect_sjc
from collect_sjc import DOCTYPES, HOST, KNOWN_IDS, SEARCH_ID, parse_results

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "debug" / "doctype_probe"

DOCTYPE_FIELD = "field_selfservice_documentTypes"
CONTAINS_FIELD = "field_selfservice_documentTypes-containsInput"


def say(m=""):
    print(m, flush=True)


def dump(name, text):
    OUT.mkdir(parents=True, exist_ok=True)
    p = OUT / name
    p.write_text(text[:400_000], encoding="utf-8")
    return p


def base_form(start, end):
    """The form the collector posts today, known to yield an unfiltered result."""
    return {
        "field_DocNumID": "",
        "field_RecDateID_DOT_StartDate": start.strftime("%m/%d/%Y"),
        "field_RecDateID_DOT_EndDate": end.strftime("%m/%d/%Y"),
        "field_BothNamesID-containsInput": "Contains Any",
        "field_BothNamesID": "",
        "field_GrantorID-containsInput": "Contains Any",
        "field_GrantorID": "",
        "field_GranteeID-containsInput": "Contains Any",
        "field_GranteeID": "",
        CONTAINS_FIELD: "Contains Any",
        DOCTYPE_FIELD: "",
        "field_UseAdvancedSearch": "",
    }


def post_search(portal, form):
    """POST the search and return (total_pages, validation_messages, raw).

    total_pages is None when the response is not the expected JSON.
    """
    raw = portal.xhr(f"{HOST}/Web/searchPost/{SEARCH_ID}", method="POST", form=form)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None, None, raw
    return data.get("totalPages"), data.get("validationMessages"), raw


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--delay", type=float, default=1.5)
    # The default window is the one already measured: 1323 documents total,
    # 5 of them TDUS. Both numbers are needed to read the result.
    ap.add_argument("--start", default="2026-06-01")
    ap.add_argument("--end", default="2026-06-03")
    ap.add_argument("--types", default="TDUS")
    ap.add_argument(
        "--verify", action="store_true", help="fetch page 1 for the winning variant and show the doc-type mix"
    )
    ap.add_argument(
        "--manual",
        action="store_true",
        help="ALWAYS pause so you can fill the doc-type field in the real UI, then "
        "read back exactly what the widget put in the form. This beats guessing: "
        "whatever the autocomplete produces IS the accepted format.",
    )
    args = ap.parse_args()

    if collect_sjc.sync_playwright is None:
        say("pip install playwright && python -m playwright install chromium")
        return 2

    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    label = DOCTYPES.get(args.types.strip(), DOCTYPES["TDUS"])
    doc_id = KNOWN_IDS.get(label, "22")
    other_label = DOCTYPES["NOTS"] if label == DOCTYPES["TDUS"] else DOCTYPES["TDUS"]
    other_id = KNOWN_IDS.get(other_label, "41")

    # Candidate encodings for an autocomplete text field. Each entry is
    # (name, doctype_value, contains_value_or_None_to_omit_the_field).
    variants = [
        ("baseline_no_doctype", "", "Contains Any"),
        ("id_only", doc_id, None),
        ("id_plus_contains", doc_id, "Contains Any"),
        ("label_only", label, None),
        ("label_plus_contains", label, "Contains Any"),
        ("label_contains_all", label, "Contains All"),
        ("label_exact", label, "Exact Match"),
        ("id_bracketed", f"[{doc_id}]", "Contains Any"),
        ("label_bracketed", f"[{label}]", "Contains Any"),
        ("two_ids_comma", f"{doc_id},{other_id}", "Contains Any"),
        ("two_labels_comma", f"{label},{other_label}", "Contains Any"),
        ("two_labels_semicolon", f"{label};{other_label}", "Contains Any"),
    ]

    say("=" * 74)
    say("DOC-TYPE FILTER PROBE — which value format does the field accept?")
    say(f"  window : {start} .. {end}")
    say(f"  target : {label}  (id {doc_id})")
    say("  reading totalPages from the searchPost JSON — one request per variant")
    say("  Over this window the county recorded 1323 documents (14 pages) and")
    say(f"  only 5 '{label}' (1 page). 14 pages => the filter was ignored.")
    say("=" * 74)

    rows = []

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
            input("  >>> Clear the disclaimer/CAPTCHA, then press Enter...\n")

        portal = collect_sjc.Portal(ctx, page, delay=args.delay, debug=False)

        # What the form actually exposes, so a variant can be built from real
        # control names rather than the collector's inherited list.
        portal.on_search_page()
        live = page.evaluate(
            "() => Array.from(document.querySelectorAll('input[name],select[name],textarea[name]')).map(e => e.name)"
        )
        dump("00_live_controls.json", json.dumps(sorted(set(live)), indent=2))
        say(f"\nlive form controls ({len(set(live))}): {sorted(set(live))}")

        # ── Manual capture: let the widget tell us the format ────────────
        # The pause is UNCONDITIONAL here. The CAPTCHA prompt earlier only fires
        # when the disclaimer is actually present, so on an already-cleared
        # profile the script blows straight past it and there is no opportunity
        # to touch the page.
        if args.manual:
            say("\n" + "=" * 74)
            say("MANUAL CAPTURE")
            say("=" * 74)
            say("In the browser window:")
            say(f"  1. Set the document type to '{label}' using the field's own")
            say("     autocomplete — click it, type, and pick from the dropdown so the")
            say("     widget populates the field the way the app does.")
            say(f"  2. Set the recording-date range to {start:%m/%d/%Y} .. {end:%m/%d/%Y}.")
            say("  3. Do NOT click Search — the point is to read the form, not run it.")
            input("\n  >>> Press Enter once the doc-type field is filled in...\n")

            snapshot = page.evaluate("""() => {
                const out = {};
                document.querySelectorAll('input[name],select[name],textarea[name]').forEach(el => {
                    out[el.name] = el.value;
                });
                return out;
            }""")
            dump("01_manual_form_values.json", json.dumps(snapshot, indent=2))
            say("  form values after your interaction:")
            for k, v in sorted(snapshot.items()):
                marker = "  <<<" if "documentTypes" in k else ""
                say(f"    {k} = {v!r}{marker}")

            captured = snapshot.get(DOCTYPE_FIELD, "")
            captured_contains = snapshot.get(CONTAINS_FIELD)
            if captured:
                say(f"\n  CAPTURED doc-type value: {captured!r}")
                say(f"  CAPTURED contains value:  {captured_contains!r}")
                # Try it immediately — this is the highest-prior candidate by far.
                form = base_form(start, end)
                form[DOCTYPE_FIELD] = captured
                if captured_contains is not None:
                    form[CONTAINS_FIELD] = captured_contains
                try:
                    pages, msgs, raw = post_search(portal, form)
                except Exception as e:
                    say(f"  POST with the captured value FAILED: {e}")
                else:
                    dump("01_manual_post.json", raw)
                    say(f"  POST with the captured value -> totalPages={pages}  msgs={msgs}")
                    if pages is not None and pages < 14:
                        say("  THAT IS THE ANSWER. Use this encoding in Portal.search.")
                    else:
                        say("  Still unfiltered. The widget's value alone is not sufficient —")
                        say("  the server may key the filter on something not in the form at")
                        say("  all (a session-side selection set). Continuing to the matrix.")
                    rows.append(("MANUAL_CAPTURED", pages, captured, captured_contains))
            else:
                say("\n  The doc-type field came back EMPTY after your interaction.")
                say("  That is itself informative: the visible control is not where the")
                say("  selection is stored, so the widget keeps it somewhere the form does")
                say("  not expose — which is why posting the field can never filter.")
                say("  Option (b), the unfiltered sweep, is then the correct design.")
        posted = set(base_form(start, end))
        phantom = sorted(posted - set(live))
        if phantom:
            say(f"\nfields the collector posts that are NOT on the form ({len(phantom)}):")
            for f in phantom:
                say(f"  {f}")
            say("  These are inherited from probe_eagle.py's S6 basic form. The server")
            say("  ignores unknown fields silently, so they are noise rather than the")
            say("  cause — but the date range demonstrably works, so at least the date")
            say("  fields are understood despite not appearing in this DOM snapshot")
            say("  (the S7 form renders panels lazily).")

        say("\n" + "-" * 74)
        say(f"{'variant':24s} {'totalPages':>10s}  {'filtered?':>9s}  validationMessages")
        say("-" * 74)

        for name, dt_value, contains in variants:
            form = base_form(start, end)
            form[DOCTYPE_FIELD] = dt_value
            if contains is None:
                form.pop(CONTAINS_FIELD, None)
            else:
                form[CONTAINS_FIELD] = contains
            try:
                pages, msgs, raw = post_search(portal, form)
            except Exception as e:
                say(f"{name:24s} {'ERROR':>10s}  {'-':>9s}  {e}")
                rows.append((name, None, dt_value, contains))
                continue
            dump(f"{name}.json", raw)
            if pages is None:
                say(f"{name:24s} {'not JSON':>10s}  {'-':>9s}  (see dump)")
            else:
                # 14 pages is the unfiltered total for this window; anything
                # materially smaller means the doc-type field was honoured.
                filtered = "YES" if pages < 14 else "no"
                msg = "" if msgs in ({}, None) else json.dumps(msgs)[:40]
                say(f"{name:24s} {pages:>10d}  {filtered:>9s}  {msg}")
            rows.append((name, pages, dt_value, contains))

        say("-" * 74)

        # ── Interpretation ──────────────────────────────────────────────
        baseline = next((r[1] for r in rows if r[0] == "baseline_no_doctype"), None)
        winners = [
            r
            for r in rows
            if r[1] is not None and baseline is not None and r[1] < baseline and r[0] != "baseline_no_doctype"
        ]

        say("")
        if not winners:
            say("RESULT: no variant filtered. Every encoding returned the unfiltered")
            say("  total, so the doc-type field is not being applied in any form this")
            say("  probe tried.")
            say("")
            say("  Take option (b): collect UNFILTERED over narrow windows and filter")
            say("  by doc_type client-side. The 2026-07-29 run proves that works — it")
            say("  parsed 1323 rows across 60 types, including 5 TDUS, 10 NOTS, 5")
            say("  Rescission Of Default and 16 Cancellation/Termination. That closes")
            say("  ROADMAP Phase 3 as a side effect, since rescissions and")
            say("  cancellations arrive in the same pass.")
            say("")
            say("  Cost: about 130 pages/month at ~100 rows/page. At the default 1.5s")
            say("  delay that is roughly 3 minutes per month collected — affordable,")
            say("  and it stays within the politeness constraints of MVP.md §10.")
            say("  Use totalPages from the POST to size each window BEFORE walking it.")
        else:
            best = min(winners, key=lambda r: r[1])
            say(f"RESULT: {len(winners)} variant(s) filtered. Best: {best[0]} -> {best[1]} page(s)")
            for name, pages, dt_value, contains in winners:
                say(
                    f"  {name:24s} {pages:>3d} pages   {DOCTYPE_FIELD}={dt_value!r}"
                    + (f"  {CONTAINS_FIELD}={contains!r}" if contains else "  (no contains field)")
                )
            say("")
            say("  Take option (a): set the doc-type value this way in Portal.search.")
            say("  One page per month per type instead of ~130 pages per month.")

            if args.verify:
                say("\nVerifying the winning variant by fetching page 1...")
                form = base_form(start, end)
                form[DOCTYPE_FIELD] = best[2]
                if best[3] is None:
                    form.pop(CONTAINS_FIELD, None)
                else:
                    form[CONTAINS_FIELD] = best[3]
                post_search(portal, form)
                html = portal.xhr(f"{HOST}/Web/searchResults/{SEARCH_ID}?page=1")
                dump("verify_page1.html", html)
                got = parse_results(html)
                mix = {}
                for r in got:
                    mix[r["doc_type"] or "?"] = mix.get(r["doc_type"] or "?", 0) + 1
                say(f"  {len(got)} rows, doc-type mix: {mix}")
                if set(mix) <= {label}:
                    say(f"  CONFIRMED: only '{label}' came back. The filter works.")
                else:
                    say("  NOT CONFIRMED: other doc types are still present, so a lower")
                    say("  totalPages did not actually mean a doc-type filter — it may")
                    say("  have narrowed on something else. Do not trust this variant.")

        say("")
        say(f"dumps: {OUT}/")
        ctx.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
