"""Tests for the unfiltered-sweep design and the §7 report sections.

Measured live on 2026-07-29: the portal discards the doc-type filter, so a search
returns every type recorded in the window. Selection moved client-side, which
makes KEEP_DOCTYPES and the day-based windows load-bearing — they are the only
things standing between the dataset and 1016 Substitution Of Trustee records a
month.
"""

import itertools
from datetime import date

import pytest

from collect_sjc import (
    DETAIL_DOCTYPES,
    DOCTYPES,
    KEEP_DOCTYPES,
    ResultCapExceeded,
    _mmddyyyy_sort_key,
    report_sections,
    windows,
)
from tests.conftest import EMPTY_PAGE as EMPTY
from tests.conftest import FakeCtx, FakePage, make_portal

pytestmark = pytest.mark.unit


# ── windows() ────────────────────────────────────────────────────────────


def test_splits_into_windows_of_the_requested_size():
    got = list(windows(date(2026, 6, 1), date(2026, 6, 9), 3))
    assert got == [
        (date(2026, 6, 1), date(2026, 6, 3)),
        (date(2026, 6, 4), date(2026, 6, 6)),
        (date(2026, 6, 7), date(2026, 6, 9)),
    ]


def test_final_window_is_clamped_to_the_end_date():
    got = list(windows(date(2026, 6, 1), date(2026, 6, 5), 3))
    assert got == [(date(2026, 6, 1), date(2026, 6, 3)), (date(2026, 6, 4), date(2026, 6, 5))]


def test_windows_never_overlap():
    """An overlap would double-store every document in the overlap.

    Harmless for correctness — the tables are append-only and the views take the
    latest observation — but it wastes requests against a county server, which
    §10 cares about.
    """
    got = list(windows(date(2026, 1, 1), date(2026, 3, 31), 3))
    for (_, prev_end), (next_start, _) in itertools.pairwise(got):
        assert next_start > prev_end
        assert (next_start - prev_end).days == 1


def test_single_day_window():
    assert list(windows(date(2026, 6, 1), date(2026, 6, 1), 3)) == [(date(2026, 6, 1), date(2026, 6, 1))]


def test_window_size_of_one_day():
    got = list(windows(date(2026, 6, 1), date(2026, 6, 3), 1))
    assert len(got) == 3
    assert all(s == e for s, e in got)


def test_zero_day_window_is_rejected():
    """Silently treating 0 as 1 would loop forever on a bad flag value."""
    with pytest.raises(ValueError):
        list(windows(date(2026, 6, 1), date(2026, 6, 3), 0))


# ── KEEP_DOCTYPES ────────────────────────────────────────────────────────


def test_keeps_exactly_the_mvp_in_scope_types():
    assert {
        "Notice Of Trustees Sale",
        "Trustees Deed Under Default",
        "Rescission Of Default",
    } == KEEP_DOCTYPES


def test_substitution_of_trustee_is_never_kept():
    """AI_CONTEXT.md rule 7. At 1016/mo it would swamp everything else.

    The sweep unavoidably RETRIEVES it — the server sends the whole window — so
    this set is the only thing that stops it being stored.
    """
    assert "Substitution Of Trustee" not in KEEP_DOCTYPES


@pytest.mark.parametrize(
    "noise",
    [
        "Reconveyance",
        "Deed",
        "Deed Of Trust",
        "Fictitious Business Name",
        "Substitution Of Trustee",
        "Lien",
        "Cancellation/Termination",
    ],
)
def test_high_volume_noise_types_are_discarded(noise):
    """Every one of these appeared in the live 3-day sweep, several in the hundreds."""
    assert noise not in KEEP_DOCTYPES


def test_the_two_mvp_types_are_kept():
    assert DOCTYPES["NOTS"] in KEEP_DOCTYPES
    assert DOCTYPES["TDUS"] in KEEP_DOCTYPES


