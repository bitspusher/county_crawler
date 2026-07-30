#!/usr/bin/env python3
"""
collect_sjc.py — San Joaquin County trustee-sale collector.

Endpoints (mapped by probe, do not re-derive):
  GET  /Web/search/DOCSEARCH3032S7                       search form (field names)
  GET  /Web/search/documentTypes/DOCSEARCH3032S7?searchText=&maxValues=1000
                                                          full doctype vocabulary as JSON
  POST /Web/searchPost/DOCSEARCH3032S7                   sets server-side search state
  GET  /Web/searchResults/DOCSEARCH3032S7?page=N         the actual result grid
  GET  /Web/document/{DOCID}?search=DOCSEARCH3032S7      detail view

Doc type IDs:  41 = Notice Of Trustees Sale     22 = Trustees Deed Under Default

Design constraints, from the spec:
  * Append-only. Every fetch is an observation with observed_at. Nothing is updated.
  * Raw values only in tables. Price derivation lives in a VIEW, so revisiting the
    transfer-tax assumption never means re-collecting.
  * Deterministic parsing. Zero rows where rows were expected is a loud failure.
  * Polite. Serial requests, sleep between each, monthly windows to stay under the
    server-side result cap.

Session: reuses ./.browser_profile from the probe, so the CAPTCHA stays solved.

NOTE: the disclaimer/CAPTCHA gate is section-scoped. Clearing it on /Web/ does not
clear it on /Web/search/{SEARCH_ID} — the search/XHR endpoints check independently.
This file checks and clears both before doing any real work.

    python collect_sjc.py --start 2026-06-01 --end 2026-07-31 --headed
    python collect_sjc.py --report          # read-only, no network
"""

import argparse
import json
import re
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sys.exit("pip install playwright && python -m playwright install chromium")

HOST = "https://sanjoaquincountyca-web.tylerhost.net"
SEARCH_ID = "DOCSEARCH3032S7"
DB = Path("sjc.db")
PROFILE = Path("./.browser_profile").resolve()

DOCTYPES = {
    "NOTS": "Notice Of Trustees Sale",
    "TDUS": "Trustees Deed Under Default",
}

# Captured from the probe's autocomplete response. Used when the live
# vocabulary lookup is unavailable.
KNOWN_IDS = {
    "Notice Of Trustees Sale": "41",
    "Trustees Deed Under Default": "22",
    "Substitution Of Trustee": "51",
    "Trust/Trustee": "153",
}

# San Joaquin County documentary transfer tax: $1.10 per $1,000, value rounded
# up to the nearest $500 before the rate applies. Kept here as a constant and
# applied only in the view, never at collection time.
DTT_RATE_PER_1000 = 1.10

SCHEMA = """
CREATE TABLE IF NOT EXISTS index_obs (
    obs_id         INTEGER PRIMARY KEY,
    observed_at    TEXT NOT NULL,
    doc_number     TEXT NOT NULL,
    doc_type       TEXT,
    recording_date TEXT,
    detail_id      TEXT,
    page_no        INTEGER,
    query_start    TEXT,
    query_end      TEXT
);
CREATE TABLE IF NOT EXISTS detail_obs (
    obs_id         INTEGER PRIMARY KEY,
    observed_at    TEXT NOT NULL,
    doc_number     TEXT NOT NULL,
    detail_id      TEXT,
    recording_date TEXT,
    num_pages      INTEGER,
    recording_fee  REAL,
    tax_amount     REAL,
    tax_raw        TEXT,
    fetch_ok       INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS party_obs (
    id          INTEGER PRIMARY KEY,
    obs_id      INTEGER NOT NULL,
    obs_source  TEXT NOT NULL,
    doc_number  TEXT NOT NULL,
    role        TEXT NOT NULL,
    name        TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS run_log (
    id           INTEGER PRIMARY KEY,
    started_at   TEXT, finished_at TEXT,
    doc_type     TEXT, query_start TEXT, query_end TEXT,
    rows_indexed INTEGER, details_fetched INTEGER, notes TEXT
);
CREATE INDEX IF NOT EXISTS ix_index_doc  ON index_obs(doc_number);
CREATE INDEX IF NOT EXISTS ix_detail_doc ON detail_obs(doc_number);
CREATE INDEX IF NOT EXISTS ix_party_doc  ON party_obs(doc_number, role);
"""

