"""Derivation-view tests — MVP.md §6, §6.4, §6.5.

These views are where the project's two unvalidated assumptions live, and
AI_CONTEXT.md rule 1 says they are the only place they may live. That makes the
views the highest-value test target in the repo: they can be exercised
completely without the portal, and revising an assumption later is a view
rebuild whose correctness these tests protect.

Note what these tests do NOT do: they verify the derivation is implemented as
specified, not that the specification is right. Whether the DTT rate is $1.10
per $1,000 for a Stockton property, and whether a $0.00 tax really means a
credit bid, are open questions that only a live TDUS month can answer
(ROADMAP Phase 2).
"""

import pytest

from collect_sjc import DOCTYPES, DTT_RATE_PER_1000

pytestmark = pytest.mark.unit


# ── derived_price (§6.4) ─────────────────────────────────────────────────


def test_derives_price_from_tax_at_the_documented_rate(db, add_tdus):
    add_tdus("2026-000001", tax_amount=451.00)
    price = db.execute("SELECT derived_price FROM v_auction_sales").fetchone()[0]
    # $451.00 at $1.10/$1,000 => $410,000
    assert price == pytest.approx(410_000, abs=1)


def test_derived_price_tracks_the_rate_constant(db, add_tdus):
    """The rate must be applied, not hardcoded into an expected number.

    If DTT_RATE_PER_1000 changes — and ROADMAP Phase 2 may well make it
    city-dependent — this test follows it instead of failing spuriously.
    """
    add_tdus("2026-000001", tax_amount=1_100.00)
    price = db.execute("SELECT derived_price FROM v_auction_sales").fetchone()[0]
    assert price == pytest.approx(1_100.00 / DTT_RATE_PER_1000 * 1000.0, abs=1)


def test_price_is_not_stored_anywhere(db, add_tdus):
    """Rule 1: no table may carry a derived value.

    Guards against a well-meaning "cache the price for speed" change. At ~30
    rows/month (§7.3) there is nothing to optimise, and a stored price would
    survive a rate revision as a stale lie.
    """
    add_tdus("2026-000001", tax_amount=451.00)
    for table in ("index_obs", "detail_obs", "party_obs"):
        cols = {r[1].lower() for r in db.execute(f"PRAGMA table_info({table})")}
        assert not cols & {"price", "derived_price", "sale_class"}, (
            f"{table} carries a derived column — see AI_CONTEXT.md rule 1"
        )


def test_zero_tax_yields_no_price(db, add_tdus):
    """A credit bid has no purchase price to derive. NULL, not 0."""
    add_tdus("2026-000001", tax_amount=0.0)
    price = db.execute("SELECT derived_price FROM v_auction_sales").fetchone()[0]
    assert price is None


def test_missing_tax_yields_no_price(db, add_tdus):
    add_tdus("2026-000001", tax_amount=None)
    price = db.execute("SELECT derived_price FROM v_auction_sales").fetchone()[0]
    assert price is None


# ── sale_class, the §11926 split (§6.5) ──────────────────────────────────


def test_zero_tax_classifies_as_reversion(db, add_tdus):
    add_tdus("2026-000001", tax_amount=0.0)
    assert db.execute("SELECT sale_class FROM v_auction_sales").fetchone()[0] == "likely_reversion"


def test_real_tax_classifies_as_third_party(db, add_tdus):
    add_tdus("2026-000001", tax_amount=451.00)
    assert db.execute("SELECT sale_class FROM v_auction_sales").fetchone()[0] == "likely_third_party"


def test_missing_tax_is_unknown_not_reversion(db, add_tdus):
    """A failed detail fetch must not masquerade as a credit bid.

    'unknown' and 'likely_reversion' both carry a NULL price; conflating them
    would let a fetch failure inflate the reversion rate and corrupt the very
    measurement ROADMAP Phase 2 uses to validate the split.
    """
    add_tdus("2026-000001", tax_amount=None)
    assert db.execute("SELECT sale_class FROM v_auction_sales").fetchone()[0] == "unknown"


# ── scope of v_auction_sales / v_upcoming (§3) ───────────────────────────


def test_auction_sales_holds_only_completed_sales(db, add_tdus):
    add_tdus("2026-000001", tax_amount=451.00, doc_type=DOCTYPES["TDUS"])
    add_tdus("2026-000002", tax_amount=None, doc_type=DOCTYPES["NOTS"])
    docs = [r[0] for r in db.execute("SELECT doc_number FROM v_auction_sales")]
    assert docs == ["2026-000001"]


def test_upcoming_holds_only_notices(db, add_tdus):
    add_tdus("2026-000001", tax_amount=451.00, doc_type=DOCTYPES["TDUS"])
    add_tdus("2026-000002", tax_amount=None, doc_type=DOCTYPES["NOTS"])
    docs = [r[0] for r in db.execute("SELECT doc_number FROM v_upcoming")]
    assert docs == ["2026-000002"]


def test_upcoming_does_not_yet_exclude_rescissions(db, add_tdus):
    """Documents the KNOWN defect rather than asserting correct behaviour.

    ROADMAP Gap 1 / Phase 3: rescissions and cancellations are not collected, so
    v_upcoming still lists sales that will not happen. §3 calls this a
    correctness requirement. When Phase 3 lands, this test should be replaced by
    one asserting the exclusion join — and its failure is the signal to do so.
    """
    add_tdus("2026-000002", tax_amount=None, doc_type=DOCTYPES["NOTS"])
    n = db.execute("SELECT COUNT(*) FROM v_upcoming").fetchone()[0]
    assert n == 1, "if this changed, Phase 3 may have landed — update this test"