def test_phase_3_type_arrives_with_the_sweep():
    """Rescissions are what make a NOTS list honest (§3).

    They cost nothing extra: the unfiltered sweep already returns them, so
    ROADMAP Phase 3 needs only the exclusion join, not new collection.
    """
    assert "Rescission Of Default" in KEEP_DOCTYPES


def test_cancellation_termination_is_not_collected():
    """Dropped 2026-07-30 on measurement, and it must not drift back in.

    All 83 June 2026 documents were City of Stockton lien releases, rooftop-solar
    UCC terminations and homebuilder filings — not one foreclosure trustee firm
    on any of them. It is a generic index label with no foreclosure signal, the
    same trap as Substitution Of Trustee. AI_CONTEXT.md rule 7.
    """
    assert "Cancellation/Termination" not in KEEP_DOCTYPES


def test_details_are_fetched_only_for_completed_sales():
    """A detail fetch is a page load. Restricting to TDUS is ~31/month instead
    of ~1300 — the difference between minutes and hours."""
    assert {"Trustees Deed Under Default"} == DETAIL_DOCTYPES
    assert DOCTYPES["NOTS"] not in DETAIL_DOCTYPES


def test_every_detail_type_is_also_kept():
    """Fetching a detail for a type that is never stored would be pure waste."""
    assert DETAIL_DOCTYPES <= KEEP_DOCTYPES


# ── the sweep's keep-filter, as main() applies it ────────────────────────


def _row(doc_number, doc_type):
    return {
        "doc_number": doc_number,
        "doc_type": doc_type,
        "recording_date": "06/02/2026",
        "detail_id": f"DID-{doc_number}",
        "grantor": ["DOE, JOHN"],
        "grantee": ["ACME CAPITAL LLC"],
        "page_no": 1,
    }


def test_keep_filter_reduces_a_realistic_sweep_to_the_in_scope_rows():
    """Proportions taken from the live 2026-06-01..03 sweep."""
    swept = (
        [_row(f"a{i}", "Substitution Of Trustee") for i in range(122)]
        + [_row(f"b{i}", "Reconveyance") for i in range(127)]
        + [_row(f"c{i}", "Deed") for i in range(205)]
        + [_row(f"d{i}", "Trustees Deed Under Default") for i in range(5)]
        + [_row(f"e{i}", "Notice Of Trustees Sale") for i in range(10)]
        + [_row(f"f{i}", "Rescission Of Default") for i in range(5)]
        + [_row(f"g{i}", "Cancellation/Termination") for i in range(16)]
    )
    keep = [r for r in swept if r["doc_type"] in KEEP_DOCTYPES]
    assert len(swept) == 490
    assert len(keep) == 20  # the 16 cancellations are swept and discarded
    assert {r["doc_type"] for r in keep} == KEEP_DOCTYPES


def test_rows_with_an_unrecognised_doc_type_are_discarded_not_stored_as_unknown():
    """A parse failure yielding doc_type None must not slip into the dataset."""
    swept = [_row("x", None), _row("y", "Trustees Deed Under Default")]
    keep = [r for r in swept if r["doc_type"] in KEEP_DOCTYPES]
    assert [r["doc_number"] for r in keep] == ["y"]


# ── totalPages guard ─────────────────────────────────────────────────────


def test_window_over_the_page_limit_is_refused_before_walking(results_html):
    """The POST reports totalPages, so an over-wide window is refused up front.

    Previously this was discovered only on reaching page 40, after 40 requests
    had already been spent on a window whose data would be discarded.
    """
    import collect_sjc

    page = FakePage([results_html, EMPTY])

    def evaluate(_js, args):
        _url, method, _form = args
        if method == "POST":
            return {"status": 200, "text": '{"validationMessages":{},"totalPages":130,"currentPage":1}'}
        raise AssertionError("no page should be fetched when the window is refused")

    page.evaluate = evaluate
    portal = collect_sjc.Portal(FakeCtx(), page, delay=0)
    with pytest.raises(ResultCapExceeded, match="130 pages"):
        portal.search(None, date(2026, 6, 1), date(2026, 6, 30), max_pages=40)


