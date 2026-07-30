"""Storage tests — window generation, page attribution, and run_log (Gaps 7, 8)."""

import itertools
from datetime import date

import pytest

from collect_sjc import DOCTYPES, months, run_log_finish, run_log_start, store_index

pytestmark = pytest.mark.unit


# ── months() ─────────────────────────────────────────────────────────────


def test_splits_a_range_into_calendar_months():
    got = list(months(date(2026, 1, 1), date(2026, 3, 31)))
    assert got == [
        (date(2026, 1, 1), date(2026, 1, 31)),
        (date(2026, 2, 1), date(2026, 2, 28)),
        (date(2026, 3, 1), date(2026, 3, 31)),
    ]


def test_clamps_the_first_and_last_window_to_the_requested_range():
    """Monthly windows exist to stay under the result cap (§5.1), but they must
    not silently collect days the caller did not ask for."""
    got = list(months(date(2026, 1, 15), date(2026, 2, 10)))
    assert got == [
        (date(2026, 1, 15), date(2026, 1, 31)),
        (date(2026, 2, 1), date(2026, 2, 10)),
    ]


def test_single_day_range_yields_one_window():
    assert list(months(date(2026, 7, 15), date(2026, 7, 15))) == [(date(2026, 7, 15), date(2026, 7, 15))]


def test_handles_a_leap_february():
    got = list(months(date(2028, 2, 1), date(2028, 2, 29)))
    assert got == [(date(2028, 2, 1), date(2028, 2, 29))]


def test_crosses_a_year_boundary():
    got = list(months(date(2025, 12, 1), date(2026, 1, 31)))
    assert got == [
        (date(2025, 12, 1), date(2025, 12, 31)),
        (date(2026, 1, 1), date(2026, 1, 31)),
    ]


def test_windows_never_overlap_and_leave_no_gap():
    windows = list(months(date(2026, 1, 1), date(2026, 12, 31)))
    for (_, prev_end), (next_start, _) in itertools.pairwise(windows):
        assert (next_start - prev_end).days == 1


# ── store_index page attribution ─────────────────────────────────────────


def _row(doc_number, page_no=None):
    r = {
        "doc_number": doc_number,
        "doc_type": DOCTYPES["TDUS"],
        "recording_date": "07/15/2026",
        "detail_id": f"DID-{doc_number}",
        "grantor": ["DOE, JOHN"],
        "grantee": ["ACME CAPITAL LLC"],
    }
    if page_no is not None:
        r["page_no"] = page_no
    return r


def test_stores_the_page_number_carried_on_the_row(db):
    store_index(db, [_row("2026-000001", page_no=3)], date(2026, 7, 1), date(2026, 7, 31))
    assert db.execute("SELECT page_no FROM index_obs").fetchone()[0] == 3


def test_page_number_is_null_when_absent_rather_than_a_misleading_zero(db):
    """A row parsed outside the pagination walk has no page. NULL says so;
    the old hardcoded 0 claimed a page that does not exist."""
    store_index(db, [_row("2026-000001")], date(2026, 7, 1), date(2026, 7, 31))
    assert db.execute("SELECT page_no FROM index_obs").fetchone()[0] is None


def test_stores_the_query_window_with_each_observation(db):
    store_index(db, [_row("2026-000001", 1)], date(2026, 7, 1), date(2026, 7, 31))
    qs, qe = db.execute("SELECT query_start, query_end FROM index_obs").fetchone()
    assert (qs, qe) == ("2026-07-01", "2026-07-31")


def test_stores_parties_against_the_index_observation(db):
    store_index(db, [_row("2026-000001", 1)], date(2026, 7, 1), date(2026, 7, 31))
    oid = db.execute("SELECT obs_id FROM index_obs").fetchone()[0]
    rows = dict(db.execute("SELECT role, name FROM party_obs WHERE obs_id=?", (oid,)))
    assert rows == {"grantor": "DOE, JOHN", "grantee": "ACME CAPITAL LLC"}


# ── run_log (Gap 8) ──────────────────────────────────────────────────────


def test_run_log_row_exists_before_the_search_runs(db):
    """The point of the table: an interrupted window still leaves a record.

    Written at start, not at finish, so a crash mid-window is distinguishable
    from a window nobody ever attempted.
    """
    run_log_start(db, DOCTYPES["TDUS"], date(2026, 7, 1), date(2026, 7, 31))
    row = db.execute("SELECT started_at, finished_at, rows_indexed FROM run_log").fetchone()
    assert row[0] is not None
    assert row[1] is None
    assert row[2] is None


def test_run_log_finish_records_counts(db):
    rid = run_log_start(db, DOCTYPES["TDUS"], date(2026, 7, 1), date(2026, 7, 31))
    run_log_finish(db, rid, 31, 28, "mode=id")
    row = db.execute("SELECT finished_at, rows_indexed, details_fetched, notes FROM run_log").fetchone()
    assert row[0] is not None
    assert (row[1], row[2], row[3]) == (31, 28, "mode=id")


def test_zero_rows_is_distinguishable_from_never_collected(db):
    """The whole reason the table exists.

    A collected-but-empty window is a row with rows_indexed=0. A never-collected
    window is the absence of a row. Without run_log both look identical from the
    observation tables, and "no foreclosures in March" is indistinguishable from
    "March was never fetched".
    """
    rid = run_log_start(db, DOCTYPES["TDUS"], date(2026, 3, 1), date(2026, 3, 31))
    run_log_finish(db, rid, 0, 0, "ZERO_ROWS: accepted via --allow-zero-rows")

    collected = {(r[0], r[1]) for r in db.execute("SELECT query_start, query_end FROM run_log")}
    assert ("2026-03-01", "2026-03-31") in collected
    assert ("2026-04-01", "2026-04-30") not in collected


def test_capped_window_is_recorded_as_incomplete(db):
    """A cap must leave a note that says the window has a hole.

    report() keys its "<< INCOMPLETE (capped)" flag off this prefix, so a
    reader of the DB months later still learns the window is not trustworthy.
    """
    rid = run_log_start(db, DOCTYPES["TDUS"], date(2026, 7, 1), date(2026, 7, 31))
    run_log_finish(db, rid, 0, 0, "RESULT_CAP: result cap hit on page 1")
    notes = db.execute("SELECT notes FROM run_log").fetchone()[0]
    assert notes.startswith("RESULT_CAP")


def test_multiple_attempts_at_one_window_all_leave_rows(db):
    """Re-collection is expected; run_log is append-per-attempt, like the
    observation tables."""
    for _ in range(3):
        rid = run_log_start(db, DOCTYPES["TDUS"], date(2026, 7, 1), date(2026, 7, 31))
        run_log_finish(db, rid, 31, 31, "mode=id")
    assert db.execute("SELECT COUNT(*) FROM run_log").fetchone()[0] == 3
