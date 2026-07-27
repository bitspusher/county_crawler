#!/usr/bin/env python3
"""
probe_eagle2.py — Section 11 probe, v2.

What v1 established (do not re-derive):
  Q2 SOLVED. POST https://sanjoaquincountyca-web.tylerhost.net/Web/searchPost/DOCSEARCH3032S6
     params: field_DocNumID, field_RecDateID_DOT_StartDate, field_RecDateID_DOT_EndDate,
             field_BothNamesID(+ -containsInput), field_GrantorID, field_GranteeID,
             field_selfservice_documentTypes(+ -containsInput), field_UseAdvancedSearch
     CAPTCHA posts to /Web/checkHuman; /Web/subscription/getSubscriptionTime is the
     session heartbeat (this is why wait_for_load_state('networkidle') never returns).

Still open:
  Q1 doc-type codes — the picker is NOT a <select>; it's an autocomplete/token widget.
  Q3 whether APN/Parcel # appears in index results. Help text mentions a "Parcel #"
     field, and field_UseAdvancedSearch was empty, so check Advanced Search.

Run this with --manual-nav and drive the browser yourself. The script harvests
whatever page you leave it on and records every XHR body along the way.

    python probe_eagle2.py --headed --slow --manual-nav
"""

import argparse
import json
import re
import sys
from datetime import date, timedelta
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sys.exit("pip install playwright && python -m playwright install chromium")

BASE = "https://sanjoaquincountyca-web.tylerhost.net/Web/"
OUT = Path("probe_out2")
PROFILE = Path("./.browser_profile").resolve()

PATTERNS = {
    "NOTS": re.compile(r"(NOT.{0,3}CE.{0,3}(OF.{0,3})?TRUSTEE|TRUSTEE.{0,3}S?.{0,3}SALE|\bNTS\b|\bNOS\b)", re.I),
    "TDUS": re.compile(r"(TRUSTEE.{0,3}S?.{0,3}DEED|\bTDUS\b|\bTDS\b|DEED.{0,10}UPON.{0,3}SALE)", re.I),
    "NOD":  re.compile(r"(NOT.{0,3}CE.{0,3}(OF.{0,3})?DEFAULT|\bNOD\b)", re.I),
    "RESCIND": re.compile(r"(RESCI|CANCEL|REVOK|WITHDRAW)", re.I),
}
APN_HEADER = re.compile(r"(\bAPN\b|PARCEL|ASSESSOR)", re.I)
APN_VALUE = re.compile(r"\b\d{3}-\d{3}-\d{2,3}(-\d{3})?\b")


def log(m): print(f"[probe] {m}", flush=True)


def snap(page, name):
    try:
        page.screenshot(path=str(OUT / f"{name}.png"), full_page=True)
        (OUT / f"{name}.html").write_text(page.content(), encoding="utf-8")
        log(f"  saved snapshot: {name}")
    except Exception as e:
        log(f"  (snapshot {name} failed: {e})")