VIEWS = f"""
DROP VIEW IF EXISTS v_latest_index;
CREATE VIEW v_latest_index AS
SELECT * FROM index_obs
WHERE obs_id IN (SELECT MAX(obs_id) FROM index_obs GROUP BY doc_number);

DROP VIEW IF EXISTS v_latest_detail;
CREATE VIEW v_latest_detail AS
SELECT * FROM detail_obs
WHERE fetch_ok = 1
  AND obs_id IN (SELECT MAX(obs_id) FROM detail_obs WHERE fetch_ok = 1 GROUP BY doc_number);

-- The auction-comps view. Price is DERIVED here, never stored.
DROP VIEW IF EXISTS v_auction_sales;
CREATE VIEW v_auction_sales AS
SELECT
    i.doc_number,
    i.recording_date,
    d.tax_amount,
    CASE WHEN d.tax_amount IS NULL THEN NULL
         WHEN d.tax_amount <= 0 THEN NULL
         ELSE ROUND(d.tax_amount / {DTT_RATE_PER_1000} * 1000.0, 0)
    END AS derived_price,
    CASE WHEN d.tax_amount IS NULL THEN 'unknown'
         WHEN d.tax_amount <= 0 THEN 'likely_reversion'
         ELSE 'likely_third_party'
    END AS sale_class,
    (SELECT GROUP_CONCAT(name, ' | ') FROM party_obs p
      WHERE p.doc_number = i.doc_number AND p.role='grantee'
        AND p.obs_id = i.obs_id) AS grantees,
    (SELECT GROUP_CONCAT(name, ' | ') FROM party_obs p
      WHERE p.doc_number = i.doc_number AND p.role='grantor'
        AND p.obs_id = i.obs_id) AS grantors
FROM v_latest_index i
LEFT JOIN v_latest_detail d ON d.doc_number = i.doc_number
WHERE i.doc_type = '{DOCTYPES["TDUS"]}';

DROP VIEW IF EXISTS v_upcoming;
CREATE VIEW v_upcoming AS
SELECT i.doc_number, i.recording_date,
       (SELECT GROUP_CONCAT(name, ' | ') FROM party_obs p
         WHERE p.doc_number=i.doc_number AND p.role='grantor' AND p.obs_id=i.obs_id) AS grantors
FROM v_latest_index i
WHERE i.doc_type = '{DOCTYPES["NOTS"]}';

-- Section 8: who buys repeatedly. This is the customer list.
DROP VIEW IF EXISTS v_repeat_buyers;
CREATE VIEW v_repeat_buyers AS
SELECT grantees AS buyer, COUNT(*) AS purchases,
       SUM(derived_price) AS total_derived
FROM v_auction_sales
WHERE sale_class = 'likely_third_party' AND grantees IS NOT NULL
GROUP BY grantees ORDER BY purchases DESC, total_derived DESC;
"""


def log(m):
    print(f"[collect] {m}", flush=True)


def _fmt_date(d):
    """M/D/YYYY, no zero-padding — matches the real browser's captured payload
    ("6/1/2026"), not strftime's "%m/%d/%Y" ("06/01/2026"). Probably harmless
    either way, but this matches reality exactly rather than assuming."""
    return f"{d.month}/{d.day}/{d.year}"


# --------------------------------------------------------------------------
# Parsers. Text-line based on purpose: Eagle's markup is jQuery-Mobile div soup
# that changes between releases, but the rendered label/value text is stable.
# --------------------------------------------------------------------------

MONEY = re.compile(r"\$?\s*([\d,]+\.?\d*)")
DOCNUM = re.compile(r"\b(\d{4}-\d{5,7})\b")
DATE_RE = re.compile(r"^\d{2}/\d{2}/\d{4}$")