# ── append-only semantics (§6) ───────────────────────────────────────────


def test_latest_index_keeps_only_the_newest_observation(db, add_tdus):
    add_tdus("2026-000001", tax_amount=451.00, recording_date="07/15/2026", observed_at="2026-07-20T10:00:00")
    add_tdus("2026-000001", tax_amount=451.00, recording_date="07/16/2026", observed_at="2026-07-25T10:00:00")
    rows = list(db.execute("SELECT recording_date FROM v_latest_index"))
    assert len(rows) == 1
    assert rows[0][0] == "07/16/2026"


def test_re_collection_adds_rows_rather_than_replacing_them(db, add_tdus):
    """Two observations of one document is the expected state, not a conflict."""
    add_tdus("2026-000001", tax_amount=451.00, observed_at="2026-07-20T10:00:00")
    add_tdus("2026-000001", tax_amount=460.00, observed_at="2026-07-25T10:00:00")
    assert db.execute("SELECT COUNT(*) FROM index_obs").fetchone()[0] == 2
    assert db.execute("SELECT COUNT(*) FROM detail_obs").fetchone()[0] == 2


def test_latest_detail_wins_and_moves_the_derived_price(db, add_tdus):
    add_tdus("2026-000001", tax_amount=451.00, observed_at="2026-07-20T10:00:00")
    add_tdus("2026-000001", tax_amount=1_100.00, observed_at="2026-07-25T10:00:00")
    price = db.execute("SELECT derived_price FROM v_auction_sales").fetchone()[0]
    assert price == pytest.approx(1_000_000, abs=1)


def test_failed_detail_fetches_are_excluded(db, add_tdus):
    """fetch_ok=0 rows are kept for the audit trail but must never be read.

    A failed fetch parses to tax_amount NULL; if v_latest_detail took it as the
    newest observation it would overwrite a good earlier reading with 'unknown'.
    """
    add_tdus("2026-000001", tax_amount=451.00, observed_at="2026-07-20T10:00:00")
    add_tdus("2026-000001", tax_amount=None, observed_at="2026-07-25T10:00:00", fetch_ok=0)
    assert db.execute("SELECT COUNT(*) FROM detail_obs").fetchone()[0] == 2
    assert db.execute("SELECT COUNT(*) FROM v_latest_detail").fetchone()[0] == 1
    price = db.execute("SELECT derived_price FROM v_auction_sales").fetchone()[0]
    assert price == pytest.approx(410_000, abs=1)


# ── v_repeat_buyers, the customer list (§8) ──────────────────────────────


def test_ranks_buyers_by_purchase_count(db, add_tdus):
    add_tdus("2026-000001", tax_amount=451.00, grantee="ACME CAPITAL LLC")
    add_tdus("2026-000002", tax_amount=451.00, grantee="ACME CAPITAL LLC")
    add_tdus("2026-000003", tax_amount=451.00, grantee="SOLO BUYER INC")
    rows = list(db.execute("SELECT buyer, purchases FROM v_repeat_buyers"))
    assert rows[0] == ("ACME CAPITAL LLC", 2)


def test_reversions_are_excluded_from_the_buyer_list(db, add_tdus):
    """Lenders taking property back are not customers.

    This is the payoff of the §11926 split: without it the buyer list is topped
    by whichever servicer credit-bid most often, which is useless as a lead list.
    """
    add_tdus("2026-000001", tax_amount=0.0, grantee="BIG BANK NA AS TRUSTEE")
    add_tdus("2026-000002", tax_amount=451.00, grantee="ACME CAPITAL LLC")
    buyers = [r[0] for r in db.execute("SELECT buyer FROM v_repeat_buyers")]
    assert buyers == ["ACME CAPITAL LLC"]


def test_grouping_is_exact_string_as_the_spec_notes(db, add_tdus):
    """§8 acknowledges exact-string grouping; this pins the limitation.

    'ACME CAPITAL LLC' and 'ACME CAPITAL, LLC' are two buyers today. Fuzzy
    grouping is not in MVP scope, so the test records the behaviour rather than
    wishing it away.
    """
    add_tdus("2026-000001", tax_amount=451.00, grantee="ACME CAPITAL LLC")
    add_tdus("2026-000002", tax_amount=451.00, grantee="ACME CAPITAL, LLC")
    n = db.execute("SELECT COUNT(*) FROM v_repeat_buyers").fetchone()[0]
    assert n == 2


def test_totals_derived_spend_per_buyer(db, add_tdus):
    add_tdus("2026-000001", tax_amount=1_100.00, grantee="ACME CAPITAL LLC")
    add_tdus("2026-000002", tax_amount=1_100.00, grantee="ACME CAPITAL LLC")
    total = db.execute("SELECT total_derived FROM v_repeat_buyers").fetchone()[0]
    assert total == pytest.approx(2_000_000, abs=2)


# ── no address, no APN (§6.3) ────────────────────────────────────────────


def test_no_view_or_table_claims_an_address_or_apn(db):
    """Rule 8: the index carries neither. A column named for one would be a lie.

    The parcel bridge (ROADMAP Phase 5) is where an APN could legitimately
    enter, and it will arrive as a join against another source — not as a
    column the recorder never gave us.
    """
    names = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view')")]
    for name in names:
        cols = {r[1].lower() for r in db.execute(f"PRAGMA table_info({name})")}
        assert not cols & {"address", "apn", "parcel", "situs", "street"}, (
            f"{name} claims a property identifier the index does not contain"
        )