def test_window_within_the_limit_proceeds(results_html):
    import collect_sjc

    responses = [results_html, EMPTY]
    page = FakePage(responses)

    def evaluate(_js, args):
        _url, method, _form = args
        if method == "POST":
            return {"status": 200, "text": '{"validationMessages":{},"totalPages":14,"currentPage":1}'}
        return {"status": 200, "text": responses.pop(0) if responses else EMPTY}

    page.evaluate = evaluate
    portal = collect_sjc.Portal(FakeCtx(), page, delay=0)
    rows = portal.search(None, date(2026, 6, 1), date(2026, 6, 3), max_pages=40)
    assert len(rows) == 3
    assert portal.total_pages == 14


def test_unfiltered_search_sends_an_empty_doctype(results_html):
    """Passing None must post an empty value, not the string 'None'."""
    portal, page = make_portal([results_html, EMPTY])
    portal.search(None, date(2026, 6, 1), date(2026, 6, 3))
    assert page.posted_forms[0]["field_selfservice_documentTypes"] == ""


# ── report ordering and rendering ────────────────────────────────────────


def test_recording_dates_sort_chronologically_not_lexically():
    """MM/DD/YYYY strings sort wrong under ORDER BY.

    Lexically '01/05/2026' precedes '12/31/2025', so a plain SQL sort would head
    the "most recent" list with the oldest rows.
    """
    dates = ["01/05/2026", "12/31/2025", "06/15/2026", "02/01/2026"]
    assert sorted(dates, key=_mmddyyyy_sort_key, reverse=True) == [
        "06/15/2026",
        "02/01/2026",
        "01/05/2026",
        "12/31/2025",
    ]


def test_sort_key_tolerates_a_time_suffix_and_junk():
    assert _mmddyyyy_sort_key("07/15/2026 09:14 AM") == (2026, 7, 15)
    assert _mmddyyyy_sort_key(None) == (0, 0, 0)
    assert _mmddyyyy_sort_key("") == (0, 0, 0)
    assert _mmddyyyy_sort_key("not a date") == (0, 0, 0)


def test_report_renders_on_an_empty_database(db, capsys):
    """An empty DB must produce a readable report, not a traceback."""
    report_sections(db)
    out = capsys.readouterr().out
    assert "SECTION A" in out
    assert "SECTION B" in out
    assert "no notices of trustee's sale collected yet" in out
    assert "no completed sales collected yet" in out


def test_report_lists_notices_and_sales(db, add_tdus, capsys):
    add_tdus("2026-000001", tax_amount=451.00, doc_type=DOCTYPES["TDUS"], grantee="ACME CAPITAL LLC")
    add_tdus("2026-000002", tax_amount=None, doc_type=DOCTYPES["NOTS"], grantor="NGUYEN, LINH")
    # add_tdus records on 07/15/2026, so pin `today` inside the 7-day window
    # rather than letting the wall clock decide whether Section A is empty.
    report_sections(db, today=date(2026, 7, 20))
    out = capsys.readouterr().out
    assert "2026-000002" in out  # the notice, in Section A
    assert "NGUYEN, LINH" in out
    assert "2026-000001" in out  # the sale, in Section B
    assert "$410,000" in out
    assert "ACME CAPITAL LLC" in out


def test_report_states_unavailable_fields_rather_than_omitting_them(db, add_tdus, capsys):
    """§7 requires it. A silently dropped field reads as 'checked and empty',
    which for address and APN would be a false claim about the data."""
    add_tdus("2026-000002", tax_amount=None, doc_type=DOCTYPES["NOTS"])
    report_sections(db)
    out = capsys.readouterr().out
    assert "unavailable" in out.lower()
    assert "Address and APN: UNAVAILABLE" in out
    assert "Auction date: UNAVAILABLE" in out