# Every document page also carries cart/print/session-timeout popup markup,
# hidden via CSS but still present in the raw HTML. _lines() strips tags
# without knowing what's display:none, so this text can bleed into whatever
# field was mid-collection (usually grantor/grantee) if it isn't excluded.
JUNK_LINE = re.compile(
    r"^(print options|warning|ok|cancel|continue\?|"
    r"if you navigate away from this page)", re.I)


def _lines(html_fragment):
    t = re.sub(r"<script.*?</script>", " ", html_fragment, flags=re.S | re.I)
    t = re.sub(r"<style.*?</style>", " ", t, flags=re.S | re.I)
    t = re.sub(r"<[^>]+>", "\n", t)
    t = t.replace("&nbsp;", " ").replace("&amp;", "&")
    t = re.sub(r"&[a-z]+;|&#\d+;", " ", t)          # &bull; and friends
    out = []
    for x in t.split("\n"):
        x = re.sub(r"\s+", " ", x).strip(" \u2022\u00b7•·-").strip()
        if x:
            out.append(x)
    return out


def parse_results(html):
    """Split the grid into records. Returns list of dicts."""
    # Every page on this site shares the same trailing template — cart popup,
    # password-change popup, session-timeout popup, footer — after whatever
    # the page's actual content is. The row-splitting below only bounds each
    # chunk at the *next* row's start, so the very last row's chunk runs
    # unbounded to the end of the whole document and sweeps in that trailing
    # template markup (this is the same "Print Options / Warning / Cancel"
    # popup text seen contaminating parse_detail, just via the results grid
    # instead of the document page). Cut it off before splitting.
    end_m = re.search(r'<div[^>]*\bid="Cart-popup-Div"'
                      r'|<div[^>]*\bclass="ss-footer[ "]', html)
    if end_m:
        html = html[:end_m.start()]
    chunks = re.split(r'(?=<[^>]*class="[^"]*ss-search-row[^"]*")', html)
    out = []
    for ch in chunks[1:]:
        lines = _lines(ch)
        m = next((DOCNUM.search(l) for l in lines if DOCNUM.search(l)), None)
        if not m:
            continue
        rec = {"doc_number": m.group(1), "doc_type": None, "recording_date": None,
               "detail_id": None, "grantor": [], "grantee": []}
        href = re.search(r'href="(/Web/document/([^"?]+)[^"]*)"', ch)
        if href:
            rec["detail_id"] = href.group(2)
        # doc type is the next non-empty line after the one carrying the doc number
        dn = m.group(1)
        for i, l in enumerate(lines):
            if dn in l:
                rest = l.replace(dn, "").strip()
                if rest:                      # same line, e.g. "2026-067463 Deed"
                    rec["doc_type"] = rest
                elif i + 1 < len(lines):
                    rec["doc_type"] = lines[i + 1]
                break
        role = None
        for j, l in enumerate(lines):
            if re.match(r"^Recording Date$", l, re.I):
                # The value normally sits on the very next line. But if the cell
                # rendered empty, _lines() drops it entirely (it only keeps
                # truthy lines) and everything after shifts by one — the next
                # line becomes "Grantor" instead of a date. Search a short
                # distance forward for something date-shaped instead of
                # blindly trusting position.
                for k in range(j + 1, min(j + 4, len(lines))):
                    if DATE_RE.match(lines[k]):
                        rec["recording_date"] = lines[k]
                        break
            elif re.match(r"^Grantor(\s*\(\d+\))?$", l, re.I):
                role = "grantor"
            elif re.match(r"^Grantee(\s*\(\d+\))?$", l, re.I):
                role = "grantee"
            elif re.match(r"^(View|Recording Date)$", l, re.I):
                role = None
            elif role and not DOCNUM.search(l) and l not in ("T", "S", "N"):
                if not DATE_RE.match(l) and not JUNK_LINE.match(l):
                    rec[role].append(l)
        out.append(rec)
    return out


