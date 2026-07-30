"""Portal.search tests — ROADMAP Gaps 3 and 7.

Portal.search is driven through a fake Playwright page, so the pagination walk,
the result-cap guard, and per-page attribution are all covered with no network
and no CAPTCHA. Only the transport is faked; the code under test is the real
thing.
"""

from datetime import date

import pytest

from collect_sjc import ResultCapExceeded, UnexpectedResponse
from tests.conftest import EMPTY_PAGE as EMPTY
from tests.conftest import FakeCtx, FakePage, make_portal

pytestmark = pytest.mark.unit

START = date(2026, 7, 1)
END = date(2026, 7, 31)


def test_returns_rows_from_a_single_page(results_html):
    portal, _ = make_portal([results_html, EMPTY])
    rows = portal.search("Trustees Deed Under Default", START, END, mode="label")
    assert len(rows) == 3


def test_walks_pages_until_one_comes_back_empty(results_html):
    portal, page = make_portal([results_html, results_html, EMPTY])
    rows = portal.search("Trustees Deed Under Default", START, END, mode="label")
    assert len(rows) == 6
    assert "page=1" in page.requested_urls[0]
    assert "page=2" in page.requested_urls[1]
    assert "page=3" in page.requested_urls[2]


def test_records_the_page_each_row_came_from(results_html):
    """ROADMAP Gap 7: page attribution used to be discarded (`store_index(..., 0, ...)`).

    Without it there is no way to tell a genuinely short final page from a walk
    that was truncated by the 40-page stop.
    """
    portal, _ = make_portal([results_html, results_html, EMPTY])
    rows = portal.search("Trustees Deed Under Default", START, END, mode="label")
    assert {r["page_no"] for r in rows[:3]} == {1}
    assert {r["page_no"] for r in rows[3:]} == {2}


def test_result_cap_raises_the_dedicated_exception(results_html_capped):
    """ROADMAP Gap 3 / §5.1: a cap is an error, never an empty result.

    The dedicated type is what lets main() tell a cap apart from a generic
    transport failure and refuse to fall through to the label-mode retry.
    """
    portal, _ = make_portal([results_html_capped])
    with pytest.raises(ResultCapExceeded):
        portal.search("Trustees Deed Under Default", START, END, mode="label")


def test_result_cap_is_a_runtime_error_subclass(results_html_capped):
    """Existing `except RuntimeError` handlers must still see it — but only
    after the narrower `except ResultCapExceeded` clause has had its chance."""
    assert issubclass(ResultCapExceeded, RuntimeError)


def test_cap_on_a_later_page_still_raises(results_html, results_html_capped):
    """A cap discovered mid-walk must discard the window, not keep page 1.

    Returning the rows gathered so far would be the worst outcome: a partial
    month that looks like a complete one.
    """
    portal, _ = make_portal([results_html, results_html_capped])
    with pytest.raises(ResultCapExceeded):
        portal.search("Trustees Deed Under Default", START, END, mode="label")


def test_cap_message_is_checked_before_parsing(results_html_capped):
    """The cap page carries one parseable row; the guard must win anyway."""
    from collect_sjc import parse_results

    assert len(parse_results(results_html_capped)) == 1
    portal, _ = make_portal([results_html_capped])
    with pytest.raises(ResultCapExceeded):
        portal.search("Trustees Deed Under Default", START, END, mode="label")


def test_disclaimer_page_raises_rather_than_degrading_to_zero_rows(detail_html_disclaimer):
    """A dead session is now named at the point of failure.

    This test previously asserted the opposite — that a disclaimer response
    yields `[]` and leaves the zero-row decision to main(). That was true and it
    was a weakness: parse-to-empty made an unauthenticated session
    indistinguishable from a genuinely empty month, so the diagnosis landed one
    layer away from the cause. Portal.xhr now inspects the response shape, so the
    session problem is reported as a session problem.

    main()'s zero-row abort is still the backstop for a window that really does
    parse to nothing.
    """
    portal, _ = make_portal([detail_html_disclaimer])
    with pytest.raises(UnexpectedResponse, match="not authenticated"):
        portal.search("Trustees Deed Under Default", START, END, mode="label")


