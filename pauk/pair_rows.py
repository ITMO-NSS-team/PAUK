"""Select the latest original row for each alias-resolved unordered pair."""

from collections.abc import Iterable, Mapping
from datetime import UTC, datetime


def latest_pair_rows(rows: Iterable[dict], aliases: Mapping[str, str] | None = None, *,
                     timestamp_field: str) -> dict[frozenset[str], dict]:
    """Rank pair rows without changing their eligibility, fields or original IDs.

    Callers supply eligible rows and a flattened alias map. Known recency wins,
    then rows already using current IDs, then the stable ``_id``. Missing and
    naive timestamps use UTC; aware timestamps are compared as UTC instants.
    Pairs collapsed by aliases are omitted, and selected rows remain untouched.
    """
    aliases = aliases or {}
    found: dict[frozenset[str], dict] = {}
    ranks: dict[frozenset[str], tuple] = {}
    for row in rows:
        original = frozenset(row["members"])
        members = frozenset(aliases.get(member, member) for member in original)
        if len(members) != 2:
            continue
        timestamp = row.get(timestamp_field)
        if timestamp is None:
            timestamp = datetime.min.replace(tzinfo=UTC)
        elif timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=UTC)
        else:
            timestamp = timestamp.astimezone(UTC)
        rank = (timestamp, original == members, row["_id"])
        if members not in ranks or rank > ranks[members]:
            ranks[members] = rank
            found[members] = row
    return found