def harvest(page, tag):
    """Dump every select, every table, and scan for APN. Works on any page."""
    data = page.evaluate("""() => {
        const selects = Array.from(document.querySelectorAll('select')).map(s => ({
            name: s.name || s.id || null, multiple: s.multiple,
            option_count: s.options.length,
            options: Array.from(s.options).map(o => ({value: o.value, text: o.text.trim()}))
        }));
        // Token/autocomplete widgets: grab anything that looks like a rendered option list.
        const listish = Array.from(document.querySelectorAll(
            '[role=option],[role=listbox] li,.ui-menu-item,.token,.chip,.selectedItem,datalist option'
        )).map(e => e.innerText.trim()).filter(Boolean).slice(0, 400);
        const tables = Array.from(document.querySelectorAll('table')).map(t => ({
            headers: Array.from(t.querySelectorAll('thead th, thead td, tr:first-child th'))
                          .map(c => c.innerText.trim()).filter(Boolean),
            row_count: t.querySelectorAll('tbody tr').length,
            sample_rows: Array.from(t.querySelectorAll('tbody tr')).slice(0,5)
                              .map(r => Array.from(r.querySelectorAll('td')).map(c => c.innerText.trim()))
        })).filter(t => t.headers.length || t.sample_rows.length);
        const fields = Array.from(document.querySelectorAll('input,select,textarea'))
            .map(e => ({tag: e.tagName.toLowerCase(), type: e.type||null,
                        name: e.name||null, id: e.id||null, placeholder: e.placeholder||null}));
        return {selects, listish, tables, fields};
    }""")
    (OUT / f"harvest_{tag}.json").write_text(json.dumps(data, indent=2))

    # Q1
    pool = []
    for s in data["selects"]:
        if s["option_count"] >= 2:
            pool += [(s["name"], o["value"], o["text"]) for o in s["options"]]
    pool += [(None, None, x) for x in data["listish"]]
    hits = {k: [] for k in PATTERNS}
    for src, val, txt in pool:
        for k, p in PATTERNS.items():
            if p.search(txt or "") or p.search(val or ""):
                hits[k].append({"source": src, "code": val, "label": txt})
    for k, v in hits.items():
        if v:
            log(f"  Q1 {k}: {len(v)} candidate(s)")
            for h in v[:8]:
                log(f"       {h['code']!r} = {h['label']!r}")

    # Q3
    verdict = "NO"
    for t in data["tables"]:
        if any(APN_HEADER.search(h) for h in t["headers"]):
            verdict = "YES (column header)"
        for r in t["sample_rows"]:
            if any(APN_VALUE.search(c) for c in r):
                verdict = "YES (value in row)"
    log(f"  Q3 [{tag}] APN present: {verdict}")
    for t in data["tables"]:
        if t["headers"]:
            log(f"       grid headers ({t['row_count']} rows): {t['headers']}")
    if any(f["name"] and re.search(r"parcel|apn", f["name"], re.I) for f in data["fields"]):
        log("       NOTE: a Parcel/APN *input field* exists on this page")
    return hits, verdict


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--slow", action="store_true")
    ap.add_argument("--manual-nav", action="store_true",
                    help="pause and let a human drive to the right page")
    ap.add_argument("--chrome", action="store_true", help="use installed Chrome")
    args = ap.parse_args()
    OUT.mkdir(exist_ok=True)
    calls = []

    with sync_playwright() as p:
        kw = dict(user_data_dir=str(PROFILE), headless=not args.headed,
                  slow_mo=400 if args.slow else 0,
                  record_har_path=str(OUT / "network.har"),
                  viewport={"width": 1500, "height": 1000})
        if args.chrome:
            kw["channel"] = "chrome"
        ctx = p.chromium.launch_persistent_context(**kw)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        def on_req(r):
            if r.resource_type not in ("document", "xhr", "fetch"):
                return
            try:
                buf = r.post_data_buffer
                body = buf.decode("utf-8", errors="replace")[:3000] if buf else ""
            except Exception as e:
                body = f"<unreadable {e}>"
            calls.append({"dir": "req", "method": r.method, "url": r.url, "body": body})

        def on_resp(r):
            if r.request.resource_type not in ("xhr", "fetch"):
                return
            try:
                t = r.text()[:6000]
            except Exception:
                return
            if re.search(r"trustee|parcel|\bAPN\b", t, re.I):
                calls.append({"dir": "resp", "url": r.url, "status": r.status,
                              "body": t, "FLAG": "matched trustee/parcel/APN"})
                log(f"  >> interesting XHR response: {r.url[:100]}")

        ctx.on("request", on_req)
        ctx.on("response", on_resp)

        log(f"opening {BASE}")
        page.goto(BASE, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(3000)
        snap(page, "01_open")

        try:
            acc = page.get_by_text("I Accept", exact=False).first
            if acc.is_visible(timeout=4000):
                acc.click(); page.wait_for_timeout(3000)
                log("  accepted disclaimer")
            else:
                log("  no disclaimer (session persisted)")
        except Exception:
            log("  no disclaimer (session persisted)")

        if args.manual_nav:
            print("\n" + "=" * 72)
            print(" DRIVE THE BROWSER YOURSELF. Before pressing Enter:")
            print("  1. Solve the CAPTCHA if shown.")
            print("  2. Go to the Official Records / document search form.")
            print("  3. Type TRUSTEE into the document-type field; let the")
            print("     dropdown appear and pick the trustee-related types.")
            print("  4. Look for Advanced Search -> is there a Parcel # field?")
            print("  5. Set a ~30 day recorded-date range and RUN THE SEARCH.")
            print("  6. Wait for results to fully render.")
            print("=" * 72)
            input(">>> Press Enter when results are on screen...\n")
        else:
            end, start = date.today(), date.today() - timedelta(days=30)
            log(f"(no --manual-nav; you are on your own) {start}..{end}")

        snap(page, "02_final")
        hits, verdict = harvest(page, "final")

        (OUT / "network_calls.json").write_text(json.dumps(calls, indent=2))
        (OUT / "q1_matches.json").write_text(json.dumps(hits, indent=2))
        (OUT / "report.md").write_text(
            f"# Probe v2 — {date.today()}\n\n## Q1\n```json\n{json.dumps(hits, indent=2)}\n```\n"
            f"\n## Q3\nVerdict: **{verdict}**\n", encoding="utf-8")
        ctx.close()
    log(f"done — {OUT.resolve()}")


if __name__ == "__main__":
    main()