def test_sends_the_window_as_us_formatted_dates(results_html):
    portal, page = make_portal([results_html, EMPTY])
    portal.search("Trustees Deed Under Default", START, END, mode="label")
    form = page.posted_forms[0]
    assert form["field_RecDateID_DOT_StartDate"] == "07/01/2026"
    assert form["field_RecDateID_DOT_EndDate"] == "07/31/2026"


def test_id_mode_sends_the_numeric_doctype_id(results_html):
    """`mode="id"` resolves the label through the vocabulary before posting."""
    portal, page = make_portal([results_html, EMPTY])
    vocab = {"Trustees Deed Under Default": "22"}
    portal.search("Trustees Deed Under Default", START, END, mode="id", vocab=vocab)
    assert page.posted_forms[0]["field_selfservice_documentTypes"] == "22"


def test_label_mode_sends_the_label_string(results_html):
    portal, page = make_portal([results_html, EMPTY])
    portal.search("Trustees Deed Under Default", START, END, mode="label")
    assert page.posted_forms[0]["field_selfservice_documentTypes"] == "Trustees Deed Under Default"


def test_never_requests_a_paid_image_path(results_html):
    """Rule 6: index and detail metadata only, never /Web/cart."""
    portal, page = make_portal([results_html, EMPTY])
    portal.search("Trustees Deed Under Default", START, END, mode="label")
    assert not any("/Web/cart" in u for u in page.requested_urls)


def test_app_shell_response_raises_despite_http_200(results_html):
    """A 200 carrying the SPA wrapper is a failure, not data.

    Observed live on 2026-07-29: the searchPost returned the app shell with a 200,
    so server-side search state was never set, and the follow-up searchResults
    answered an unfiltered query — which then tripped the result cap on a month
    that holds ~31 documents. Status-only checking made a total failure look like
    success.
    """
    shell = '<!doctype html>\n<html lang="en"><head><script>var id = "";</script></head>'
    portal, _ = make_portal([shell])
    with pytest.raises(UnexpectedResponse, match="app shell"):
        portal.search("Trustees Deed Under Default", START, END, mode="label")


def test_the_rejected_post_is_what_raises_first(results_html):
    """The POST is validated before any results are fetched.

    Diagnosing the outer symptom (a capped window) instead of the inner cause (a
    no-op POST) sent this investigation the wrong way once already.
    """
    shell = "<!doctype html><html><head></head></html>"
    import collect_sjc

    page = FakePage([results_html, EMPTY])

    def shell_post(_js, args):
        _url, method, _form = args
        if method == "POST":
            return {"status": 200, "text": shell}
        raise AssertionError("results must not be fetched after a rejected POST")

    page.evaluate = shell_post
    portal = collect_sjc.Portal(FakeCtx(), page, delay=0)
    with pytest.raises(UnexpectedResponse):
        portal.search("Trustees Deed Under Default", START, END, mode="label")


def test_disclaimer_response_raises_with_a_session_hint(detail_html_disclaimer):
    """An unauthenticated session is named as such rather than surfacing as
    zero rows, which is a different problem with a different fix."""
    portal, _ = make_portal([detail_html_disclaimer])
    with pytest.raises(UnexpectedResponse, match="not authenticated"):
        portal.xhr("https://host/Web/searchResults/x")


def test_unexpected_response_is_a_runtime_error_subclass():
    assert issubclass(UnexpectedResponse, RuntimeError)


def test_a_fragment_mentioning_html_is_not_misread(results_html):
    """Only the head of the body is inspected.

    A results fragment that happens to contain an escaped `<html` deep in a legal
    description must still parse. Scanning the whole body would reject real data.
    """
    padded = results_html + "<!-- &lt;html&gt; appears in a legal description -->"
    portal, _ = make_portal([padded, EMPTY])
    assert len(portal.search("Trustees Deed Under Default", START, END, mode="label")) == 3


def test_non_200_raises(results_html):
    import collect_sjc

    class Failing(FakePage):
        def evaluate(self, _js, args):
            return {"status": 500, "text": "boom"}

    portal = collect_sjc.Portal(FakeCtx(), Failing([]), delay=0)
    with pytest.raises(RuntimeError, match="HTTP 500"):
        portal.search("Trustees Deed Under Default", START, END, mode="label")