def parse_detail(html):
    """Label/value extraction from the document detail view."""
    # Bound to the real content section first. The full page also renders
    # cart, password-change, and session-timeout popups (hidden via CSS, not
    # removed from the DOM) — without this, a name-collecting loop that never
    # hits a recognized stop-label can wander straight into "Print Options /
    # Warning / OK / Cancel" text from one of those popups and silently
    # append it as if it were a grantor/grantee name.
    start_m = re.search(r'<div[^>]*\bid="documentIndexingInformation"', html)
    if start_m:
        tail = html[start_m.start():]
        end_m = re.search(r'<div[^>]*\bclass="ss-image-margins"'
                          r'|<div[^>]*\bid="Cart-popup-Div"', tail)
        html = tail[:end_m.start()] if end_m else tail
    lines = _lines(html)
    d = {"doc_number": None, "recording_date": None, "num_pages": None,
         "recording_fee": None, "tax_amount": None, "tax_raw": None,
         "grantor": [], "grantee": []}
    labels = {
        "rec #": "doc_number", "recording date": "recording_date",
        "number pages": "num_pages", "recording fee": "recording_fee",
        "tax amount": "tax_amount",
    }
    role = None
    for i, l in enumerate(lines):
        key = l.rstrip(":").strip().lower()
        if key in labels and i + 1 < len(lines):
            val = lines[i + 1]
            f = labels[key]
            if f in ("recording_fee", "tax_amount"):
                d["tax_raw"] = val if f == "tax_amount" else d["tax_raw"]
                mm = MONEY.search(val)
                d[f] = float(mm.group(1).replace(",", "")) if mm else None
            elif f == "num_pages":
                mm = re.search(r"\d+", val)
                d[f] = int(mm.group(0)) if mm else None
            elif f == "recording_date":
                d[f] = val.split()[0] if val else None
            else:
                d[f] = val
            role = None
        elif re.match(r"^Grantor:?$", l, re.I):
            role = "grantor"
        elif re.match(r"^Grantee:?$", l, re.I):
            role = "grantee"
        elif re.match(r"^(Names|Legal|Related|Images?)\b", l, re.I):
            role = None
        elif role:
            if l.rstrip(":").strip().lower() in labels or JUNK_LINE.match(l):
                role = None
            else:
                d[role].append(l)
    return d


# --------------------------------------------------------------------------
# HTTP, riding the authenticated browser session
# --------------------------------------------------------------------------

