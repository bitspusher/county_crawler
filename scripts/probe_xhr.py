#!/usr/bin/env python3
"""probe_xhr.py — settle the §5.1 automation question, and nothing else.

ROADMAP Phase 1's first item: confirm whether `Portal.xhr` — a `fetch` issued
from inside the page with `X-Requested-With: XMLHttpRequest` — actually returns
JSON and result HTML, or whether Eagle serves the app shell / disclaimer instead.
Everything downstream of collection is blocked on the answer, and the answer
cannot be reached without a browser and a human to clear the CAPTCHA once.

This drives the REAL `collect_sjc.Portal`, not a copy, so a pass here is
evidence about the shipping code path. It performs at most four requests and
stores nothing in the database.

    python scripts/probe_xhr.py --headed

Exit status: 0 if the XHR path works, 1 if it does not (i.e. the UI-automation
fallback is required), 2 if the probe could not run at all.
"""

import argparse
import json
import re
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import collect_sjc
from collect_sjc import DOCTYPES, HOST, KNOWN_IDS, SEARCH_ID, parse_results

OUT = Path("debug/xhr_probe")


def say(msg=""):
    print(msg, flush=True)


def verdict(ok, label, detail=""):
    say(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    if detail:
        for line in detail.strip().splitlines():
            say(f"         {line}")
    return ok


def dump(name, text):
    OUT.mkdir(parents=True, exist_ok=True)
    p = OUT / name
    p.write_text(text[:400_000], encoding="utf-8")
    return p


def looks_like_disclaimer(text):
    return "I Accept" in text or "recaptcha" in text.lower()


def looks_like_app_shell(text):
    """The shell is the SPA wrapper served instead of a fragment.

    Distinguishing feature: it carries a full document with head/script tags but
    none of the result-grid markup the parser keys on.
    """
    has_doc = bool(re.search(r"<html|<head|data-role=\"page\"", text, re.I))
    return has_doc and "ss-search-row" not in text


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--headed", action="store_true", help="show the browser so the CAPTCHA can be cleared by hand")
    ap.add_argument("--delay", type=float, default=1.5, help="seconds between requests — keep it polite (MVP.md §10)")
    ap.add_argument("--types", default="TDUS", help="which doc type to probe with (default TDUS, ~31/mo)")
    args = ap.parse_args()

    if collect_sjc.sync_playwright is None:
        say("playwright is not installed:")
        say("  pip install playwright && python -m playwright install chromium")
        return 2

    label = DOCTYPES.get(args.types.strip(), DOCTYPES["TDUS"])
    # A recently-completed month: recent enough to be populated, old enough to
    # be fully recorded.
    end = date.today().replace(day=1) - timedelta(days=1)
    start = end.replace(day=1)

    say("=" * 72)
    say("XHR PROBE — does Portal.xhr return data, or the app shell?")
    say(f"  doc type : {label}")
    say(f"  window   : {start} .. {end}")
    say(f"  raw dumps: {OUT}/")
    say("=" * 72)

    results = {}

    with collect_sjc.sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(collect_sjc.PROFILE), headless=not args.headed, viewport={"width": 1400, "height": 900}
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(f"{HOST}/Web/", wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(2500)

        say("\n1. Session")
        if "I Accept" in page.content():
            if not args.headed:
                ctx.close()
                say("  [FAIL] disclaimer/CAPTCHA gate, and no display to clear it in.")
                say("         Re-run with --headed.")
                return 2
            input("  >>> Clear the disclaimer/CAPTCHA in the browser, then press Enter...\n")
        results["session"] = verdict(True, "session is past the disclaimer gate")

        portal = collect_sjc.Portal(ctx, page, delay=args.delay, debug=False)

        # ── 2. Does the vocabulary endpoint return JSON? ─────────────────
        say("\n2. Vocabulary endpoint (GET .../documentTypes/...)")
        try:
            raw = portal.xhr(f"{HOST}/Web/search/documentTypes/{SEARCH_ID}?searchText=&maxValues=1000")
        except Exception as e:
            results["vocab_json"] = verdict(False, "request failed", str(e))
            raw = ""
        else:
            path = dump("01_documentTypes.txt", raw)
            try:
                items = json.loads(raw)
            except json.JSONDecodeError:
                head = raw.strip()[:200].replace("\n", " ")
                why = (
                    "served the DISCLAIMER page"
                    if looks_like_disclaimer(raw)
                    else "served the APP SHELL"
                    if looks_like_app_shell(raw)
                    else "not JSON"
                )
                results["vocab_json"] = verdict(
                    False, f"response is not JSON — {why}", f"first bytes: {head}\nfull dump:   {path}"
                )
            else:
                results["vocab_json"] = verdict(True, f"returned JSON ({len(items)} document types)")

                # Settle the key orientation while we are here. doctype_vocab()
                # builds {it["value"]: it["name"]}, but main() then indexes it by
                # LABEL to get an id (`vocab[label]`), the same way KNOWN_IDS is
                # keyed. Exactly one of those readings can be right, and only
                # the live payload says which.
                if items and isinstance(items[0], dict):
                    sample = items[0]
                    say(f"         item keys: {sorted(sample)}")
                    say(f"         first item: {json.dumps(sample)[:160]}")
                    built = {it.get("value"): it.get("name") for it in items if isinstance(it, dict)}
                    label_keyed = label in built
                    id_keyed = KNOWN_IDS.get(label) in built
                    if label_keyed:
                        results["vocab_orientation"] = verdict(
                            True, "doctype_vocab() is keyed by LABEL — main() can index it"
                        )
                    elif id_keyed:
                        results["vocab_orientation"] = verdict(
                            False,
                            "doctype_vocab() is keyed by ID, but main() indexes it by LABEL",
                            "The dict comprehension in doctype_vocab() is inverted for the\n"
                            "way main() uses it: `vocab[label]` will KeyError, or silently\n"
                            "fall back to label mode. Flip it to {name: value}.",
                        )
                    else:
                        results["vocab_orientation"] = verdict(
                            False,
                            f"'{label}' not found under either key",
                            "The county may have renamed the document type. Compare the\n"
                            f"dump at {path} against DOCTYPES / KNOWN_IDS.",
                        )

        # ── 3. Does searchPost + searchResults return a grid? ────────────
        say("\n3. Search path (POST searchPost, GET searchResults?page=1)")
        try:
            form_vocab = {label: KNOWN_IDS[label]} if label in KNOWN_IDS else None
            mode = "id" if form_vocab else "label"
            rows = portal.search(label, start, end, mode=mode, vocab=form_vocab)
        except collect_sjc.ResultCapExceeded as e:
            # A cap is a genuine PASS for this probe: the server understood the
            # query and answered about volume. The window just needs narrowing.
            results["search"] = verdict(
                True,
                "server returned the result-cap message",
                f"{e}\nThe XHR path reached the search engine. Narrow the window and re-run.",
            )
            results["rows"] = True
        except Exception as e:
            results["search"] = verdict(False, "search raised", str(e))
            results["rows"] = False
        else:
            results["search"] = verdict(True, "search completed without raising")
            if rows:
                results["rows"] = verdict(
                    True,
                    f"parsed {len(rows)} rows",
                    "\n".join(
                        f"{r['doc_number']}  {r['recording_date']}  "
                        f"{(r['doc_type'] or '?')[:34]}  page={r.get('page_no')}"
                        for r in rows[:5]
                    ),
                )
            else:
                results["rows"] = verdict(
                    False,
                    "ZERO rows — the blocker is real, or the session died",
                    f"Expected roughly 31/month for {label} (MVP.md §4).",
                )

        # ── 4. Raw grid inspection, whatever the parse said ──────────────
        say("\n4. Raw result markup")
        try:
            raw_grid = portal.xhr(f"{HOST}/Web/searchResults/{SEARCH_ID}?page=1")
        except Exception as e:
            results["grid_shape"] = verdict(False, "could not re-fetch page 1", str(e))
        else:
            path = dump("02_searchResults_p1.html", raw_grid)
            if looks_like_disclaimer(raw_grid):
                results["grid_shape"] = verdict(
                    False, "page 1 is the DISCLAIMER — session is not authenticated", f"dump: {path}"
                )
            elif "ss-search-row" in raw_grid:
                n = len(parse_results(raw_grid))
                results["grid_shape"] = verdict(
                    True, f"page 1 carries ss-search-row markup ({n} parsed)", f"dump: {path}"
                )
            elif looks_like_app_shell(raw_grid):
                results["grid_shape"] = verdict(
                    False,
                    "page 1 is the APP SHELL, not a result fragment",
                    "This is the documented failure mode: header emulation is not\n"
                    "enough and Eagle is rendering the SPA wrapper. The UI-automation\n"
                    f"fallback is required (ROADMAP Phase 1).\ndump: {path}",
                )
            else:
                results["grid_shape"] = verdict(
                    False, "page 1 is neither a grid nor a recognisable shell", f"Inspect it by hand: {path}"
                )

        ctx.close()

    # ── Verdict ─────────────────────────────────────────────────────────
    say("\n" + "=" * 72)
    core = ["vocab_json", "search", "rows", "grid_shape"]
    ok = all(results.get(k) for k in core)
    if ok:
        say("VERDICT: the XHR path WORKS.")
        say("  Portal.xhr returns JSON and real result markup. ROADMAP Phase 1's")
        say("  automation question is answered — no UI-automation fallback needed.")
        say("  Next: capture fixtures (scripts/capture_fixtures.py --headed), then")
        say("  run one TDUS month with details for Phase 2.")
    else:
        failed = [k for k in core if not results.get(k)]
        say(f"VERDICT: the XHR path DOES NOT work. Failing checks: {', '.join(failed)}")
        say("  Switch to UI automation: fill the form, click Search, read the DOM,")
        say("  page through. ~31 records/month makes the slow path affordable")
        say("  (MVP.md §5.1, §11.1).")
        say(f"  Read the dumps in {OUT}/ before changing any code.")
    if results.get("vocab_orientation") is False:
        say("\nALSO: doctype_vocab()'s key orientation is wrong for main()'s usage.")
        say("  Fix that regardless of the verdict above — see check 2.")
    say("=" * 72)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
