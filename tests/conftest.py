import sqlite3
import sys
from pathlib import Path

import pytest

# collect_sjc.py lives at the repo root, not in a package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"

# ── Fake Playwright transport ────────────────────────────────────────────
# Shared here rather than in one test module, so several modules can drive the
# real Portal without importing each other (tests/ is not a package, so a
# cross-module import fails to resolve).

EMPTY_PAGE = "<div>no more rows</div>"


class FakePage:
    """Stands in for a Playwright page, serving canned bodies per XHR.

    `responses` is consumed in order for the searchResults GETs. The searchPost
    POST is answered with `post_body` and recorded separately, so a test can
    assert on what the collector actually sent.
    """

    def __init__(self, responses, post_body=""):
        import collect_sjc

        self.url = f"https://host/Web/search/{collect_sjc.SEARCH_ID}"
        self._responses = list(responses)
        self.post_body = post_body
        self.posted_forms = []
        self.requested_urls = []

    def evaluate(self, _js, args):
        url, method, form = args
        if method == "POST":
            self.posted_forms.append(form)
            return {"status": 200, "text": self.post_body}
        self.requested_urls.append(url)
        if not self._responses:
            return {"status": 200, "text": EMPTY_PAGE}
        return {"status": 200, "text": self._responses.pop(0)}

    def goto(self, *_a, **_kw):  # pragma: no cover - url already matches
        raise AssertionError("should not navigate: already on the search page")

    def wait_for_timeout(self, _ms):  # pragma: no cover
        pass


class FakeCtx:
    def __init__(self):
        self.request = None


def make_portal(responses, post_body=""):
    """A real Portal over a fake transport.

    delay=0 keeps the suite fast. Never do this against the live portal — §10
    requires a real sleep between real requests.
    """
    import collect_sjc

    page = FakePage(responses, post_body=post_body)
    return collect_sjc.Portal(FakeCtx(), page, delay=0), page


@pytest.fixture
def portal_factory():
    """Fixture form of make_portal, for tests that prefer injection."""
    return make_portal


def _load_fixture(name):
    """Read a committed markup fixture, failing LOUDLY when it is absent.

    These files are committed under tests/fixtures/, so a missing one means a
    broken checkout or the wrong working directory — not a normal state. A
    `pytest.mark.skipif(not path.exists())` guard would report green while
    silently covering nothing, which is exactly the failure mode AI_CONTEXT.md
    rule 9 exists to prevent.
    """
    path = FIXTURE_DIR / name
    if not path.exists():
        pytest.fail(
            f"Markup fixture missing: {path}. It is committed to the repo, so its "
            "absence is a broken checkout or a wrong working directory — failing "
            "loudly instead of skipping silently."
        )
    return path.read_text(encoding="utf-8")


@pytest.fixture
def results_html():
    """A NOTS/TDUS search-results grid fragment."""
    return _load_fixture("search_results.html")


@pytest.fixture
def results_html_capped():
    """A results page carrying the server's result-cap message."""
    return _load_fixture("search_results_capped.html")


@pytest.fixture
def detail_html_tdus():
    """A Trustees Deed Under Default detail view carrying a real tax amount."""
    return _load_fixture("detail_tdus.html")


@pytest.fixture
def detail_html_zero_tax():
    """A TDUS detail view with $0.00 tax — the §11926 credit-bid signal."""
    return _load_fixture("detail_tdus_zero_tax.html")


@pytest.fixture
def detail_html_disclaimer():
    """The disclaimer/CAPTCHA gate the portal serves to an unauthenticated session."""
    return _load_fixture("disclaimer.html")


@pytest.fixture
def db():
    """An in-memory DB with the real SCHEMA and VIEWS applied.

    The derivation views are the thing most worth testing: they encode two
    unvalidated assumptions (the DTT rate and the §11926 split) and, per
    AI_CONTEXT.md rule 1, they are the only place those assumptions may live.
    """
    import collect_sjc

    con = sqlite3.connect(":memory:")
    con.executescript(collect_sjc.SCHEMA)
    con.executescript(collect_sjc.VIEWS)
    yield con
    con.close()


@pytest.fixture
def add_tdus(db):
    """Insert one TDUS document (index + parties + optional detail).

    Returns the obs_id of the index observation so tests can add further
    observations of the same document and assert the append-only latest-wins
    semantics of v_latest_index / v_latest_detail.
    """
    import collect_sjc

    def _add(
        doc_number,
        recording_date="07/15/2026",
        tax_amount=None,
        grantee="ACME CAPITAL LLC",
        grantor="DOE, JOHN",
        observed_at="2026-07-20T10:00:00",
        doc_type=None,
        page_no=1,
        detail_observed_at=None,
        fetch_ok=1,
        with_detail=True,
    ):
        doc_type = doc_type or collect_sjc.DOCTYPES["TDUS"]
        cur = db.execute(
            "INSERT INTO index_obs(observed_at,doc_number,doc_type,recording_date,"
            "detail_id,page_no,query_start,query_end) VALUES(?,?,?,?,?,?,?,?)",
            (
                observed_at,
                doc_number,
                doc_type,
                recording_date,
                f"DID-{doc_number}",
                page_no,
                "2026-07-01",
                "2026-07-31",
            ),
        )
        oid = cur.lastrowid
        for role, name in (("grantee", grantee), ("grantor", grantor)):
            if name:
                db.execute(
                    "INSERT INTO party_obs(obs_id,obs_source,doc_number,role,name) VALUES(?,?,?,?,?)",
                    (oid, "index", doc_number, role, name),
                )
        if with_detail:
            db.execute(
                "INSERT INTO detail_obs(observed_at,doc_number,detail_id,recording_date,"
                "num_pages,recording_fee,tax_amount,tax_raw,fetch_ok)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    detail_observed_at or observed_at,
                    doc_number,
                    f"DID-{doc_number}",
                    recording_date,
                    3,
                    25.0,
                    tax_amount,
                    None if tax_amount is None else f"${tax_amount:,.2f}",
                    fetch_ok,
                ),
            )
        db.commit()
        return oid

    return _add
