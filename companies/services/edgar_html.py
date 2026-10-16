"""Preprocessing for raw EDGAR filing HTML before it reaches Docling.

SEC filings are inline XBRL: every number is wrapped in `ix:nonFraction`/
`ix:nonNumeric` tags, and section headings are styled `<p>`/`<div>` elements
rather than `<h1>`-`<h6>`. Docling's HTML backend doesn't unwrap `ix:*` (so
table cells come back empty) and has no `<h*>` to detect (so it emits no
section structure and HybridChunker.contextualize() has no breadcrumbs to
prepend). Verified against RKLB's FY2025 10-K: 1770 `ix:*` tags found and
fully unwrapped, 23 "Item N" headings promoted (expected range for a 10-K:
Items 1-16 with letter sub-items -- 1 means this pass didn't run, 40+ means
the table-of-contents guard failed).
"""

import re

from bs4 import BeautifulSoup

# ix:header/hidden/references/resources are XBRL metadata blocks (contexts,
# units, hidden facts) -- not displayed content, drop entirely rather than
# unwrap. Every other ix:* tag (nonFraction, nonNumeric, continuation,
# exclude, ...) wraps text that *is* displayed, so it's unwrapped instead of
# dropped: bs4+lxml lowercases tag names, so these arrive as e.g.
# "ix:nonfraction", matched by the startswith("ix:") check below.
DROP_TAGS = ["ix:header", "ix:hidden", "ix:references", "ix:resources"]

# \s matches the non-breaking spaces EDGAR uses ("Item\xa01A"). Anchored at
# the start and capped at 120 chars so prose cross-references ("as discussed
# in Item 1A...") don't match.
ITEM_RE = re.compile(r"^item\s+\d{1,2}[abc]?\b", re.IGNORECASE)
_MAX_HEADING_LEN = 120


def preprocess_edgar_html(html: str) -> str:
    """Unwrap inline-XBRL fact tags and promote "Item N" markers to <h2>,
    so Docling can extract real table values and real section structure.
    Idempotent -- re-running on already-clean HTML is a no-op."""
    soup = BeautifulSoup(html, "lxml")

    for tag in soup.find_all(DROP_TAGS):
        tag.decompose()

    for tag in soup.find_all(lambda t: t.name is not None and t.name.startswith("ix:")):
        tag.unwrap()

    # Table-of-contents entries are almost always inside a <table>, and
    # would otherwise generate ~20 fake headings alongside the real ones.
    for tag in soup.find_all(["p", "div"]):
        text = tag.get_text(" ", strip=True)
        if len(text) < _MAX_HEADING_LEN and ITEM_RE.match(text) and tag.find_parent("table") is None:
            tag.name = "h2"

    return str(soup)
