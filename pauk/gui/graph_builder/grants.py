"""Grant numbers from `Publication.funding` -> one key per grant, so the site
can list every publication of a grant. The raw numbers come from OpenAlex as
the authors typed them: `Project 075-15-2019-1896`, `075-15-2019-1896?`,
unicode dashes, and numbers cut short (`18-19-`, `075-15`) that would
otherwise glue unrelated papers together.
"""

from __future__ import annotations

import re
from collections import defaultdict

_PREFIX = re.compile(r"^(?:grant|project|contract|agreement|no\.?|n°|№|#)\s*", re.IGNORECASE)
_DASHES = re.compile(r"[‐-―−]")
_JUNK = re.compile(r"[^0-9A-Za-z/.\-]")
MIN_KEY_LEN = 5


def normalize_grant_id(raw: str) -> str | None:
    """Canonical form of one grant number, `None` if it is too broken to group by.

    Example:
        >>> normalize_grant_id("Project 075-15-2019-1896?"), normalize_grant_id("Priority 2030")
        ('075-15-2019-1896', 'PRIORITY2030')
        >>> normalize_grant_id("18-19-") is None  # cut short
        True
    """
    text = _strip_words(raw)
    if text.endswith("-"):
        return None
    key = _JUNK.sub("", text).strip("-./").upper()
    return key if len(key) >= MIN_KEY_LEN else None


def _strip_words(raw: str) -> str:
    text = _DASHES.sub("-", raw.strip())
    while (stripped := _PREFIX.sub("", text)) != text:
        text = stripped.strip()
    return text


def add_grant_keys(pubs_detail: list[dict]) -> None:
    """Adds `grant_key` to every `funding` entry of `pubs_detail` and drops
    redundant entries, in place.

    A key that is a dash-prefix of longer keys is a number cut short: it joins
    the longest one when there is exactly one, and is dropped (`None`) when
    there are several - `075-15` prefixes hundreds of ministry contracts.

    OpenAlex often lists one grant several times per paper, sometimes next to
    a fragment of it (`075-15-` beside `075-15-2021-1349`) or its funder with
    no number. Within a publication those repeats are dropped, see `_dedupe`.

    Args:
        pubs_detail: `pubs-detail.json` rows with `funding: [{funder, grant_id}]`.
    """
    keys = {
        key
        for pub in pubs_detail
        for entry in pub["funding"]
        if isinstance(entry, dict) and (key := normalize_grant_id(entry.get("grant_id") or ""))
    }
    extensions: dict[str, set[str]] = defaultdict(set)
    for key in keys:
        parts = key.split("-")
        for i in range(1, len(parts)):
            prefix = "-".join(parts[:i])
            if prefix in keys:
                extensions[prefix].add(key)
    # Only the longest extensions count: in A < B < C, A still resolves to C.
    resolved = {}
    for prefix, longer in extensions.items():
        leaves = {k for k in longer if not any(o != k and o.startswith(k + "-") for o in longer)}
        resolved[prefix] = next(iter(leaves)) if len(leaves) == 1 else None

    for pub in pubs_detail:
        for entry in pub["funding"]:
            if isinstance(entry, dict):
                key = normalize_grant_id(entry.get("grant_id") or "")
                entry["grant_key"] = resolved.get(key, key) if key else None
        pub["funding"] = _dedupe(pub["funding"])


def _dedupe(funding: list) -> list:
    """One entry per grant, no fragment of a listed grant, no bare funder that
    is already listed with a number."""
    keyed = [e for e in funding if isinstance(e, dict) and e.get("grant_key")]
    keys = {e["grant_key"] for e in keyed}
    numbered_funders = {e.get("funder") for e in keyed}
    kept: list = []
    seen: set[str] = set()
    for entry in funding:
        if not isinstance(entry, dict):
            kept.append(entry)
            continue
        key = entry.get("grant_key")
        if key:
            if key not in seen:
                seen.add(key)
                kept.append(entry)
            continue
        fragment = _JUNK.sub("", _strip_words(entry.get("grant_id") or "")).strip("-./").upper()
        if fragment and any(k.startswith(fragment) for k in keys):
            continue
        if not fragment and entry.get("funder") in numbered_funders:
            continue
        kept.append(entry)
    return kept
