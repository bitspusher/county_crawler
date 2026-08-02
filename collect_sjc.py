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
  * Polite. Serial requests, sleep between each, short windows (default 3 days)
    to stay under the server-side result cap. Every sweep is unfiltered — not
    because the filter cannot work, but because the rescissions Phase 3 needs
    arrive free with the sweep — so selection happens client-side via
    KEEP_DOCTYPES, and the window has to be short enough that a whole day of
    county recordings fits under the cap.

Session: reuses ./.browser_profile from the probe, so the CAPTCHA stays solved.

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
except ImportError:  # pragma: no cover - exercised by the absence of the package
    # Deferred, not fatal at import time. The parsers, the date-window logic and
    # the derivation views are all pure and testable with no browser at all;
    # exiting here made every one of them unimportable and the whole non-network
    # suite unrunnable (including from cron, where no browser is installed).
    # main() is the only thing that genuinely needs Playwright, so that is where
    # the requirement is enforced.
    sync_playwright = None

HOST = "https://sanjoaquincountyca-web.tylerhost.net"
SEARCH_ID = "DOCSEARCH3032S7"
DB = Path("sjc.db")
PROFILE = Path("./.browser_profile").resolve()

DOCTYPES = {
    "NOTS": "Notice Of Trustees Sale",
    "TDUS": "Trustees Deed Under Default",
}

# A sweep is unfiltered, so it retrieves every type recorded in the window —
# roughly 1300 documents per three days, of which about 20 are relevant.
# Selection therefore happens here, on the way into the database.
#
# Everything not listed is DISCARDED rather than stored: keeping it would mean
# collecting `Substitution Of Trustee` at 1016/mo, which AI_CONTEXT.md rule 7
# forbids because it tracks payoffs, not distress, and would swamp the dataset
# with false positives.
#
# `Rescission Of Default` arrives free with the sweep and is what makes ROADMAP
# Phase 3 reachable — it is the document that says a recorded sale will not
# happen. June 2026's 47 look right: MERS plus a lender in nearly every case.
#
# `Cancellation/Termination` was collected through June 2026 and then DROPPED,
# because the measurement said it carries no foreclosure signal at all. All 83
# June documents partition into City of Stockton lien releases (34), rooftop-solar
# UCC terminations (36) and homebuilder filings (4); not one foreclosure trustee
# firm appears on any of them — no Clear Recon, Prime Recon, ZBS Law, Affinia,
# Entra, National Default, Prestige. It is a generic index label, the same trap as
# `Substitution Of Trustee`: high volume, wrong signal. Phase 3's join is against
# rescissions only. The 83 already in the database stay there — the tables are
# append-only (rule 2) and a stored observation is still a true observation.
KEEP_DOCTYPES = {
    "Notice Of Trustees Sale",
    "Trustees Deed Under Default",
    "Rescission Of Default",
}

# Only these need a detail fetch. The price derivation (§6.4) reads the
# documentary transfer tax, which only completed sales carry; a NOTS detail adds
# nothing the index does not already have. At ~31 TDUS/month this is ~31 extra
# requests, against ~1300/month if details were fetched for everything relevant.
DETAIL_DOCTYPES = {"Trustees Deed Under Default"}

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


class UnexpectedResponse(RuntimeError):
    """An XHR returned HTTP 200, but not the JSON or fragment the app would get.

    Eagle answers a request it does not accept as an app-level XHR by serving the
    SPA wrapper (or the disclaimer gate) with a 200 status. Checking only the
    status code therefore treats a total failure as a success: the searchPost that
    sets server-side search state silently no-ops, and the follow-up
    searchResults request answers some default unfiltered query instead. That is
    how a month of TDUS — roughly 31 documents (§4) — comes back reporting the
    result cap: the doc-type filter was never applied at all.

    Same principle as the zero-row guard one level lower down: a response that
    looks like success and carries no data is a loud failure, not a result.
    See AI_CONTEXT.md rule 3.
    """


# A response body opening with a full HTML document is the SPA shell, not the
# fragment or JSON an app XHR receives. Only the head of the body is examined so
# that a fragment merely *mentioning* these strings is not misread.
_FULL_DOCUMENT = re.compile(r"<!doctype\s+html|<html[\s>]", re.I)
_DISCLAIMER_MARKERS = ("I Accept", "g-recaptcha")


class ResultCapExceeded(RuntimeError):
    """The server reported more documents than it will return for this window.

    Distinct from a generic RuntimeError on purpose. A capped window is
    *incomplete*, which is strictly worse than missing, because downstream it
    looks complete. §5.1 requires a cap be treated as an error and never as an
    empty result, so callers must not let this fall through into the
    empty-result retry path. See AI_CONTEXT.md rule 4.
    """


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


