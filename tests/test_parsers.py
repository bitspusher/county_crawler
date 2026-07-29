"""Parser tests — MVP.md §6.2, ROADMAP Gap 2.

Read tests/fixtures/README.md before trusting a green run here: the fixtures are
synthetic, so these tests lock in current behaviour and cover edge cases, but
they do not yet prove the parsers handle the county's real markup.
"""

import pytest

from collect_sjc import _lines, parse_detail, parse_results

pytestmark = pytest.mark.unit


# ── parse_results ────────────────────────────────────────────────────────


def test_parses_every_row(results_html):
    rows = parse_results(results_html)
    assert [r["doc_number"] for r in rows] == ["2026-067463", "2026-067502", "2026-067777"]


def test_reads_doc_type_from_the_line_after_the_doc_number(results_html):
    rows = parse_results(results_html)
    assert rows[0]["doc_type"] == "Trustees Deed Under Default"
    assert rows[2]["doc_type"] == "Notice Of Trustees Sale"


def test_extracts_detail_id_from_the_document_href(results_html):
    rows = parse_results(results_html)
    assert rows[0]["detail_id"] == "RG9jSWQtMjAyNi0wNjc0NjM"


def test_missing_detail_link_yields_none_not_an_error(results_html):
    """A row without a /Web/document/ href must parse, with detail_id None.

    main() filters on `r["detail_id"]` before fetching details, so None is the
    contract that keeps such a row in the index without triggering a fetch.
    """
    rows = parse_results(results_html)
    assert rows[2]["detail_id"] is None


def test_reads_recording_date_from_the_labelled_value(results_html):
    rows = parse_results(results_html)
    assert rows[0]["recording_date"] == "07/15/2026"


def test_collects_multiple_grantors_under_one_label(results_html):
    """`Grantor (2)` must collect BOTH names, not just the first."""
    rows = parse_results(results_html)
    assert rows[1]["grantor"] == ["SMITH, MARIA E", "SMITH, ROBERT L"]


def test_separates_grantor_from_grantee(results_html):
    rows = parse_results(results_html)
    assert rows[0]["grantor"] == ["DOE, JOHN A"]
    assert rows[0]["grantee"] == ["ACME CAPITAL LLC"]


def test_view_link_does_not_leak_into_party_names(results_html):
    """The `View` action link terminates party collection.

    Without that terminator "View" lands in the grantee list and then flows
    into v_repeat_buyers as a buyer — the exact-string grouping of §8 has no
    defence against it.
    """
    rows = parse_results(results_html)
    for r in rows:
        assert "View" not in r["grantor"]
        assert "View" not in r["grantee"]


def test_the_cap_page_is_not_silently_parsed_as_data(results_html_capped):
    """The cap message must not be mistaken for a normal short page.

    parse_results is deliberately unaware of the cap — Portal.search checks the
    raw HTML for the message and raises before parsing matters. This test pins
    the fixture's wording so the detection string and the fixture cannot drift
    apart unnoticed.
    """
    assert "more documents than the maximum allowed" in results_html_capped


def test_disclaimer_page_parses_to_zero_rows(detail_html_disclaimer):
    """An unauthenticated session yields no rows — indistinguishable from an
    empty month by parsing alone, which is why the zero-row guard exists."""
    assert parse_results(detail_html_disclaimer) == []


def test_no_rows_from_markup_without_search_rows():
    assert parse_results("<div>nothing here</div>") == []


# ── parse_detail ─────────────────────────────────────────────────────────


def test_reads_the_money_fields(detail_html_tdus):
    d = parse_detail(detail_html_tdus)
    assert d["tax_amount"] == 451.00
    assert d["recording_fee"] == 25.00


def test_keeps_the_raw_tax_string_alongside_the_float(detail_html_tdus):
    """tax_raw is the audit trail for a derived price. Keep both."""
    d = parse_detail(detail_html_tdus)
    assert d["tax_raw"] == "$451.00"


def test_strips_the_time_from_the_recording_date(detail_html_tdus):
    d = parse_detail(detail_html_tdus)
    assert d["recording_date"] == "07/15/2026"


def test_reads_doc_number_and_page_count(detail_html_tdus):
    d = parse_detail(detail_html_tdus)
    assert d["doc_number"] == "2026-067463"
    assert d["num_pages"] == 3


def test_collects_detail_parties(detail_html_tdus):
    d = parse_detail(detail_html_tdus)
    assert d["grantor"] == ["DOE, JOHN A", "DOE, JANE M"]
    assert d["grantee"] == ["ACME CAPITAL LLC"]


def test_legal_description_does_not_leak_into_party_names(detail_html_tdus):
    """`Legal` terminates party collection — a lot/block string is not a person."""
    d = parse_detail(detail_html_tdus)
    assert not any("LOT 14" in n for n in d["grantor"] + d["grantee"])


def test_zero_tax_parses_as_zero_not_as_missing(detail_html_zero_tax):
    """0.0 and None mean different things downstream.

    v_auction_sales maps `tax_amount <= 0` to 'likely_reversion' and NULL to
    'unknown'. Collapsing $0.00 into None would erase the §11926 discriminator
    the whole sale classification rests on (§6.5).
    """
    d = parse_detail(detail_html_zero_tax)
    assert d["tax_amount"] == 0.0
    assert d["tax_amount"] is not None


def test_absent_fields_stay_none(detail_html_disclaimer):
    d = parse_detail(detail_html_disclaimer)
    assert d["tax_amount"] is None
    assert d["doc_number"] is None


def test_money_with_thousands_separators():
    html = """<div><span>Tax Amount</span><span>$1,237.50</span></div>"""
    assert parse_detail(html)["tax_amount"] == 1237.50


# ── _lines ───────────────────────────────────────────────────────────────


def test_lines_drops_script_and_style_content():
    html = "<script>var x = 'Grantor';</script><style>.a{}</style><p>Real</p>"
    assert _lines(html) == ["Real"]


def test_lines_decodes_entities_and_collapses_whitespace():
    assert _lines("<p>A&nbsp;&amp;   B</p>") == ["A & B"]


def test_lines_strips_bullet_decoration():
    assert _lines("<li>&bull; DOE, JOHN</li>") == ["DOE, JOHN"]