class Portal:
    def __init__(self, ctx, page, delay=1.5, debug=False):
        self.ctx, self.page, self.delay, self.debug = ctx, page, delay, debug
        self.req = ctx.request
        self._n = 0

    def _pause(self):
        time.sleep(self.delay)

    def _dump(self, tag, text):
        if not self.debug:
            return
        Path("debug").mkdir(exist_ok=True)
        self._n += 1
        p = Path("debug") / f"{self._n:03d}_{tag}.txt"
        p.write_text(text[:200000], encoding="utf-8")
        log(f"    (debug -> {p})")

    def on_search_page(self):
        """Eagle keys search state to the page you're on. Be there first."""
        if SEARCH_ID not in self.page.url:
            self.page.goto(f"{HOST}/Web/search/{SEARCH_ID}",
                           wait_until="domcontentloaded", timeout=45000)
            self.page.wait_for_timeout(2000)

    def xhr(self, url, method="GET", form=None):
        """Issue the request from inside the page so it looks like the app's own
        XHR. ctx.request omits X-Requested-With, which makes Eagle serve HTML."""
        self._pause()
        self.on_search_page()
        js = """
        async ([url, method, form]) => {
            const opts = {method, headers: {
                'X-Requested-With': 'XMLHttpRequest',
                'Accept': 'application/json, text/javascript, */*; q=0.01'
            }, credentials: 'same-origin'};
            if (form) {
                opts.headers['Content-Type'] =
                    'application/x-www-form-urlencoded; charset=UTF-8';
                opts.body = new URLSearchParams(form).toString();
            }
            const r = await fetch(url, opts);
            return {status: r.status, text: await r.text()};
        }"""
        res = self.page.evaluate(js, [url, method, form])
        if res["status"] != 200:
            raise RuntimeError(f"{method} {url} -> HTTP {res['status']}")
        return res["text"]

    def get(self, url):
        self._pause()
        r = self.req.get(url, timeout=45000)
        if r.status != 200:
            raise RuntimeError(f"GET {url} -> HTTP {r.status}")
        return r.text()

    def post(self, url, form):
        self._pause()
        r = self.req.post(url, form=form, timeout=45000)
        if r.status != 200:
            raise RuntimeError(f"POST {url} -> HTTP {r.status}")
        return r.text()

    def doctype_vocab(self):
        """Nice-to-have: lets the collector notice if the county renames a type.
        Never fatal — the IDs we need were captured by the probe."""
        try:
            raw = self.xhr(f"{HOST}/Web/search/documentTypes/{SEARCH_ID}"
                           f"?searchText=&maxValues=1000")
        except Exception as e:
            log(f"  ! vocabulary fetch failed ({e}); using known IDs")
            return dict(KNOWN_IDS)
        try:
            items = json.loads(raw)
        except json.JSONDecodeError:
            self._dump("vocab_not_json", raw)
            head = raw.strip()[:160].replace("\n", " ")
            log("  ! vocabulary was not JSON; using known IDs")
            log(f"    server said: {head}")
            if "I Accept" in raw or "disclaimer" in raw.lower():
                log("    ^ that is the DISCLAIMER page — session is not authenticated")
            return dict(KNOWN_IDS)
        return {it["value"]: it["name"] for it in items}

    def search(self, doctype_label, start, end, vocab=None):
        """Two-step: POST sets state, GET reads the grid.

        The document-type control is not a single form field — it's a token/
        autocomplete widget backed by five coordinated fields. Captured live from
        the browser against a real submission:

            field_selfservice_documentTypes-holderInput:   41                       (numeric id)
            field_selfservice_documentTypes-holderValue:   Notice Of Trustees Sale  (display label)
            field_selfservice_documentTypes-searchInput:   notice of trustees sale  (lowercase text)
            field_selfservice_documentTypes-containsInput: Contains Any             (mode flag)
            field_selfservice_documentTypes:               notice of trustees sale  (bare, same text)

        The old code sent only the bare field with a single value and guessed
        between "id" and "label" — neither is a valid filter on its own, so the
        server silently ignored it and returned every doc type unfiltered. There's
        no more guessing needed now that the real shape is known.

        Also matching reality: the live payload omits field_DocNumID,
        field_BothNamesID, field_GrantorID, and field_GranteeID entirely (not even
        as blank strings), so those are dropped here too.
        """
        doctype_id = (vocab or {}).get(doctype_label) or KNOWN_IDS.get(doctype_label)
        if not doctype_id:
            raise RuntimeError(f"no known document-type id for {doctype_label!r}")
        search_text = doctype_label.lower()
        form = {
            "field_RecDateID_DOT_StartDate": _fmt_date(start),
            "field_RecDateID_DOT_EndDate": _fmt_date(end),
            "field_selfservice_documentTypes-holderInput": doctype_id,
            "field_selfservice_documentTypes-holderValue": doctype_label,
            "field_selfservice_documentTypes-searchInput": search_text,
            "field_selfservice_documentTypes-containsInput": "Contains Any",
            "field_selfservice_documentTypes": search_text,
            "field_UseAdvancedSearch": "",
        }
        self.xhr(f"{HOST}/Web/searchPost/{SEARCH_ID}", method="POST", form=form)
        rows, page = [], 1
        while True:
            html = self.xhr(f"{HOST}/Web/searchResults/{SEARCH_ID}"
                            f"?page={page}&_={int(time.time()*1000)}")
            if page == 1:
                self._dump("results_p1", html)
            if "more documents than the maximum allowed" in html:
                raise RuntimeError("result cap hit — narrow the date window")
            got = parse_results(html)
            if not got:
                break
            rows += got
            log(f"    page {page}: {len(got)} rows")
            if page >= 40:
                log("    !! 40 pages, stopping — window too wide")
                break
            page += 1
        return rows

    def detail(self, detail_id):
        # Detail views are real page loads, not XHR — navigate to them.
        self._pause()
        self.page.goto(f"{HOST}/Web/document/{detail_id}?search={SEARCH_ID}",
                       wait_until="domcontentloaded", timeout=45000)
        self.page.wait_for_timeout(1200)
        html = self.page.content()
        self._dump(f"detail_{detail_id}", html)
        return html


