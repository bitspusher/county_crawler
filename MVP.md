# San Joaquin County Foreclosure Tracker — MVP Spec

*Draft v2 — July 2026. Supersedes v1. Changes marked **[v2]**.*

---

## 1. Goal

A weekly list of San Joaquin County properties going to trustee's sale, plus what comparable properties actually cleared for at recent auctions.

Primary user: **investors and auction buyers.** Factual, cold, no hand-holding.

**[v2] Caveat now attached to that goal:** the recorder index does not carry an address or APN. "Properties" is currently aspirational — see §6.3. Until the parcel bridge is built, this is a list of *transactions*, not properties.

---

## 2. Hypothesis to test

1. **Freshness.** Recorded notices can be surfaced meaningfully earlier than national aggregators publish them. *Untested.* Requires a side-by-side against PropStream or Foreclosure.com on the same week's notices.
2. **Auction comps are a gap.** Nobody publishes a clean feed of what distressed properties actually clear at, at auction, in this county.

**[v2] Hypothesis 2 has partial support and a new problem.** Sale prices *are* derivable (§6.4), which was the main technical risk. But the honest volume is far lower than the raw document count suggests, and the comps carry no address. See §7.3.

**[v2] Third hypothesis, added:** that a buyer will pay for auction comps with **no property identifier attached**. If the parcel bridge proves expensive, this becomes the load-bearing question.

---

## 3. Scope

### In

- Notice of Trustees Sale (NOTS) — upcoming auctions
- Trustees Deed Under Default (TDUS) — completed auctions
- **[v2]** Rescission Of Default and Cancellation/Termination — see below
- Parcel data — for assessed value and, if possible, an APN bridge
- One weekly output

### [v2] Promoted into scope: rescissions and cancellations

A recorded NOTS is not a live auction. Sales get cancelled and postponed, sometimes by verbal announcement at the sale that is never recorded at all. Publishing an unfiltered NOTS list means publishing auctions that will not happen.

The index carries `Rescission Of Default` (55/mo) and `Cancellation/Termination` (113/mo). These must be collected and joined out, or Section A is knowingly wrong. This is a correctness requirement, not a feature.

Note the residual risk: postponements announced orally at the sale never reach the recorder. No amount of collection fixes that. The weekly output should say so.

### Explicitly out (for now)

- Notice of Default and the equity play — **[v2]** note the index label is `Default` (81/mo), not "Notice of Default"
- Any prediction or modeling
- Scoring / ranking
- Court records — California is non-judicial; nothing to collect from Superior Court
- Tax delinquency — five-year runway makes it a slow-burn risk factor, not a trigger
- Web UI, accounts, notifications beyond a plain email or page
- Any homeowner-facing product

### [v2] Explicitly out, newly identified

- **Substitution Of Trustee** (1016/mo). Not a distress signal. It runs at nearly the same volume as `Reconveyance` (1183/mo) because servicers substitute a trustee immediately before reconveying a paid-off loan. Treating it as foreclosure would swamp the product in false positives. Named here so the mistake isn't made later.

---

## 4. Background: the California timeline

Non-judicial foreclosure, Civil Code §2924. The whole sequence lives in recorded documents.

**[v2] With the county's actual index labels and observed monthly volumes** (window: Jun 25 – Jul 25, 2026):

| Stage | County index label | Doc type ID | Vol/mo | Status |
|---|---|---|---|---|
| 1 | `Default` | — | 81 | Roadmap |
| 2 | `Notice Of Trustees Sale` | **41** | 35 | **MVP** |
| 3 | `Trustees Deed Under Default` | **22** | 31 | **MVP** |
| — | `Rescission Of Default` | — | 55 | **[v2] MVP** |
| — | `Cancellation/Termination` | — | 113 | **[v2] MVP** |
| — | `Substitution Of Trustee` | 51 | 1016 | Excluded |