def test_section_a_does_not_call_the_grantor_list_the_owner(db, add_tdus, capsys):
    """Live June 2026 data showed the grantor list carries the foreclosure
    TRUSTEE alongside the homeowner — doc 2026-055449 pairs the homeowner with
    PRIME RECON LLC — and
    the index does not label roles. Labelling that column "owner" asserts a
    distinction the data does not make (AI_CONTEXT.md rule 8)."""
    add_tdus(
        "2026-000002",
        tax_amount=None,
        doc_type=DOCTYPES["NOTS"],
        grantor="DOE, JOHN | PRIME RECON LLC",  # synthetic homeowner, real trustee firm
    )
    report_sections(db, today=date(2026, 7, 20))
    out = capsys.readouterr().out
    assert "PARTIES ON THE NOTICE" in out
    assert "PROPERTY / OWNER" not in out
    # And the caveat has to explain WHY, or the rename is just cosmetic.
    assert "foreclosure trustee" in out
    assert "does not label who is who" in out


def test_section_a_does_not_promise_the_first_party_is_the_owner(db, add_tdus, capsys):
    """Trustees did appear last in every observed sample, but ordering is not
    documented anywhere and one counter-example would silently mislabel a lead."""
    add_tdus("2026-000002", tax_amount=None, doc_type=DOCTYPES["NOTS"])
    report_sections(db)
    assert "that ordering is not guaranteed" in capsys.readouterr().out


def test_report_carries_the_derivation_caveats(db, add_tdus, capsys):
    add_tdus("2026-000001", tax_amount=451.00)
    report_sections(db)
    out = capsys.readouterr().out
    assert "DERIVED, not recorded" in out
    assert "UNVALIDATED" in out
    assert "finding aid" in out


def test_report_warns_that_rescissions_are_not_yet_joined_out(db, capsys):
    """Until ROADMAP Phase 3 lands, Section A knowingly lists sales that will
    not happen. §3 calls that a correctness requirement, so the output has to
    say so."""
    report_sections(db)
    out = capsys.readouterr().out
    assert "not yet joined" in out
    assert "spoken announcement" in out


# ── sweep reconciliation against totalPages ──────────────────────────────


def _grid(doc_numbers):
    """A minimal results grid: one ss-search-row per document number."""
    return "".join(
        f"""<div class="ss-search-row"><a href="/Web/document/DID-{d}?search=X">View</a>
        <div>{d}</div><div>Notice Of Trustees Sale</div>
        <div>Recording Date</div><div>06/02/2026</div>
        <div>Grantor</div><div>ACME LLC</div></div>"""
        for d in doc_numbers
    )


def _portal_over(pages, total_pages):
    """A real Portal whose searchPost reports `total_pages` and whose
    searchResults serves `pages` (a list of doc-number lists) in order."""
    import collect_sjc

    bodies = [_grid(p) for p in pages]
    page = FakePage([], post_body=f'{{"validationMessages":{{}},"totalPages":{total_pages},"currentPage":1}}')

    def evaluate(_js, args):
        _url, method, form = args
        if method == "POST":
            page.posted_forms.append(form)
            return {"status": 200, "text": page.post_body}
        return {"status": 200, "text": bodies.pop(0) if bodies else EMPTY}

    page.evaluate = evaluate
    return collect_sjc.Portal(FakeCtx(), page, delay=0), page


def test_a_clean_sweep_raises_no_warnings():
    portal, _ = _portal_over([["2026-000001", "2026-000002"], ["2026-000003"]], total_pages=2)
    rows = portal.search(None, date(2026, 6, 1), date(2026, 6, 3))
    assert len(rows) == 3
    assert portal.sweep_warnings == []


def test_a_repeated_document_is_reported():
    """Doc 2026-057938 came back on both page 8 and page 9 of one June window.

    Harmless in itself — append-only absorbs it — but it is evidence the page
    boundary is not stable, which is why the count checks below exist.
    """
    portal, _ = _portal_over([["2026-000001", "2026-057938"], ["2026-057938", "2026-000003"]], total_pages=2)
    portal.search(None, date(2026, 6, 1), date(2026, 6, 3))
    assert any(w.startswith("DUPLICATE_ROWS") for w in portal.sweep_warnings)
    assert "2026-057938" in portal.sweep_warnings[0]


