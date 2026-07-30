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

Exit status (updated after the 2026-07-29 settlement):
  0 — healthy. Either the full pass (transport works AND the doc-type filter is
      honored), or the SETTLED state: transport works and the filter is
      server-ignored, which is the architecture collection now runs on.
  1 — the transport itself is broken (POST rejected, no grid markup): a real
      regression. THIS is the exit that means the portal changed.
  2 — the probe could not run at all.
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

ROOT = Path(__file__).resolve().parent.parent
# Repo-relative, NOT cwd-relative: running this as `python3 probe_xhr.py` from
# inside scripts/ used to scatter the dumps into scripts/debug/, where nobody
# looks for them.
OUT = ROOT / "debug" / "xhr_probe"


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


CAP_MESSAGE = "more documents than the maximum allowed"


def classify_body(text):
    """Name the shape of a response body.

    The earlier version knew only 'grid' and 'app shell', so a legitimate
    cap-message FRAGMENT — no <html>, no ss-search-row — matched neither and got
    reported as an unrecognisable failure. It is in fact proof the request
    reached the search engine.
    """
    if not text.strip():
        return "empty"
    if looks_like_disclaimer(text):
        return "disclaimer"
    if CAP_MESSAGE in text:
        return "cap"
    if "ss-search-row" in text:
        return "grid"
    if looks_like_app_shell(text):
        return "shell"
    return "unknown"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--headed", action="store_true", help="show the browser so the CAPTCHA can be cleared by hand")
    ap.add_argument("--delay", type=float, default=1.5, help="seconds between requests — keep it polite (MVP.md §10)")
    ap.add_argument("--types", default="TDUS", help="which doc type to probe with (default TDUS, ~31/mo)")
    ap.add_argument("--start", help="window start YYYY-MM-DD (default: first day of last month)")
    ap.add_argument("--end", help="window end YYYY-MM-DD (default: last day of last month)")
    args = ap.parse_args()

    if collect_sjc.sync_playwright is None:
        say("playwright is not installed:")
        say("  pip install playwright && python -m playwright install chromium")
        return 2

    label = DOCTYPES.get(args.types.strip(), DOCTYPES["TDUS"])
    # A recently-completed month: recent enough to be populated, old enough to
    # be fully recorded. Overridable, because a narrow window is the cleanest
    # test of whether the doc-type filter is applied at all — see check 4.
    end = date.fromisoformat(args.end) if args.end else date.today().replace(day=1) - timedelta(days=1)
    start = date.fromisoformat(args.start) if args.start else end.replace(day=1)

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

        # ── 2. What does the form ACTUALLY post? ─────────────────────────
        # The decisive check once the search path is known to work but a filter
        # is being dropped: read the real control names off the live DOM instead
        # of trusting the field names the collector was written against. A wrong
        # field name is silently ignored by the server — no error, just an
        # unfiltered search.
        say("\n2. Search form controls (what field names does the form really use?)")
        try:
            portal.on_search_page()
            fields = page.evaluate("""() => {
                const out = [];
                document.querySelectorAll('input[name], select[name], textarea[name]').forEach(el => {
                    const rec = {name: el.name, tag: el.tagName.toLowerCase(), type: el.type || null};
                    if (el.tagName.toLowerCase() === 'select') {
                        rec.optionCount = el.options.length;
                        rec.sampleOptions = Array.from(el.options).slice(0, 6)
                            .map(o => ({value: o.value, text: o.text}));
                    }
                    out.push(rec);
                });
                return out;
            }""")
        except Exception as e:
            results["form_fields"] = verdict(False, "could not read the form DOM", str(e))
        else:
            path = dump("00_form_fields.json", json.dumps(fields, indent=2))
            posted = set(collect_sjc.Portal.SEARCH_FIELDS) if hasattr(collect_sjc.Portal, "SEARCH_FIELDS") else set()
            live = {f["name"] for f in fields}
            doctype_controls = [f for f in fields if re.search(r"doc.*type|type.*doc", f["name"], re.I)]

            detail = [f"{len(fields)} named controls; dump: {path}"]
            if doctype_controls:
                detail.append("doc-type control(s) on the live form:")
                for f in doctype_controls:
                    detail.append(f"  name={f['name']!r}  tag={f['tag']}  type={f['type']}")
                    if f.get("sampleOptions"):
                        detail.append(f"    {f['optionCount']} options, first few:")
                        for o in f["sampleOptions"]:
                            detail.append(f"      value={o['value']!r}  text={o['text']!r}")
            else:
                detail.append("NO control matching /doc.*type/ found — the doc-type")
                detail.append("selector may be built by JS rather than being a form field.")

            # The name the collector posts, against what the form exposes.
            COLLECTOR_FIELD = "field_selfservice_documentTypes"
            if COLLECTOR_FIELD in live:
                detail.append(f"\n{COLLECTOR_FIELD!r} EXISTS on the form —")
                detail.append("the name is right, so the VALUE format is what the server rejects.")
            else:
                detail.append(f"\n{COLLECTOR_FIELD!r} is NOT on the form.")
                detail.append("That is why the filter is dropped: the collector posts a field")
                detail.append("name the server does not recognise, and it is ignored silently.")
            if posted:
                detail.append(f"unrecognised names the collector posts: {sorted(posted - live)}")

            results["form_fields"] = verdict(COLLECTOR_FIELD in live, "read the live form controls", "\n".join(detail))

        # ── 3. Does the vocabulary endpoint return JSON? ─────────────────
        say("\n3. Vocabulary endpoint (GET .../documentTypes/...)")
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

        # ── 3. Does the POST that sets search state actually take? ────────
        # Checked on its OWN, before any search. If the POST is silently
        # rejected, every later request answers a query that was never set — and
        # the earlier version of this probe could not see that, because it only
        # looked at what came back from searchResults.
        say("\n4. Search state (POST searchPost) — does it take?")
        form_vocab = {label: KNOWN_IDS[label]} if label in KNOWN_IDS else None
        mode = "id" if form_vocab else "label"
        dt = KNOWN_IDS[label] if form_vocab else label
        form = {
            "field_DocNumID": "",
            "field_RecDateID_DOT_StartDate": start.strftime("%m/%d/%Y"),
            "field_RecDateID_DOT_EndDate": end.strftime("%m/%d/%Y"),
            "field_BothNamesID-containsInput": "Contains Any",
            "field_BothNamesID": "",
            "field_GrantorID-containsInput": "Contains Any",
            "field_GrantorID": "",
            "field_GranteeID-containsInput": "Contains Any",
            "field_GranteeID": "",
            "field_selfservice_documentTypes-containsInput": "Contains Any",
            "field_selfservice_documentTypes": dt,
            "field_UseAdvancedSearch": "",
        }
        try:
            post_body = portal.xhr(f"{HOST}/Web/searchPost/{SEARCH_ID}", method="POST", form=form)
        except collect_sjc.UnexpectedResponse as e:
            results["search_post"] = verdict(
                False,
                "the POST was REJECTED — it did nothing",
                f"{e}\n"
                "This is the root failure. Search state is never set, so anything\n"
                "fetched afterwards answers a DIFFERENT query than the one asked.",
            )
        except Exception as e:
            results["search_post"] = verdict(False, "POST raised", str(e))
        else:
            path = dump("02_searchPost.txt", post_body)
            kind = classify_body(post_body)
            detail = [f"dump: {path}"]
            # Show a short body inline. This response is the server's only word on
            # whether it accepted the query, and having to go open a 57-byte file
            # to read it wasted a round trip.
            if len(post_body) <= 500:
                detail.append(f"body: {post_body.strip()!r}")
            results["search_post"] = verdict(
                kind not in ("shell", "disclaimer"),
                f"POST accepted (response shape: {kind}, {len(post_body)} bytes)",
                "\n".join(detail),
            )

        # ── 4. Does the results page carry the query we asked for? ────────
        say("\n5. Results (GET searchResults?page=1)")
        try:
            rows = portal.search(label, start, end, mode=mode, vocab=form_vocab)
        except collect_sjc.ResultCapExceeded as e:
            # NOT a pass. TDUS runs ~31/month (§4), so a single month cannot
            # legitimately cap. A cap here means the doc-type filter was not
            # applied and the search matched everything recorded in the window.
            results["rows"] = verdict(
                False,
                "RESULT CAP on a window that cannot legitimately cap",
                f"{e}\n"
                f"'{label}' runs about 31 documents/month (MVP.md §4), so hitting\n"
                "the cap means the doc-type filter was NOT applied and the search\n"
                "matched every document recorded in the window. Consistent with a\n"
                "silently-rejected POST above.\n"
                "Confirm with a narrow window: --start/--end over 2-3 days. If the\n"
                "rows come back carrying MANY doc types, the filter is being ignored.",
            )
        except collect_sjc.UnexpectedResponse as e:
            results["rows"] = verdict(False, "results request was rejected", str(e))
        except Exception as e:
            results["rows"] = verdict(False, "search raised", str(e))
        else:
            if rows:
                types = {}
                for r in rows:
                    types[r["doc_type"] or "?"] = types.get(r["doc_type"] or "?", 0) + 1
                only_wanted = set(types) <= {label}
                results["rows"] = verdict(
                    only_wanted,
                    f"parsed {len(rows)} rows across {len(types)} doc type(s)",
                    "\n".join(
                        f"{r['doc_number']}  {r['recording_date']}  "
                        f"{(r['doc_type'] or '?')[:34]}  page={r.get('page_no')}"
                        for r in rows[:5]
                    )
                    + (
                        ""
                        if only_wanted
                        else f"\n\nDOC TYPE MIX: {types}\n"
                        f"More than '{label}' came back — the doc-type filter is\n"
                        "being IGNORED. The search is unfiltered."
                    ),
                )
            else:
                results["rows"] = verdict(
                    False,
                    "ZERO rows — the blocker is real, or the session died",
                    f"Expected roughly 31/month for {label} (MVP.md §4).",
                )

        # ── 5. Raw markup, whatever the parse said ───────────────────────
        say("\n6. Raw result markup")
        try:
            raw_grid = portal.xhr(f"{HOST}/Web/searchResults/{SEARCH_ID}?page=1")
        except collect_sjc.UnexpectedResponse as e:
            results["grid_shape"] = verdict(False, "results request was rejected", str(e))
            raw_grid = None
        except Exception as e:
            results["grid_shape"] = verdict(False, "could not re-fetch page 1", str(e))
            raw_grid = None
        if raw_grid is not None:
            path = dump("03_searchResults_p1.html", raw_grid)
            kind = classify_body(raw_grid)
            if kind == "grid":
                n = len(parse_results(raw_grid))
                results["grid_shape"] = verdict(
                    True, f"page 1 carries ss-search-row markup ({n} parsed)", f"dump: {path}"
                )
            elif kind == "cap":
                results["grid_shape"] = verdict(
                    False,
                    "page 1 is the RESULT-CAP message, not a grid",
                    "The request DID reach the search engine — this is a real server\n"
                    "response, not the shell. But the window returned no usable rows.\n"
                    f"dump: {path}",
                )
            elif kind == "disclaimer":
                results["grid_shape"] = verdict(
                    False, "page 1 is the DISCLAIMER — session is not authenticated", f"dump: {path}"
                )
            elif kind == "shell":
                results["grid_shape"] = verdict(
                    False,
                    "page 1 is the APP SHELL, not a result fragment",
                    "The documented failure mode: header emulation is not enough and\n"
                    "Eagle is rendering the SPA wrapper. The UI-automation fallback\n"
                    f"is required (ROADMAP Phase 1).\ndump: {path}",
                )
            else:
                results["grid_shape"] = verdict(
                    False, f"page 1 shape is '{kind}' — unrecognised", f"Inspect it by hand: {path}"
                )

        ctx.close()

    # ── Verdict ─────────────────────────────────────────────────────────
    say("\n" + "=" * 72)
    core = ["vocab_json", "search_post", "rows", "grid_shape"]
    ok = all(results.get(k) for k in core)
    # The transport working and the QUERY being honoured are separate questions,
    # and conflating them produced a misleading "switch to UI automation" verdict
    # when in fact the grid was parsing fine and only one field was being dropped.
    transport_works = bool(results.get("grid_shape")) and bool(results.get("search_post"))
    if ok:
        say("VERDICT: the XHR path WORKS.")
        say("  Portal.xhr returns JSON and real result markup, the POST takes, and")
        say("  the doc-type filter is applied. ROADMAP Phase 1's automation")
        say("  question is answered — no UI-automation fallback needed.")
        say("  Next: capture fixtures (scripts/capture_fixtures.py --headed), then")
        say("  run one TDUS month with details for Phase 2.")
    elif transport_works and results.get("rows") is False:
        say("VERDICT: the transport WORKS; the doc-type FILTER does not.")
        say("  This is the SETTLED, healthy state (2026-07-29) — collection runs")
        say("  unfiltered sweeps with client-side selection, so nothing has")
        say("  regressed. Exit 0.")
        say("  searchResults returns real, parseable grid markup and pagination")
        say("  terminates cleanly, so no UI-automation rewrite is needed. What fails")
        say("  is narrower: the doc-type field is dropped, so the search is")
        say("  unfiltered and the date range is doing all the work.")
        say("  Two ways forward, and check 2 above decides between them:")
        say("    (a) Fix the field. If the form exposes a different control name or")
        say("        value format, use it — one page per month instead of ~130.")
        say("    (b) Collect unfiltered over narrow windows and filter by doc_type")
        say("        client-side. Costs far more requests, but yields EVERY doc type")
        say("        in one pass — including the rescissions and cancellations of")
        say("        ROADMAP Phase 3, at no extra cost.")
    else:
        failed = [k for k in core if not results.get(k)]
        say(f"VERDICT: the XHR path DOES NOT work. Failing checks: {', '.join(failed)}")
        if results.get("search_post") is False:
            say("  ROOT CAUSE: the searchPost is silently rejected, so search state is")
            say("  never set and every later request answers a query nobody asked for.")
            say("  Header emulation is insufficient. Switch to UI automation: fill the")
            say("  form, click Search, read the DOM, page through. ~31 records/month")
            say("  makes the slow path affordable (MVP.md §5.1, §11.1).")
        elif results.get("grid_shape") is False:
            say("  The results endpoint does not return a usable grid. Switch to UI")
            say("  automation: fill the form, click Search, read the DOM, page")
            say("  through (MVP.md §5.1, §11.1).")
        else:
            say("  See the failing checks above before changing any code.")
        say(f"  Read the dumps in {OUT}/ before changing any code.")
    if results.get("vocab_orientation") is False:
        say("\nALSO: doctype_vocab()'s key orientation is wrong for main()'s usage.")
        say("  Fix that regardless of the verdict above — see check 2.")
    say("=" * 72)
    if ok or (transport_works and results.get("rows") is False):
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