The county's vocabulary differs from the statutory names in every case. "Trustee's Deed Upon Sale" does not exist here; it is `Trustees Deed Under Default`. Worth remembering for multi-county expansion — the Tyler Eagle *platform* is portable, the *vocabulary* is not.

---

## 5. Data sources

### 5.1 Recorder — Tyler Eagle **[v2: unknowns resolved]**

Portal: `https://sanjoaquincountyca-web.tylerhost.net/Web/` — Eagle Self-Service 2026.1.5.

**Search definition:** `DOCSEARCH3032S7` (advanced search; the basic form is S6 and does not expose the document-type filter — use S7).

**Endpoints:**

| Purpose | Call |
|---|---|
| Doc type vocabulary | `GET /Web/search/documentTypes/DOCSEARCH3032S7?searchText=&maxValues=1000` → JSON `[{"name":"41","value":"Notice Of Trustees Sale"}, …]` |
| Set search state | `POST /Web/searchPost/DOCSEARCH3032S7` |
| Read results | `GET /Web/searchResults/DOCSEARCH3032S7?page=N` |
| Document detail | `GET /Web/document/{DOCID}?search=DOCSEARCH3032S7` |

Search is **two steps**: the POST returns a 56-byte acknowledgement and sets server-side state; the results are fetched separately. Pagination is a plain integer — no cursor, no continuation token. Backfill is therefore cheap: roughly 12 requests per doc type per year of history.

**POST parameters:**

```
field_DocNumID
field_RecDateID_DOT_StartDate           MM/DD/YYYY
field_RecDateID_DOT_EndDate             MM/DD/YYYY
field_BothNamesID           + -containsInput=Contains Any
field_GrantorID             + -containsInput=Contains Any
field_GranteeID             + -containsInput=Contains Any
field_selfservice_documentTypes  + -containsInput=Contains Any
field_UseAdvancedSearch
```

**Result cap.** An unfiltered 30-day query returns *no rows at all* — only a facet sidebar and "We found more documents than the maximum allowed." Every query must be narrowed by document type, and windows kept small. At 31–35/mo the MVP doc types fit comfortably in a one-month window. The collector must treat a cap message as an error, never as an empty result.

**Access gate.** The portal is behind a Google reCAPTCHA posting to `/Web/checkHuman`, plus a disclaimer at `/Web/user/disclaimer`. Clearance persists **per browser profile**, not per session — a persistent profile survives across runs, so the human step is occasional rather than weekly. There is also a `/Web/session/pingSession` heartbeat, which means network-idle waits never settle; automation must not depend on them.

**[v2] Open engineering issue.** Driving these endpoints programmatically from within an authenticated browser context currently returns the app shell rather than JSON/result HTML. The same calls succeed when a human drives the UI. If header emulation can't be made to work, the fallback is UI automation — fill the form, click Search, read the DOM, click through pagination. At ~30 records/month that costs perhaps two minutes a week and stops fighting the framework. Not yet resolved.

Caveats retained from v1: the grantor/grantee index covers 1968 forward; the county states the index is a finding aid, not something to rely on for decisions about the underlying document. Fine for a lead list, not for title work. Document *bodies* are scanned images behind per-page fees (there is a `/Web/cart`); MVP uses index and detail metadata only.

### 5.2 Parcels — open data

- `https://opendata.sjgov.org` — parcels via ArcGIS REST, plus CSV / GeoJSON / KML
- `https://sjmap.org` — shapefile extracts, refreshed roughly twice a year

Pull once, refresh occasionally, join locally. No scraping needed.

### 5.3 **[v2] New: AG §2924m trustee-sale database**

Under Civil Code §2924m the California Attorney General publishes a searchable database of residential trustee sales carrying **county, address, APN, sale date, and winning bidder**. It is partial — only sales to "eligible bidders," lagged to the deemed-final date — and carries no sale price, so it does not replace TDUS collection.

Its value is threefold: zero-cost ground truth for validating the parser, a possible source of the APN the recorder index lacks, and an immediate head start on §8 validation with no scraping at all.