def test_walking_fewer_pages_than_the_server_reported_is_flagged():
    """The failure this exists to catch: a window that quietly came up short.

    Nothing else would record it — the missing rows look exactly like documents
    the county never recorded.
    """
    portal, _ = _portal_over([["2026-000001"], ["2026-000002"]], total_pages=5)
    portal.search(None, date(2026, 6, 1), date(2026, 6, 3))
    assert any(w.startswith("PAGE_COUNT_MISMATCH") for w in portal.sweep_warnings)
    assert "walked 2 page(s), server reported 5" in portal.sweep_warnings[0]


def test_visiting_every_page_but_losing_rows_at_a_boundary_is_flagged():
    """Page count alone would pass here: 3 pages walked, 3 reported.

    But page 1 held 4 rows, so 3 full pages cannot total 8 — a row was dropped
    where two pages meet, which is the skip half of the duplicate defect.
    """
    portal, _ = _portal_over(
        [
            ["2026-000001", "2026-000002", "2026-000003", "2026-000004"],
            ["2026-000005", "2026-000006"],
            ["2026-000007", "2026-000008"],
        ],
        total_pages=3,
    )
    portal.search(None, date(2026, 6, 1), date(2026, 6, 3))
    assert any(w.startswith("ROW_COUNT_SHORT") for w in portal.sweep_warnings)
    assert not any(w.startswith("PAGE_COUNT_MISMATCH") for w in portal.sweep_warnings)


def test_a_short_final_page_is_not_mistaken_for_a_dropped_row():
    """The common, correct case: the last page is partial. Must stay silent, or
    every well-formed window carries a false warning and the real ones stop
    being read."""
    portal, _ = _portal_over(
        [["2026-000001", "2026-000002"], ["2026-000003", "2026-000004"], ["2026-000005"]],
        total_pages=3,
    )
    portal.search(None, date(2026, 6, 1), date(2026, 6, 3))
    assert portal.sweep_warnings == []


def test_reconciliation_is_skipped_when_the_server_reported_no_page_count():
    """Older behaviour: a non-JSON searchPost leaves total_pages unknown. There
    is nothing to compare against, so warn about nothing rather than guess."""
    import collect_sjc

    bodies = [_grid(["2026-000001"])]
    page = FakePage([], post_body="not json")

    def evaluate(_js, args):
        _url, method, _form = args
        if method == "POST":
            return {"status": 200, "text": page.post_body}
        return {"status": 200, "text": bodies.pop(0) if bodies else EMPTY}

    page.evaluate = evaluate
    portal = collect_sjc.Portal(FakeCtx(), page, delay=0)
    portal.search(None, date(2026, 6, 1), date(2026, 6, 3))
    assert portal.total_pages is None
    assert portal.sweep_warnings == []


def test_serving_more_pages_than_reported_is_also_a_mismatch():
    """The over-run direction. The server said 2 pages and kept handing out a
    third — which means totalPages is not a reliable description of the window,
    so the count checks built on it are not to be trusted for it either."""
    portal, _ = _portal_over([["2026-000001"], ["2026-000002"], ["2026-000003"]], total_pages=2)
    portal.search(None, date(2026, 6, 1), date(2026, 6, 3))
    assert any(w.startswith("PAGE_COUNT_MISMATCH") for w in portal.sweep_warnings)