# --------------------------------------------------------------------------
# Parsers. Text-line based on purpose: Eagle's markup is jQuery-Mobile div soup
# that changes between releases, but the rendered label/value text is stable.
# --------------------------------------------------------------------------

MONEY = re.compile(r"\$?\s*([\d,]+\.?\d*)")
DOCNUM = re.compile(r"\b(\d{4}-\d{5,7})\b")


def _lines(html_fragment):
    t = re.sub(r"<script.*?</script>", " ", html_fragment, flags=re.S | re.I)
    t = re.sub(r"<style.*?</style>", " ", t, flags=re.S | re.I)
    t = re.sub(r"<[^>]+>", "\n", t)
    t = t.replace("&nbsp;", " ").replace("&amp;", "&")
    t = re.sub(r"&[a-z]+;|&#\d+;", " ", t)  # &bull; and friends
    out = []
    for x in t.split("\n"):
        # B005 is suppressed below: the multi-char set is intentional — strip
        # ANY of these bullet glyphs from either end, not a fixed prefix.
        x = re.sub(r"\s+", " ", x).strip(" \u2022\u00b7•·-").strip()  # noqa: B005
        if x:
            out.append(x)
    return out


def parse_results(html):
    """Split the grid into records. Returns list of dicts."""
    chunks = re.split(r'(?=<[^>]*class="[^"]*ss-search-row[^"]*")', html)
    out = []
    for ch in chunks[1:]:
        lines = _lines(ch)
        m = next((DOCNUM.search(l) for l in lines if DOCNUM.search(l)), None)
        if not m:
            continue
        rec = {
            "doc_number": m.group(1),
            "doc_type": None,
            "recording_date": None,
            "detail_id": None,
            "grantor": [],
            "grantee": [],
        }
        href = re.search(r'href="(/Web/document/([^"?]+)[^"]*)"', ch)
        if href:
            rec["detail_id"] = href.group(2)
        # doc type is the next non-empty line after the one carrying the doc number
        dn = m.group(1)
        for i, l in enumerate(lines):
            if dn in l:
                rest = l.replace(dn, "").strip()
                if rest:  # same line, e.g. "2026-067463 Deed"
                    rec["doc_type"] = rest
                elif i + 1 < len(lines):
                    rec["doc_type"] = lines[i + 1]
                break
        role = None
        for j, l in enumerate(lines):
            if re.match(r"^Recording Date$", l, re.I) and j + 1 < len(lines):
                rec["recording_date"] = lines[j + 1]
            elif re.match(r"^Grantor(\s*\(\d+\))?$", l, re.I):
                role = "grantor"
            elif re.match(r"^Grantee(\s*\(\d+\))?$", l, re.I):
                role = "grantee"
            elif re.match(r"^(View|Recording Date)$", l, re.I):
                role = None
            elif role and not DOCNUM.search(l) and l not in ("T", "S", "N"):  # noqa: SIM102
                # Kept nested: the date guard is a distinct classification
                # step, not part of deciding whether the line is a party.
                if not re.match(r"^\d{2}/\d{2}/\d{4}$", l):
                    rec[role].append(l)
        out.append(rec)
    return out