### 5.4 **[v2] New: published legal notices**

Every NOTS must be published in a newspaper of general circulation. Those notices carry **APN, property address, and opening bid** — all three absent from the recorder index, and opening bid is a Section A field with no other source. Worth investigating as the primary parcel bridge.

### 5.5 Not in MVP

Treasurer-Tax Collector Property Tax Default list, published as a parseable PDF. Five-year runway makes it a slow-burn risk factor. Roadmap.

---

## 6. Data model

**Append-only observations, SQLite.** Store every observation with `observed_at` rather than overwriting. History is the product.

**[v2] Stronger form of the same rule: store only what the portal said; derive everything else in views.** Sale price is not a stored column — it is computed from the recorded tax amount in a view. When the transfer-tax assumption gets refined (§6.4), that is a one-line change and a view rebuild, not a re-scrape.

### 6.1 **[v2]** What the index actually contains

Result rows carry:

| Field | Example |
|---|---|
| Document number | `2026-067463` — year-sequence, stable primary key |
| Document type | `Trustees Deed Under Default` |
| Recording date | `07/24/2026` |
| Grantor (n) | `ZBS LAW LLP`, `WARN MICHAEL JAMES`, `WARN STEPHANIE` |
| Grantee (n) | `AHMAD UZAIR Q`, `AHMAD SHAISTA A` |
| Detail link | `/Web/document/DOC3765S8263` |

Detail views add:

| Field | Example |
|---|---|
| Number of pages | `3` |
| Recording fee | `$22.00` |
| **Tax amount** | **`$445.50`** |

Results render as jQuery Mobile `<div>` listviews, not tables. Parse on the `ss-search-row` container; there are no `<th>` elements to key on.

### 6.2 Parsing approach

Everything is structured data, not natural language. Recorder index → CSS selectors on rendered HTML. Parcels → already JSON.

**No LLM at runtime.** Deterministic parsers fail loudly; models fail silently, and a misread digit in a price or APN quietly poisons the database. Use a model at *development* time to write the parsers, then ship something deterministic.

**[v2]** Parsers are text-line based rather than markup-structural — Eagle's div soup changes between releases, but rendered label/value text is stable. Both parsers are unit-tested against fixtures captured from live markup. A zero-row result where ~30 were expected raises a warning rather than succeeding quietly.

### 6.3 **[v2] The parcel bridge — v1's biggest risk, now confirmed real**

v1 flagged "if the Eagle index doesn't carry APN, an assessor lookup becomes the bridge. Confirm this early; it's the single biggest schema risk."

**Confirmed: there is no APN and no property address anywhere** — not in the result grid, not in the detail view. 557KB of rendered results across 100 records contained zero APN-shaped values. The county's help text references a searchable "Parcel #" field, so the underlying index may hold one, but it is not exposed in any view reachable without paying for document images.

The collector therefore produces a price dataset with no way to say which property each price refers to. Three candidate bridges, in rough order of promise:

1. **Owner name → assessor parcel data.** The defaulting owner appears as a grantor on both NOTS and TDUS, and parcel data carries owner-of-record. The pre-sale owner is exactly the right vintage. Messy, but free and immediate.
2. **Published legal notices** (§5.4). Carries APN, address, and opening bid. Probably the highest-quality bridge.
3. **AG §2924m dataset** (§5.3). Verified APN and address for a subset; best used as ground truth to score the other two.

Until one of these works, this is not yet a property tracker.

### 6.4 **[v2] Sale price, derived from documentary transfer tax**

The index carries no price field, but the detail view carries **Tax Amount** — the documentary transfer tax, which is levied on consideration. San Joaquin County's rate is **$1.10 per $1,000**, with value rounded up to the nearest $500 before the rate applies.

```
derived_price = tax_amount ÷ 1.10 × 1000        (±$500)
```