# --------------------------------------------------------------------------

def months(start, end):
    cur = start.replace(day=1)
    while cur <= end:
        nxt = (cur.replace(day=28) + timedelta(days=4)).replace(day=1)
        yield max(cur, start), min(nxt - timedelta(days=1), end)
        cur = nxt


def db_open():
    con = sqlite3.connect(DB)
    con.executescript(SCHEMA)
    con.executescript(VIEWS)
    return con


def store_index(con, rows, page_no, qs, qe):
    now = datetime.now().isoformat(timespec="seconds")
    for r in rows:
        cur = con.execute(
            "INSERT INTO index_obs(observed_at,doc_number,doc_type,recording_date,"
            "detail_id,page_no,query_start,query_end) VALUES(?,?,?,?,?,?,?,?)",
            (now, r["doc_number"], r["doc_type"], r["recording_date"],
             r["detail_id"], page_no, str(qs), str(qe)))
        oid = cur.lastrowid
        for role in ("grantor", "grantee"):
            for n in r[role]:
                con.execute("INSERT INTO party_obs(obs_id,obs_source,doc_number,role,name)"
                            " VALUES(?,?,?,?,?)", (oid, "index", r["doc_number"], role, n))
    con.commit()


def store_detail(con, doc_number, detail_id, d, ok=True):
    now = datetime.now().isoformat(timespec="seconds")
    con.execute(
        "INSERT INTO detail_obs(observed_at,doc_number,detail_id,recording_date,"
        "num_pages,recording_fee,tax_amount,tax_raw,fetch_ok) VALUES(?,?,?,?,?,?,?,?,?)",
        (now, doc_number, detail_id, d.get("recording_date"), d.get("num_pages"),
         d.get("recording_fee"), d.get("tax_amount"), d.get("tax_raw"), 1 if ok else 0))
    con.commit()


