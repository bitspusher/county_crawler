"""Tests for the unfiltered-sweep design and the §7 report sections.

Measured live on 2026-07-29: the portal discards the doc-type filter, so a search
returns every type recorded in the window. Selection moved client-side, which
makes KEEP_DOCTYPES and the day-based windows load-bearing — they are the only
things standing between the dataset and 1016 Substitution Of Trustee records a
month.
"""

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
    for (_, prev_end), (next_start, _) in zip(got, got[1:], strict=False):
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
        "Cancellation/Termination",
    } == KEEP_DOCTYPES


def test_substitution_of_trustee_is_never_kept():
    """AI_CONTEXT.md rule 7. At 1016/mo it would swamp everything else.

    The sweep unavoidably RETRIEVES it — the server sends the whole window — so
    this set is the only thing that stops it being stored.
    """
    assert "Substitution Of Trustee" not in KEEP_DOCTYPES


@pytest.mark.parametrize(
    "noise",
    ["Reconveyance", "Deed", "Deed Of Trust", "Fictitious Business Name", "Substitution Of Trustee", "Lien"],
)
def test_high_volume_noise_types_are_discarded(noise):
    """Every one of these appeared in the live 3-day sweep, several in the hundreds."""
    assert noise not in KEEP_DOCTYPES


def test_the_two_mvp_types_are_kept():
    assert DOCTYPES["NOTS"] in KEEP_DOCTYPES
    assert DOCTYPES["TDUS"] in KEEP_DOCTYPES


def test_phase_3_types_arrive_with_the_sweep():
    """Rescissions and cancellations are what make a NOTS list honest (§3).

    They cost nothing extra now: the unfiltered sweep already returns them, so
    ROADMAP Phase 3 needs only the exclusion join, not new collection.
    """
    assert "Rescission Of Default" in KEEP_DOCTYPES
    assert "Cancellation/Termination" in KEEP_DOCTYPES


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
    assert len(keep) == 36
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
    report_sections(db)
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