Worked example, from a real record: `2026-067463`, tax `$445.50` → **$405,000**.

Two things to verify before trusting any number:

- **Stockton is a charter city** with its own ordinance, listed as $0.55 county + $0.55 city. Whether the recorder's Tax Amount captures the combined $1.10 or only the county's half is unverified. If only half, Stockton — the county's largest city — is wrong by 2×. Test by comparing derived prices for Stockton against Lodi or Tracy properties with known sale prices.
- **General-law cities** (Escalon, Lathrop, Lodi) split the same $1.10 total, so the countywide formula should hold there.

### 6.5 **[v2] The credit-bid problem, and a free discriminator**

Most trustee sales do not sell to a third party. The beneficiary credit-bids and takes the property back as REO — and every one of those still records a `Trustees Deed Under Default`. Lender reversions clear at approximately the debt owed, which bears no relation to market value. Averaging them together with genuine sales produces a meaningless ratio.

**Revenue & Taxation Code §11926** exempts a trustee's deed to the beneficiary to the extent of the debt. So lender credit-bids should record **$0.00 transfer tax** while third-party purchases record the real amount. If that holds, the same field that yields price also cleanly separates the two populations — no name-matching required.

**Status: hypothesis, not yet validated.** The first collector run tests it. Expect a bimodal distribution: a cluster at exactly $0.00 and a spread of real values. If everything carries tax, fall back to grantee-name classification (bank or "as trustee for" → reversion; LLC or individual → genuine purchase).

The v1 record already looks right: `ZBS LAW LLP` (trustee firm) + defaulting owners as grantors, two individuals as grantees, $445.50 tax. That is a real third-party purchase.

---

## 7. Output

A weekly list. Email or a plain page. No UI.

### 7.1 Section A — Coming up for auction

Sourced from NOTS, **[v2]** minus anything with a matching rescission or cancellation.

| Field | Source | **[v2] Status** |
|---|---|---|
| Document number | recorder index | available |
| Recording date | recorder index | available |
| Owner name | recorder index (grantor) | available |
| Trustee firm | recorder index (grantor) | available |
| Address | parcel bridge | **not yet available** |
| APN | parcel bridge | **not yet available** |
| Sale date | NOTS body / legal notice | **not in index** |
| Opening bid | legal notice | **not in index** |
| Assessed value | parcel data | blocked on bridge |

Note that **sale date is not in the index** — only the recording date of the notice. For a product whose headline is "properties going to trustee's sale," the auction date arguably matters more than anything else in the table. It lives in the document body or the published legal notice.

### 7.2 Section B — Just closed

Sourced from TDUS, previous 7 days.

| Field | Source | **[v2] Status** |
|---|---|---|
| Document number | recorder index | available |
| Recording date | recorder index | available |
| Buyer | recorder index (grantee) | available |
| Sale price | **derived from tax amount** | available, pending §6.4 validation |
| Sale class | **derived from $0 tax** | available, pending §6.5 validation |
| Address / APN | parcel bridge | **not yet available** |
| Assessed value | parcel data | blocked on bridge |
| Price / assessed ratio | computed | blocked on bridge |

**Assessed value caveat** (unchanged): Prop 13 means a long-held home is assessed well below market. A rough yardstick, not a valuation. Free and directionally useful; don't present it as market value.

### 7.3 **[v2] Honest volume**

35 NOTS and 31 TDUS per month. After removing lender reversions, genuine third-party auction purchases are plausibly **2–4 per week**, and the comps dataset accumulates at perhaps 150–250 records a year rather than 370.

Thin. Not necessarily fatal — thin-but-exclusive can sell, and a backfill bootstraps history cheaply now that pagination is known to be simple. But it sharpens the §8 question considerably, and it moves multi-county expansion from "roadmap" toward "precondition for the product to be interesting."

---

## 8. Validation