def test_warnings_reset_between_windows():
    """They are per-search state, and main() reads them once per window into
    run_log. A warning leaking forward would attribute a hole to the wrong
    date range — and one that never clears would mark every later window."""
    import collect_sjc

    windows_served = [
        ([_grid(["2026-000001"]), _grid(["2026-000002"])], 9),  # short: 2 of 9 pages
        ([_grid(["2026-000003"])], 1),  # clean
    ]
    page = FakePage([])

    def evaluate(_js, args):
        _url, method, _form = args
        bodies, total = windows_served[0]
        if method == "POST":
            return {"status": 200, "text": f'{{"validationMessages":{{}},"totalPages":{total},"currentPage":1}}'}
        if bodies:
            return {"status": 200, "text": bodies.pop(0)}
        return {"status": 200, "text": EMPTY}

    page.evaluate = evaluate
    portal = collect_sjc.Portal(FakeCtx(), page, delay=0)

    portal.search(None, date(2026, 6, 1), date(2026, 6, 3))
    assert any(w.startswith("PAGE_COUNT_MISMATCH") for w in portal.sweep_warnings)

    windows_served.pop(0)
    portal.search(None, date(2026, 6, 4), date(2026, 6, 6))
    assert portal.sweep_warnings == []


# ── Section A's date window ──────────────────────────────────────────────


def test_section_a_actually_applies_its_day_window(db, add_tdus, capsys):
    """The header said "last 7 days" while the query had no date predicate, so a
    June run printed the claim above notices spanning the whole month."""
    add_tdus("2026-000001", recording_date="07/18/2026", doc_type=DOCTYPES["NOTS"], with_detail=False)
    add_tdus("2026-000002", recording_date="06/02/2026", doc_type=DOCTYPES["NOTS"], with_detail=False)
    report_sections(db, days=7, today=date(2026, 7, 20))
    out = capsys.readouterr().out
    assert "2026-000001" in out
    assert "2026-000002" not in out


def test_section_a_states_the_dates_it_used(db, capsys):
    """A day count is not checkable against the rows; a date range is."""
    report_sections(db, days=7, today=date(2026, 7, 20))
    assert "07/14/2026 to 07/20/2026" in capsys.readouterr().out


def test_section_a_counts_what_the_window_excluded(db, add_tdus, capsys):
    """Silently hiding rows is the same sin as omitting an unavailable field:
    the reader cannot tell a filtered list from a complete one."""
    add_tdus("2026-000001", recording_date="07/18/2026", doc_type=DOCTYPES["NOTS"], with_detail=False)
    add_tdus("2026-000002", recording_date="06/02/2026", doc_type=DOCTYPES["NOTS"], with_detail=False)
    add_tdus("2026-000003", recording_date="06/03/2026", doc_type=DOCTYPES["NOTS"], with_detail=False)
    report_sections(db, days=7, today=date(2026, 7, 20))
    out = capsys.readouterr().out
    assert "2 further notice(s) are in the database" in out
    assert "--report-days 0" in out


def test_section_a_distinguishes_an_empty_window_from_an_empty_database(db, add_tdus, capsys):
    """ "Nothing collected" and "nothing this week" are different facts, and
    reporting the second as the first is absence stored as data."""
    add_tdus("2026-000002", recording_date="06/02/2026", doc_type=DOCTYPES["NOTS"], with_detail=False)
    report_sections(db, days=7, today=date(2026, 7, 20))
    out = capsys.readouterr().out
    assert "no notices recorded in this window" in out
    assert "1 older notice(s) are" in out
    assert "no notices of trustee's sale collected yet" not in out


def test_report_days_zero_shows_everything(db, add_tdus, capsys):
    add_tdus("2026-000002", recording_date="06/02/2026", doc_type=DOCTYPES["NOTS"], with_detail=False)
    report_sections(db, days=0, today=date(2026, 7, 20))
    out = capsys.readouterr().out
    assert "all notices collected" in out
    assert "2026-000002" in out


def test_a_notice_with_no_recording_date_survives_the_window(db, add_tdus, capsys):
    """It cannot be placed in or out of the window, and dropping it would shrink
    the list invisibly. Keep it — it prints as "unavailable", a visible gap."""
    add_tdus("2026-000004", recording_date=None, doc_type=DOCTYPES["NOTS"], with_detail=False)
    report_sections(db, days=7, today=date(2026, 7, 20))
    assert "2026-000004" in capsys.readouterr().out
