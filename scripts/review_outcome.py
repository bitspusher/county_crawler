#!/usr/bin/env python3
"""Classify an architect post-review output into MERGE / REJECT / INFRA.

The three-way split matters. MERGE and REJECT are real verdicts about a diff.
INFRA means the reviewer never got to form one — quota, auth, an empty response —
and must NOT be recorded as a rejection, or the circuit breaker eventually blocks
a perfectly good spec because the API was down three nights running.

Defaulting to INFRA when no verdict is found is deliberate: silence is evidence
of a broken review, not of a bad diff, and INFRA preserves the branch for a retry
while REJECT deletes it.

Usage: review_outcome.py < review.txt   # prints one of: MERGE REJECT INFRA
Also importable: classify(text) -> str
"""

import re
import sys

INFRA_PATTERN = (
    r"session limit|rate limit|credit balance|budget|quota|"
    r"authentication_error|overloaded_error|invalid api key|"
    r"connection ?refused|unable to connect|"
    r"agent returned empty output"
)


def classify(text: str) -> str:
    """Classify review text into MERGE, REJECT, or INFRA."""
    # The verdict marker is case-sensitive on purpose: the agent is instructed to
    # emit `VERDICT: MERGE` on its own line, and prose that merely discusses
    # "the verdict" should not be mistaken for one.
    match = re.search(r"VERDICT:\s*(\w+)", text)
    if match:
        value = match.group(1).upper()
        if value == "MERGE":
            return "MERGE"
        if value == "REJECT":
            return "REJECT"

    if re.search(INFRA_PATTERN, text, re.IGNORECASE):
        return "INFRA"

    # No parseable verdict and no recognisable infra signature. Still INFRA: an
    # unparseable review is a broken review.
    return "INFRA"


def main() -> None:
    print(classify(sys.stdin.read()))
    sys.exit(0)


if __name__ == "__main__":
    main()