**The customer list is already in the data.** The same LLCs and individuals appear repeatedly as grantees on trustee's deeds. Group grantee names over TDUS records, count, sort. One of them saying "yes, I'd pay for that" is worth more than a polished tool nobody asked for.

**[v2] This is now also a collector feature, not just a research step** — the same grantee grouping that finds customers also classifies reversions (§6.5).

**[v2] Two refinements:**

- Exact-string grouping is not good enough for a customer list. `AHMAD UZAIR Q | AHMAD SHAISTA A` and `AHMAD UZAIR` will count as different buyers. Fine for a first look at who's active; needs normalisation before outreach.
- **Start now, without the scraper.** The AG §2924m dataset (§5.3) carries winning bidder names and needs no collection. Repeat-buyer analysis can begin today and is entirely independent of the unresolved automation issue in §5.1.

**[v2] Validation questions in priority order:**

1. Does the §11926 zero-tax split hold? *(First collector run answers it.)*
2. Does the derived price match known sale prices, in Stockton and elsewhere?
3. Will a buyer pay for comps with no address attached?
4. Is the freshness claim true at all? *(Still completely untested — hypothesis 1 has had no work done on it.)*

---

## 9. Roadmap (parked, deliberately)

- **`Default` collection + equity estimate.** The default amount against assessed or market value separates a lead worth chasing from a dead one — and it's precisely what paid lead lists don't give you. Highest-value next step. **[v2]** 81/mo, roughly 2.3× NOTS volume.
- **Scoring / ranking.** Weight signals by strength, decay by age, bonus for stacked signals. Keep it as a **view**, not baked into collection, so retuning never means re-scraping. Arithmetic, not modeling.
- **Tax default collector.**
- **Prediction.** Needs history that doesn't exist yet.
- **Homeowner-facing product.** Genuinely valuable and underserved, but a trust-and-service business needing a human and possibly a license. Different company, different face. Don't build both.
- **Multi-county expansion.** **[v2]** Eased by the Eagle pattern — endpoint shapes and field names transfer directly, only the search-definition ID and document-type vocabulary change per county. Given §7.3, possibly not optional.

---

## 10. Legal and operational notes

- **Civil Code §2945** (foreclosure consultants) and **§1695** (purchases from owners already in default) impose contract, disclosure, and rescission requirements with real teeth. These bite on *outreach*, not on collecting public records — but read them before designing any homeowner contact.
- The recorder portal carries an indemnification clause in its terms. Review before automating; rate-limit politely regardless.
- **[v2]** The portal is behind a CAPTCHA. Do not defeat it — solve it manually, once per profile. Automated solving would be both a terms problem and a permanently adversarial engineering position.
- **[v2] Ask the Recorder for bulk data.** This has moved from footnote to recommended path. A subscription or bulk extract would likely be cheaper than the engineering time this portal will consume, would remove the CAPTCHA question entirely, and might carry the APN the public views omit. It is also the fastest possible backfill. Recorder: 209-468-3939. If no product exists, a CPRA request under Gov. Code §6253.9 covers electronic records.
- Check the SB 272 Enterprise System Catalog before scraping anything new.

---

## 11. **[v2] Next implementation steps**

v1's three questions are answered: endpoints and pagination (§5.1), document type IDs (§4), APN presence (§6.3 — absent).

In order:

1. **Resolve the automation issue** (§5.1). Header emulation or UI automation. Everything downstream is blocked on this.
2. **Run one month of TDUS with details.** Validates the §11926 split and the price derivation in a single ~31-record pass.
3. **Spot-check derived prices** against known sales, including at least one Stockton property, to settle the charter-city rate question.
4. **Start the repeat-buyer analysis from the AG dataset.** Independent of 1–3; can begin immediately.
5. **Investigate the parcel bridge** — legal notices first, owner-name join second.
6. **Make the bulk-data call.** Cheap, and could obsolete steps 1 and 5 entirely.

Steps 4 and 6 are the ones with the best ratio of information gained to effort spent, and neither depends on any code being written.
