# Markup fixtures — provenance

**Read this before trusting a green parser test.**

| Fixture | Provenance |
|---|---|
| `search_results.html` | **SYNTHETIC** — hand-authored |
| `search_results_capped.html` | **SYNTHETIC** — hand-authored |
| `detail_tdus.html` | **SYNTHETIC** — hand-authored |
| `detail_tdus_zero_tax.html` | **SYNTHETIC** — hand-authored |
| `disclaimer.html` | **SYNTHETIC** — hand-authored |

Every fixture in this directory is currently synthetic. None was captured from
the live Tyler Eagle portal, because the portal sits behind a reCAPTCHA that
must be cleared by a human (MVP.md §5.1, §10) and no collection run has
completed yet.

## What that means for the tests

MVP.md §6.2 says both parsers are unit-tested **against captured fixtures**.
They are not yet. The distinction matters:

- These tests **do** lock in current parser behaviour, so a refactor that
  changes it fails loudly. That is real regression value.
- These tests **do** cover edge cases the parsers must survive — multi-party
  rows, missing tax amounts, `$0.00` tax, absent detail links, the cap message,
  the disclaimer gate.
- These tests **do not** prove the parsers handle the county's real markup.
  The fixtures were written by reading the parsers, so agreement between them is
  partly circular. A field the parsers were never taught to find is a field
  these fixtures do not contain.

Until the fixtures are real, a passing suite means "the parsers still do what
they did yesterday", not "the parsers work".

## Replacing them with real captures

Run this once, with a human present to clear the CAPTCHA:

```sh
python scripts/capture_fixtures.py --headed
```

It overwrites the files above with live markup and rewrites the provenance table
in this README. Expect parser tests to fail on the first real capture — that
failure is the point, and it is the first genuine signal this project has had
about whether `parse_results` and `parse_detail` actually work.

Redact nothing that is already public record; the recorder index is a public
finding aid. Do keep captures small — one page of results, two detail views.