def report(con):
    print("\n" + "=" * 68)
    n_i = con.execute("SELECT COUNT(DISTINCT doc_number) FROM index_obs").fetchone()[0]
    n_d = con.execute("SELECT COUNT(DISTINCT doc_number) FROM detail_obs WHERE fetch_ok=1").fetchone()[0]
    print(f"documents indexed: {n_i}    details fetched: {n_d}")

    print("\n-- by doc type --")
    for t, c in con.execute("SELECT doc_type, COUNT(DISTINCT doc_number) FROM v_latest_index"
                            " GROUP BY doc_type ORDER BY 2 DESC"):
        print(f"   {c:5d}  {t}")

    print("\n-- TDUS: reversion vs third-party (the §11926 test) --")
    rows = list(con.execute("SELECT sale_class, COUNT(*), ROUND(AVG(derived_price)) "
                            "FROM v_auction_sales GROUP BY sale_class"))
    if not rows:
        print("   (no TDUS records yet)")
    for cls, c, avg in rows:
        avg_s = f"avg derived ${avg:,.0f}" if avg else ""
        print(f"   {c:5d}  {cls:20s} {avg_s}")

    print("\n-- sample derived prices --")
    for dn, rd, tax, price, cls, gees in con.execute(
            "SELECT doc_number,recording_date,tax_amount,derived_price,sale_class,grantees"
            " FROM v_auction_sales WHERE derived_price IS NOT NULL"
            " ORDER BY recording_date DESC LIMIT 12"):
        print(f"   {dn}  {rd}  tax ${tax:>9,.2f} -> ${price:>12,.0f}  {(gees or '')[:44]}")

    print("\n-- repeat buyers (Section 8) --")
    for b, n, tot in con.execute("SELECT buyer,purchases,total_derived FROM v_repeat_buyers LIMIT 12"):
        print(f"   {n:3d}  {b[:52]:52s} ${tot or 0:,.0f}")
    print("=" * 68)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default=(date.today() - timedelta(days=30)).isoformat())
    ap.add_argument("--end", default=date.today().isoformat())
    ap.add_argument("--types", default="TDUS,NOTS")
    ap.add_argument("--no-detail", action="store_true", help="index only, skip detail fetches")
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--delay", type=float, default=1.5)
    ap.add_argument("--limit-details", type=int, default=0, help="0 = no limit")
    ap.add_argument("--report", action="store_true", help="read the DB, no network")
    ap.add_argument("--debug", action="store_true", help="dump raw responses to ./debug")
    args = ap.parse_args()

    con = db_open()
    if args.report:
        report(con)
        return

    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    want = [DOCTYPES[k.strip()] for k in args.types.split(",") if k.strip() in DOCTYPES]

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE), headless=not args.headed,
            viewport={"width": 1400, "height": 900})
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        # --- Checkpoint 1: root page ---
        page.goto(f"{HOST}/Web/", wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(2500)
        if "I Accept" in page.content():
            if not args.headed:
                ctx.close()
                sys.exit("Disclaimer/CAPTCHA gate. Rerun with --headed and clear it.")
            input(">>> Clear the disclaimer/CAPTCHA, then press Enter...\n")

        # --- Checkpoint 2: search page ---
        # The gate is section-scoped. Clearing it on /Web/ does not clear it on
        # /Web/search/{SEARCH_ID} — the search/XHR endpoints check independently.
        page.goto(f"{HOST}/Web/search/{SEARCH_ID}", wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(2500)
        if "I Accept" in page.content() or "recaptcha" in page.content().lower():
            if not args.headed:
                ctx.close()
                sys.exit("Disclaimer/CAPTCHA gate on search page. Rerun with --headed and clear it.")
            input(">>> Clear the disclaimer/CAPTCHA on the SEARCH page, then press Enter...\n")

        portal = Portal(ctx, page, delay=args.delay, debug=args.debug)

        vocab = portal.doctype_vocab()
        log(f"vocabulary: {len(vocab)} document types")
        for k, label in DOCTYPES.items():
            if label not in vocab:
                log(f"  !! '{label}' NOT IN VOCABULARY — county may have renamed it")
            else:
                log(f"  {k}: id={vocab[label]}  '{label}'")

        for label in want:
            for qs, qe in months(start, end):
                log(f"{label}  {qs} .. {qe}")
                rows = []
                try:
                    rows = portal.search(label, qs, qe, vocab=vocab)
                except RuntimeError as e:
                    log(f"  ! {e}")
                log(f"  {len(rows)} rows")
                if not rows:
                    log("  !! ZERO ROWS — expected ~30/month. Investigate before trusting.")
                store_index(con, rows, 0, qs, qe)

                if args.no_detail:
                    continue
                todo = [r for r in rows if r["detail_id"]]
                if args.limit_details:
                    todo = todo[:args.limit_details]
                for i, r in enumerate(todo, 1):
                    try:
                        d = parse_detail(portal.detail(r["detail_id"]))
                        store_detail(con, r["doc_number"], r["detail_id"], d)
                        log(f"    [{i}/{len(todo)}] {r['doc_number']} tax={d.get('tax_raw')}")
                    except Exception as e:
                        log(f"    [{i}/{len(todo)}] {r['doc_number']} FAILED: {e}")
                        store_detail(con, r["doc_number"], r["detail_id"], {}, ok=False)
        ctx.close()

    report(con)


if __name__ == "__main__":
    main()