def parse_detail(html):
    """Label/value extraction from the document detail view."""
    lines = _lines(html)
    d = {
        "doc_number": None,
        "recording_date": None,
        "num_pages": None,
        "recording_fee": None,
        "tax_amount": None,
        "tax_raw": None,
        "grantor": [],
        "grantee": [],
    }
    labels = {
        "rec #": "doc_number",
        "recording date": "recording_date",
        "number pages": "num_pages",
        "recording fee": "recording_fee",
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
            if l.rstrip(":").strip().lower() in labels:
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
        # Set per search(); initialised here so a caller that catches an
        # exception out of search() still finds defined attributes.
        self.total_pages = None
        self.sweep_warnings = []

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
            self.page.goto(f"{HOST}/Web/search/{SEARCH_ID}", wait_until="domcontentloaded", timeout=45000)
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
        body = res["text"]

        # A 200 is not evidence the request was accepted as an app XHR. Validate
        # the SHAPE of the body, or a silently-rejected POST looks like success
        # and every request after it operates on state that was never set.
        head = body[:2000]
        if any(m in head for m in _DISCLAIMER_MARKERS):
            raise UnexpectedResponse(
                f"{method} {url} -> disclaimer/CAPTCHA gate. The session is not "
                "authenticated; re-run with --headed and clear it."
            )
        if _FULL_DOCUMENT.search(head):
            raise UnexpectedResponse(
                f"{method} {url} -> app shell, not a data fragment. Header "
                "emulation was not accepted for this endpoint, so this request "
                "did nothing. See ROADMAP Phase 1."
            )
        return body

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
            raw = self.xhr(f"{HOST}/Web/search/documentTypes/{SEARCH_ID}?searchText=&maxValues=1000")
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

    def search(self, doctype_value, start, end, mode="id", vocab=None, max_pages=40):
        """Two-step: POST sets state, GET reads the grid.

        `doctype_value` may be None to search the window UNFILTERED, which is
        what collection does. The doc-type selection that actually happens is
        client-side, in KEEP_DOCTYPES and in the views (`v_upcoming`,
        `v_auction_sales`).

        A non-None value posts a single bare `field_selfservice_documentTypes`,
        and the server DISCARDS that — the field is a text-input autocomplete
        whose real payload is five fields (`-holderInput` carrying the id,
        `-holderValue` the label, `-searchInput`, `-containsInput`, and the bare
        field). Sending all five does filter: June 2026 NOTS returns 43 documents
        on one page against 1,323 across 14 pages for an unfiltered *three-day*
        window. So the filter works and this method does not implement it.

        That is deliberate, not an oversight. The unfiltered sweep returns
        `Rescission Of Default` for free, and Phase 3's exclusion join needs it;
        a filtered search would cost a second query per window to get the same
        rows back. The trade is requests (~150/month swept vs ~8 filtered) against
        collecting the rescissions at all. Revisit if backfill volume makes the
        request count the binding constraint — a wider window becomes viable at
        the same time, since a filtered month is one page, not ~130.
        """
        dt = "" if doctype_value is None else (vocab[doctype_value] if (mode == "id" and vocab) else doctype_value)
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
        post_body = self.xhr(f"{HOST}/Web/searchPost/{SEARCH_ID}", method="POST", form=form)

        # The POST answers with JSON — {"validationMessages":{},"totalPages":N,
        # "currentPage":1} — so the size of the result set is known BEFORE any
        # page is walked. Checking it here turns "discover at page 40 that the
        # window was too wide" into "refuse the window up front", and surfaces
        # server-side validation complaints that would otherwise be invisible.
        self.total_pages = None
        self.sweep_warnings = []
        try:
            meta = json.loads(post_body)
        except json.JSONDecodeError:
            pass  # older behaviour: fall back to walking
        else:
            self.total_pages = meta.get("totalPages")
            msgs = meta.get("validationMessages") or {}
            if msgs:
                log(f"    ! server validation messages: {msgs}")
            if self.total_pages:
                log(f"    server reports {self.total_pages} page(s)")
                if self.total_pages > max_pages:
                    raise ResultCapExceeded(
                        f"window needs {self.total_pages} pages, over the {max_pages}-page "
                        "limit — narrow it (try a shorter --window-days)"
                    )

        rows, page = [], 1
        pages_walked, page_size = 0, None
        while True:
            html = self.xhr(f"{HOST}/Web/searchResults/{SEARCH_ID}?page={page}&_={int(time.time() * 1000)}")
            if page == 1:
                self._dump("results_p1", html)
            if "more documents than the maximum allowed" in html:
                raise ResultCapExceeded(f"result cap hit on page {page} — narrow the date window")
            got = parse_results(html)
            if not got:
                break
            # Attribute each row to the page it came from. Discarding this (the
            # old `store_index(..., 0, ...)`) threw away the only evidence that
            # would let a reader tell a short final page from a truncated walk.
            for r in got:
                r["page_no"] = page
            rows += got
            pages_walked = page
            if page_size is None:
                page_size = len(got)
            log(f"    page {page}: {len(got)} rows")
            if page >= max_pages:
                log(f"    !! {max_pages} pages, stopping — window too wide")
                break
            page += 1

        self._reconcile(rows, pages_walked, page_size)
        return rows

    def _reconcile(self, rows, pages_walked, page_size):
        """Check what was swept against what the server said it had.

        Doc 2026-057938 came back twice in one June window, on pages 8 and 9 —
        one repeat in 1,168 rows. The duplicate itself is harmless: the tables
        are append-only and the views take the latest observation per document.
        The concern is the other half of the same defect. A paginated boundary
        that can repeat a row can also SKIP one, and a skipped row is invisible
        downstream — it looks exactly like a document the county never recorded,
        which is absence stored as data (rule 3's failure mode, one level up).

        `totalPages` arrived free with the searchPost, so this costs no extra
        requests. Warnings are recorded rather than raised: unlike a result cap,
        a mismatch does not mean the rows in hand are wrong, only that the window
        may be short. `main` writes them into `run_log.notes` so the hole is
        attributable to a window later.
        """
        self.sweep_warnings = []

        seen, dupes = set(), []
        for r in rows:
            if r["doc_number"] in seen:
                dupes.append(r["doc_number"])
            seen.add(r["doc_number"])
        if dupes:
            shown = ", ".join(sorted(set(dupes))[:5])
            self.sweep_warnings.append(f"DUPLICATE_ROWS: {len(set(dupes))} doc(s) returned more than once ({shown})")

        if self.total_pages:
            if pages_walked != self.total_pages:
                self.sweep_warnings.append(
                    f"PAGE_COUNT_MISMATCH: walked {pages_walked} page(s), server reported {self.total_pages}"
                )
            elif page_size and len(rows) <= page_size * (self.total_pages - 1):
                # Every page but the last should be full. Falling at or below
                # what n-1 full pages would hold means the walk lost rows at a
                # boundary, even though it visited every page.
                self.sweep_warnings.append(
                    f"ROW_COUNT_SHORT: {len(rows)} rows across {self.total_pages} page(s) of {page_size}; "
                    "at least one row was dropped at a page boundary"
                )

        for w in self.sweep_warnings:
            log(f"    !! {w}")

    def detail(self, detail_id):
        # Detail views are real page loads, not XHR — navigate to them.
        self._pause()
        self.page.goto(
            f"{HOST}/Web/document/{detail_id}?search={SEARCH_ID}", wait_until="domcontentloaded", timeout=45000
        )
        self.page.wait_for_timeout(1200)
        html = self.page.content()
        self._dump(f"detail_{detail_id}", html)
        return html


# --------------------------------------------------------------------------


def windows(start, end, days):
    """Split [start, end] into consecutive windows of at most `days` days.

    Monthly windows are too wide now that every sweep is unfiltered: a month is
    ~13,000 documents and roughly 130 pages, which trips the result cap. Three
    days is ~1300 documents and 14 pages, which the server serves happily.
    """
    if days < 1:
        raise ValueError("days must be >= 1")
    cur = start
    while cur <= end:
        stop = min(cur + timedelta(days=days - 1), end)
        yield cur, stop
        cur = stop + timedelta(days=1)


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


def store_index(con, rows, qs, qe):
    """Store index observations, each carrying the result page it was read from.

    `page_no` comes off the row (set by Portal.search) rather than being passed
    in, so per-page attribution survives into the DB.
    """
    now = datetime.now().isoformat(timespec="seconds")
    for r in rows:
        cur = con.execute(
            "INSERT INTO index_obs(observed_at,doc_number,doc_type,recording_date,"
            "detail_id,page_no,query_start,query_end) VALUES(?,?,?,?,?,?,?,?)",
            (
                now,
                r["doc_number"],
                r["doc_type"],
                r["recording_date"],
                r["detail_id"],
                r.get("page_no"),
                str(qs),
                str(qe),
            ),
        )
        oid = cur.lastrowid
        for role in ("grantor", "grantee"):
            for n in r[role]:
                con.execute(
                    "INSERT INTO party_obs(obs_id,obs_source,doc_number,role,name) VALUES(?,?,?,?,?)",
                    (oid, "index", r["doc_number"], role, n),
                )
    con.commit()


def store_detail(con, doc_number, detail_id, d, ok=True):
    now = datetime.now().isoformat(timespec="seconds")
    con.execute(
        "INSERT INTO detail_obs(observed_at,doc_number,detail_id,recording_date,"
        "num_pages,recording_fee,tax_amount,tax_raw,fetch_ok) VALUES(?,?,?,?,?,?,?,?,?)",
        (
            now,
            doc_number,
            detail_id,
            d.get("recording_date"),
            d.get("num_pages"),
            d.get("recording_fee"),
            d.get("tax_amount"),
            d.get("tax_raw"),
            1 if ok else 0,
        ),
    )
    con.commit()


def run_log_start(con, doc_type, qs, qe):
    """Open a run_log row for one (doc_type, window) attempt. Returns its id.

    Written unconditionally, BEFORE the search runs, so a window that fails or
    returns nothing still leaves a record. Without this the absence of rows for
    a month is ambiguous between "no foreclosures were recorded" and "that
    month was never collected" — ROADMAP Gap 8.
    """
    cur = con.execute(
        "INSERT INTO run_log(started_at,doc_type,query_start,query_end) VALUES(?,?,?,?)",
        (datetime.now().isoformat(timespec="seconds"), doc_type, str(qs), str(qe)),
    )
    con.commit()
    return cur.lastrowid


def run_log_finish(con, run_id, rows_indexed, details_fetched, notes=None):
    """Close a run_log row. `notes` carries the failure reason, if any."""
    con.execute(
        "UPDATE run_log SET finished_at=?, rows_indexed=?, details_fetched=?, notes=? WHERE id=?",
        (datetime.now().isoformat(timespec="seconds"), rows_indexed, details_fetched, notes, run_id),
    )
    con.commit()


def _mmddyyyy_sort_key(v):
    """Sort MM/DD/YYYY strings chronologically.

    Recording dates are stored exactly as the portal renders them (raw values
    only, §6), so ORDER BY on the column sorts lexically — putting 01/05 before
    12/31 of the prior year. Ordering happens in Python instead of changing the
    stored format.
    """
    if not v:
        return (0, 0, 0)
    parts = str(v).split()[0].split("/")
    if len(parts) != 3:
        return (0, 0, 0)
    try:
        m, d, y = (int(p) for p in parts)
    except ValueError:
        return (0, 0, 0)
    return (y, m, d)


def _us(d):
    """Render a date the way the portal does, so report text and stored
    `recording_date` values read in the same format."""
    return d.strftime("%m/%d/%Y")


def report_sections(con, days=7, limit=40, today=None):
    """The §7 output: Section A (coming up) and Section B (just closed).

    This is what the product is for — everything below it in report() is
    diagnostics. Unavailable fields are printed as unavailable rather than
    omitted, per §7: a silently dropped field reads as "checked and empty".

    `days` bounds Section A to notices recorded in the last `days` days,
    inclusive of `today`; 0 means no bound. It used to appear in the header and
    nowhere else, so a run over June data printed "last 7 days" above notices
    spanning the whole month — invisible on a three-day database and a false
    claim once Phase 4's backfill lands. A notice excluded by the bound is
    COUNTED and reported, never silently dropped: the whole point of §7's
    unavailable-not-omitted rule is that the reader can tell what was not shown.

    `today` is injectable so the bound is testable without freezing the clock.
    """
    today = today or date.today()
    cutoff = today - timedelta(days=days - 1) if days else None
    scope = "all notices collected" if cutoff is None else f"notices recorded {_us(cutoff)} to {_us(today)}"

    print("\n" + "=" * 72)
    print(f"SECTION A — COMING UP FOR AUCTION   ({scope})")
    print("=" * 72)

    collected = [r for r in con.execute("SELECT doc_number, recording_date, grantors FROM v_upcoming")]
    collected.sort(key=lambda r: _mmddyyyy_sort_key(r[1]), reverse=True)
    if cutoff is None:
        rows, hidden = collected, 0
    else:
        floor = (cutoff.year, cutoff.month, cutoff.day)
        # An unreadable or missing recording date sorts to (0,0,0) and would be
        # dropped by the bound. Keep it: it prints as "unavailable", which is a
        # visible gap, whereas excluding it would silently shrink the list.
        rows = [r for r in collected if _mmddyyyy_sort_key(r[1]) >= floor or _mmddyyyy_sort_key(r[1]) == (0, 0, 0)]
        hidden = len(collected) - len(rows)
    if not rows:
        if collected:
            print(f"  (no notices recorded in this window. {len(collected)} older notice(s) are")
            print("   in the database — re-run with --report-days 0 to see them all.)")
        else:
            print("  (no notices of trustee's sale collected yet)")
    else:
        # NOT labelled "owner". Live data (June 2026) shows the grantor list
        # carries the foreclosure TRUSTEE alongside the homeowner — e.g.
        # e.g. docs 2026-055449 and 2026-051883, where the homeowner is listed
        # beside PRIME RECON LLC / ZBS LAW LLP (individual names stay in the DB,
        # not in code — AI_CONTEXT.md rule 11).
        # The index does not label party roles, so calling this column "owner"
        # asserts a distinction the data does not make. AI_CONTEXT.md rule 8.
        print(f"  {'DOC NUMBER':14s} {'RECORDED':11s} {'AUCTION DATE':13s} PARTIES ON THE NOTICE")
        for dn, rd, grantors in rows[:limit]:
            parties = (grantors or "unavailable")[:52]
            # Neither of these is in the recorder index (§6.3, and the index
            # carries the notice's recording date only, not the sale date).
            print(f"  {dn:14s} {(rd or 'unavailable'):11s} {'unavailable':13s} {parties}")
        if len(rows) > limit:
            print(f"  ... and {len(rows) - limit} more")
    if hidden:
        print(f"\n  {hidden} further notice(s) are in the database, recorded before {_us(cutoff)} and")
        print("  therefore outside this window. Re-run with --report-days 0 for all of them.")
    print("\n  Address and APN: UNAVAILABLE — not present anywhere in the recorder")
    print("  index or detail views (MVP.md §6.3).")
    print("  PARTIES is the raw grantor list from the grantor/grantee index, and it")
    print("  mixes roles: the homeowner AND the foreclosure trustee appear together")
    print("  (e.g. '... | PRIME RECON LLC', '... | ZBS LAW LLP'), because the index")
    print("  does not label who is who. Do not read the first name as the owner —")
    print("  that ordering is not guaranteed. The recorder states this index is a")
    print("  finding aid only, not a substitute for a title search.")
    print("  Auction date: UNAVAILABLE — the index carries the notice's recording")
    print("  date, not the sale date.")
    print("  WARNING: rescissions of default are collected but not yet joined out of")
    print("  this list (ROADMAP Phase 3), so some of these sales will not")
    print("  happen. Sales are also postponed by spoken announcement at the auction,")
    print("  which never reaches the recorder at all — no amount of collection")
    print("  catches those.")

    print("\n" + "=" * 72)
    print("SECTION B — JUST CLOSED   (completed trustee's sales)")
    print("=" * 72)
    sales = list(
        con.execute(
            "SELECT doc_number, recording_date, tax_amount, derived_price, sale_class, grantees FROM v_auction_sales"
        )
    )
    sales.sort(key=lambda r: _mmddyyyy_sort_key(r[1]), reverse=True)
    if not sales:
        print("  (no completed sales collected yet)")
    else:
        print(f"  {'DOC NUMBER':14s} {'RECORDED':11s} {'PRICE':>13s}  {'CLASS':19s} BUYER")
        for dn, rd, _tax, price, cls, gees in sales[:limit]:
            price_s = f"${price:,.0f}" if price else "unavailable"
            print(f"  {dn:14s} {(rd or '?'):11s} {price_s:>13s}  {(cls or '?'):19s} {(gees or 'unavailable')[:34]}")
        if len(sales) > limit:
            print(f"  ... and {len(sales) - limit} more")
    print("\n  Price is DERIVED, not recorded: computed from the documentary transfer")
    print(f"  tax at ${DTT_RATE_PER_1000:.2f}/$1,000 (±$500). Two assumptions behind it are still")
    print("  UNVALIDATED — the Stockton charter-city rate, and the §11926 zero-tax")
    print("  split separating genuine purchases from lender credit-bids (§6.4, §6.5).")
    print("  'likely_reversion' means $0.00 tax, read as the lender taking the")
    print("  property back. 'unknown' means the tax amount could not be read.")
    print("=" * 72)


def report(con, days=7):
    report_sections(con, days=days)
    print("\n" + "=" * 68)
    print("DIAGNOSTICS")
    n_i = con.execute("SELECT COUNT(DISTINCT doc_number) FROM index_obs").fetchone()[0]
    n_d = con.execute("SELECT COUNT(DISTINCT doc_number) FROM detail_obs WHERE fetch_ok=1").fetchone()[0]
    print(f"documents indexed: {n_i}    details fetched: {n_d}")

    print("\n-- by doc type --")
    for t, c in con.execute(
        "SELECT doc_type, COUNT(DISTINCT doc_number) FROM v_latest_index GROUP BY doc_type ORDER BY 2 DESC"
    ):
        print(f"   {c:5d}  {t}")

    print("\n-- TDUS: reversion vs third-party (the §11926 test) --")
    rows = list(
        con.execute("SELECT sale_class, COUNT(*), ROUND(AVG(derived_price)) FROM v_auction_sales GROUP BY sale_class")
    )
    if not rows:
        print("   (no TDUS records yet)")
    for cls, c, avg in rows:
        avg_s = f"avg derived ${avg:,.0f}" if avg else ""
        print(f"   {c:5d}  {cls:20s} {avg_s}")

    print("\n-- sample derived prices --")
    for dn, rd, tax, price, _cls, gees in con.execute(
        "SELECT doc_number,recording_date,tax_amount,derived_price,sale_class,grantees"
        " FROM v_auction_sales WHERE derived_price IS NOT NULL"
        " ORDER BY recording_date DESC LIMIT 12"
    ):
        print(f"   {dn}  {rd}  tax ${tax:>9,.2f} -> ${price:>12,.0f}  {(gees or '')[:44]}")

    print("\n-- collection coverage (run_log) --")
    cov = list(
        con.execute(
            "SELECT doc_type, query_start, query_end, rows_indexed, details_fetched, notes"
            " FROM run_log ORDER BY query_start DESC, doc_type LIMIT 24"
        )
    )
    if not cov:
        print("   (no runs recorded — an empty table here means NOT COLLECTED,")
        print("    which is different from a month with no foreclosures)")
    for dt, qs, qe, ri, df, notes in cov:
        flag = ""
        if notes and notes.startswith("RESULT_CAP"):
            flag = "  << INCOMPLETE (capped)"
        elif notes and any(m in notes for m in ("PAGE_COUNT_MISMATCH", "ROW_COUNT_SHORT")):
            # Not proof of a hole, but the only evidence there would ever be:
            # a row lost at a page boundary leaves nothing else behind.
            flag = "  << SUSPECT (swept fewer rows than the server reported)"
        elif ri == 0:
            flag = "  << zero rows"
        ri_s = "-" if ri is None else str(ri)
        df_s = "-" if df is None else str(df)
        print(f"   {qs}..{qe}  {(dt or '?')[:32]:32s} idx={ri_s:>4s} det={df_s:>4s}{flag}")

    print("\n-- repeat buyers (Section 8) --")
    for b, n, tot in con.execute("SELECT buyer,purchases,total_derived FROM v_repeat_buyers LIMIT 12"):
        print(f"   {n:3d}  {b[:52]:52s} ${tot or 0:,.0f}")
    print("=" * 68)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default=(date.today() - timedelta(days=30)).isoformat())
    ap.add_argument("--end", default=date.today().isoformat())
    ap.add_argument(
        "--types",
        default="TDUS,NOTS",
        help="kept for compatibility; the server ignores the doc-type "
        "filter, so every sweep is unfiltered and selection happens "
        "against KEEP_DOCTYPES on the way into the DB",
    )
    ap.add_argument(
        "--window-days",
        type=int,
        default=3,
        help="days per search window (default 3). A month is ~130 pages "
        "unfiltered and trips the result cap; 3 days is ~14.",
    )
    ap.add_argument("--max-pages", type=int, default=40, help="refuse a window needing more pages than this")
    ap.add_argument("--no-detail", action="store_true", help="index only, skip detail fetches")
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--delay", type=float, default=1.5)
    ap.add_argument("--limit-details", type=int, default=0, help="0 = no limit")
    ap.add_argument("--report", action="store_true", help="read the DB, no network")
    ap.add_argument(
        "--report-days",
        type=int,
        default=7,
        help="Section A shows notices recorded in the last N days (default 7). 0 shows every notice collected.",
    )
    ap.add_argument("--debug", action="store_true", help="dump raw responses to ./debug")
    ap.add_argument(
        "--allow-zero-rows",
        action="store_true",
        help="treat a zero-row window as a real result instead of aborting. "
        "Only pass this once you have checked WHY it was empty — an "
        "unauthenticated session also returns zero rows.",
    )
    args = ap.parse_args()

    if args.report_days < 0:
        sys.exit("--report-days must be >= 0 (0 means no date bound)")

    con = db_open()
    if args.report:
        report(con, days=args.report_days)
        return

    if sync_playwright is None:
        sys.exit("pip install playwright && python -m playwright install chromium")

    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    # --types is no longer used to build a query: collection runs ONE unfiltered
    # sweep per window and selects against KEEP_DOCTYPES, so nothing consults
    # this flag. It is retained so existing invocations keep working, but it is
    # reported as inert rather than silently ignored. See Portal.search on why
    # the sweep is unfiltered.
    if args.types and args.types != "TDUS,NOTS":
        log(
            f"note: --types {args.types!r} does not affect the query — every sweep "
            "is unfiltered. Selection uses KEEP_DOCTYPES."
        )

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE), headless=not args.headed, viewport={"width": 1400, "height": 900}
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(f"{HOST}/Web/", wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(2500)
        if "I Accept" in page.content():
            if not args.headed:
                ctx.close()
                sys.exit("Disclaimer/CAPTCHA gate. Rerun with --headed and clear it.")
            input(">>> Clear the disclaimer/CAPTCHA, then press Enter...\n")

        portal = Portal(ctx, page, delay=args.delay, debug=args.debug)

        vocab = portal.doctype_vocab()
        log(f"vocabulary: {len(vocab)} document types")
        for k, label in DOCTYPES.items():
            if label not in vocab:
                log(f"  !! '{label}' NOT IN VOCABULARY — county may have renamed it")
            else:
                log(f"  {k}: id={vocab[label]}  '{label}'")

        # ONE unfiltered sweep per window, not one per doc type. The server
        # discards the doc-type filter, so searching per type issued N identical
        # requests and stored the same rows N times.
        capped_windows = []
        for qs, qe in windows(start, end, args.window_days):
            log(f"sweep  {qs} .. {qe}  (unfiltered — the server ignores the type filter)")
            run_id = run_log_start(con, "ALL (unfiltered sweep)", qs, qe)
            rows = []

            # A cap is fatal for this window: it returned an incomplete set, not
            # an empty one, and continuing would store a truncated window that
            # looks complete. §5.1 / AI_CONTEXT.md rule 4.
            try:
                rows = portal.search(None, qs, qe, max_pages=args.max_pages)
            except ResultCapExceeded as e:
                log(f"  !! RESULT CAP: {e}")
                log("     window is INCOMPLETE — narrow it and re-run. Nothing stored.")
                run_log_finish(con, run_id, 0, 0, f"RESULT_CAP: {e}")
                capped_windows.append(f"{qs}..{qe}")
                continue
            except UnexpectedResponse as e:
                # The session died or the portal changed shape. Every remaining
                # window would fail the same way, so stop rather than grind.
                run_log_finish(con, run_id, 0, 0, f"UNEXPECTED_RESPONSE: {e}")
                ctx.close()
                sys.exit(f"\n!! {e}\n")
            except RuntimeError as e:
                log(f"  ! {e}")

            # An unexpected zero-row window is a loud failure, not a result. The
            # portal returns an empty grid both for "nothing matched" and for
            # "your session is no longer authenticated", and storing the second
            # as the first poisons the dataset with absence. Aborting the whole
            # run rather than this window is deliberate: a dead session returns
            # zero for EVERY window. AI_CONTEXT.md rule 3.
            if not rows:
                if args.allow_zero_rows:
                    log("  zero rows accepted (--allow-zero-rows)")
                    run_log_finish(con, run_id, 0, 0, "ZERO_ROWS: accepted via --allow-zero-rows")
                    continue
                run_log_finish(con, run_id, 0, 0, "ZERO_ROWS: aborted run")
                ctx.close()
                sys.exit(
                    f"\n!! ZERO ROWS for the sweep {qs}..{qe}.\n"
                    "   An unfiltered window should return hundreds of documents, so\n"
                    "   this is a failure, not an empty window. Check, in order:\n"
                    "     1. Is the session still authenticated? Re-run with --headed\n"
                    "        and look for the disclaimer/CAPTCHA gate.\n"
                    "     2. Did the portal change? Re-run with --debug and inspect\n"
                    "        ./debug/001_results_p1.txt for the app shell instead of a grid.\n"
                    "   If the window is genuinely empty, re-run with --allow-zero-rows.\n"
                )

            # Selection happens HERE, because the server would not do it. Keeping
            # everything would mean collecting Substitution Of Trustee at
            # 1016/mo, which rule 7 forbids.
            keep = [r for r in rows if r["doc_type"] in KEEP_DOCTYPES]
            mix = {}
            for r in keep:
                mix[r["doc_type"]] = mix.get(r["doc_type"], 0) + 1
            log(f"  {len(rows)} documents in window; keeping {len(keep)} in scope: {mix or '{}'}")

            store_index(con, keep, qs, qe)

            note = f"swept={len(rows)}; kept={len(keep)}; mix={mix}"
            # Pagination warnings belong in run_log, not just the console: they
            # are the only record that a given window may be short a row.
            if portal.sweep_warnings:
                note += "; " + "; ".join(portal.sweep_warnings)
            if args.no_detail:
                run_log_finish(con, run_id, len(keep), 0, note + "; --no-detail")
                continue

            # Details only for the types whose tax amount the price derivation
            # needs — ~31/month rather than ~1300.
            todo = [r for r in keep if r["detail_id"] and r["doc_type"] in DETAIL_DOCTYPES]
            if args.limit_details:
                todo = todo[: args.limit_details]
            n_ok = 0
            for i, r in enumerate(todo, 1):
                try:
                    d = parse_detail(portal.detail(r["detail_id"]))
                    store_detail(con, r["doc_number"], r["detail_id"], d)
                    n_ok += 1
                    log(f"    [{i}/{len(todo)}] {r['doc_number']} tax={d.get('tax_raw')}")
                except Exception as e:
                    log(f"    [{i}/{len(todo)}] {r['doc_number']} FAILED: {e}")
                    store_detail(con, r["doc_number"], r["detail_id"], {}, ok=False)
            run_log_finish(con, run_id, len(keep), n_ok, note)
        ctx.close()

    report(con, days=args.report_days)

    # A capped window means the dataset has a known hole. Exit non-zero so a
    # caller (or the agent pipeline) cannot mistake this run for a clean one.
    if capped_windows:
        sys.exit(
            "\n!! RESULT CAP hit on "
            f"{len(capped_windows)} window(s): {', '.join(capped_windows)}\n"
            "   Those windows stored NOTHING and are incomplete. Narrow them "
            "(try --start/--end a fortnight at a time) and re-run.\n"
        )


if __name__ == "__main__":
    main()
