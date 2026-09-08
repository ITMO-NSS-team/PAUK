"""Questions the deduplicator could not answer, and the answers people give.

`plan_person_merges` sorts every candidate into merged or held. A held pair
is not a bug: two "A. V. Yulin" with no shared coauthor look identical to
the rules and there is no evidence either way, so the rules refuse and say
why. Until now the refusal went to a JSONL journal nobody reads back, and
the next run refused the same pairs again.

Kept here instead, a refusal becomes a question, and a person's answer
outlives the run that asked. That is the whole point: the answer has to be
consulted *before* the algorithm decides, not patched over the result
afterwards, because a merge cannot be undone.

Two shapes of question, because the rules refuse in two ways:

- a **pair** the rules would not merge, answered "same" or "different";
- a **group** the rules refused whole, because it spans two ORCIDs or two
  addresses. Seven "Andrey Bogdanov" under two addresses are not seven
  people and not one, and the only honest answer here today is "leave them
  apart", which stops the question coming back. Splitting a group properly
  needs its own screen and is not in this module yet.

Storage sits beside the prepared rows rather than in `pauk/graph/`, unlike
`graph_overrides`: the dedup stage writes these long before anything is
published, and `pauk/pipeline/` imports nothing from the graph layer.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from pymongo.database import Database

logger = logging.getLogger(__name__)

COLLECTION = "review_pairs"

PAIR = "person_pair"
GROUP = "person_group"
KINDS = (PAIR, GROUP)

SAME = "same"
DIFFERENT = "different"
VERDICTS = (SAME, DIFFERENT)

#: Where the question came from. The dedup stage runs inside a collection,
#: before anything is published; the graph-wide pass runs over nodes. The
#: same pair can be asked by both, and it is one question either way.
STAGE = "stage"
GRAPH = "graph"


class ReviewError(Exception):
    """A question or an answer that does not describe anything real."""


def _now() -> datetime:
    """Current time at the precision BSON keeps.

    Mongo stores milliseconds, so a plain datetime.now() comes back rounded
    and a document read back differs from the one written.
    """
    moment = datetime.now(UTC)
    return moment.replace(microsecond=moment.microsecond // 1000 * 1000)


def question_id(kind: str, members: list[str]) -> str:
    """Deterministic key, so the same question asked twice is one document.

    Members are sorted, which is what makes (a, b) and (b, a) the same
    question. Without that the queue would fill with mirrored duplicates
    and an answer to one would not settle the other.

    Raises:
        ReviewError: Unknown kind, or fewer than two members.
    """
    if kind not in KINDS:
        raise ReviewError(f"unknown kind: {kind!r} (known: {', '.join(KINDS)})")
    unique = sorted(set(members))
    if len(unique) < 2:
        raise ReviewError("a question needs at least two distinct members")
    return ":".join([kind, *unique])


def members_of(row: dict) -> list[str]:
    """The people one held report row is about, whichever shape it has.

    A pair carries `person_a`/`person_b`, a refused group carries `persons`.
    Both come out of the same report list, so callers should not have to
    know which they are looking at.
    """
    if "persons" in row:
        return sorted(set(row["persons"]))
    return sorted({row["person_a"], row["person_b"]})


def kind_of(row: dict) -> str:
    return GROUP if "persons" in row else PAIR


def record_held(db: Database, report: list[dict], source: str = STAGE) -> int:
    """Store what a run refused to decide, as questions.

    Only rows held back are kept. A merge the rules made needs no question,
    and the audit already records it.

    An answer already given is never touched. The evidence and the time it
    was last seen are refreshed, because a pair the rules keep re-examining
    may have gained a shared coauthor since somebody looked at it, and the
    conflict screen compares the two.

    Args:
        db: Mongo database.
        report: The review journal rows from `plan_person_merges`.
        source: Which pass asked, `STAGE` or `GRAPH`.

    Returns:
        How many questions were written or refreshed.
    """
    moment = _now()
    # One upsert per question rather than a bulk write: a run holds a few
    # hundred pairs at most and takes hours to produce them, so the round
    # trips cost nothing, and the rest of the storage layer writes this way
    # too (see PreparedStore.upsert_models).
    written = 0
    for row in report:
        if row.get("status") != "held":
            continue
        kind = kind_of(row)
        members = members_of(row)
        evidence = {name: value for name, value in row.items()
                    if name not in ("status", "person_a", "person_b", "persons")}
        db[COLLECTION].update_one(
            {"_id": question_id(kind, members)},
            {"$set": {"evidence": evidence, "seen_at": moment, "source": source},
             "$setOnInsert": {"kind": kind, "members": members}},
            upsert=True)
        written += 1
    if written:
        logger.info("review: %d question(s) from the %s pass", written, source)
    return written


def record_verdict(db: Database, kind: str, members: list[str], verdict: str,
                   actor: str = "unknown", note: str = "") -> dict:
    """Write down what a person decided about one question.

    The question need not exist yet. Somebody may answer from the CLI about
    a pair the current data no longer produces, and the answer still has to
    hold when it does.

    Raises:
        ReviewError: Unknown verdict, or "same" on a group. A group is
            refused because its members disagree about an identity field,
            so "these are all one person" would be a decision to ignore
            two different ORCIDs — which the merge itself would refuse.
    """
    if verdict not in VERDICTS:
        raise ReviewError(f"unknown verdict: {verdict!r} (known: {', '.join(VERDICTS)})")
    if kind == GROUP and verdict == SAME:
        raise ReviewError("a refused group cannot be answered 'same'; "
                          "answer the pairs inside it instead")
    key = question_id(kind, members)
    moment = _now()
    db[COLLECTION].update_one(
        {"_id": key},
        {"$set": {"verdict": verdict, "actor": actor, "note": note,
                  "decided_at": moment},
         "$setOnInsert": {"kind": kind, "members": sorted(set(members)),
                          "evidence": {}, "seen_at": moment, "source": STAGE}},
        upsert=True)
    logger.info("review: %s answered %s by %s", key, verdict, actor)
    return db[COLLECTION].find_one({"_id": key})


def withdraw(db: Database, kind: str, members: list[str]) -> bool:
    """Take an answer back, leaving the question in the queue.

    The question itself is kept: it was asked by a real run, and deleting
    it would only mean the next run asks it again from scratch.
    """
    result = db[COLLECTION].update_one(
        {"_id": question_id(kind, members)},
        {"$unset": {"verdict": "", "actor": "", "note": "",
                    "decided_at": "", "applied_at": ""}})
    return result.matched_count > 0


def mark_applied(db: Database, kind: str, members: list[str]) -> bool:
    """Record that a "same" answer reached the graph.

    Separate from the answer because the two happen at different times. A
    pair answered before its group is published has nothing to merge yet,
    and the panel has to be able to say which of the two states it is in.
    """
    result = db[COLLECTION].update_one(
        {"_id": question_id(kind, members), "verdict": SAME},
        {"$set": {"applied_at": _now()}})
    return result.matched_count > 0


def decisions(db: Database, aliases: dict[str, str] | None = None
              ) -> dict[frozenset[str], str]:
    """Every answer given, keyed by the people it is about.

    Args:
        aliases: Merged-away id to the id that survived, when the caller
            knows it (`fetch_merged_id_map` for the graph, `merged_ids` on
            prepared rows). A person folded into another keeps the answers
            made about them under an id that no longer exists, and without
            this they would quietly stop applying.

    Returns:
        Members to verdict. Members are a frozenset, so the caller does not
        have to sort before looking one up.
    """
    aliases = aliases or {}
    found: dict[frozenset[str], str] = {}
    for row in db[COLLECTION].find({"verdict": {"$exists": True}}):
        members = frozenset(aliases.get(member, member) for member in row["members"])
        if len(members) < 2:
            # Both sides ended up the same person, so the question is moot:
            # whatever was decided, they are already one node.
            continue
        found[members] = row["verdict"]
    return found


def questions(db: Database, *, answered: bool | None = None, kind: str = "",
              reason: str = "", limit: int = 50, skip: int = 0) -> list[dict]:
    """One page of the queue, the longest-waiting first.

    Args:
        answered: True for answered questions, False for open ones, None
            for both.
        kind: `PAIR` or `GROUP`, empty for both.
        reason: One of the strings in `held_because`, matched exactly.
    """
    query: dict = {}
    if answered is not None:
        query["verdict"] = {"$exists": answered}
    if kind:
        query["kind"] = kind
    if reason:
        query["evidence.held_because"] = reason
    rows = db[COLLECTION].find(query).sort(
        [("seen_at", 1), ("_id", 1)]).skip(skip).limit(limit)
    return list(rows)


def count(db: Database, *, answered: bool | None = None, kind: str = "",
          reason: str = "") -> int:
    query: dict = {}
    if answered is not None:
        query["verdict"] = {"$exists": answered}
    if kind:
        query["kind"] = kind
    if reason:
        query["evidence.held_because"] = reason
    return db[COLLECTION].count_documents(query)


def reasons(db: Database) -> list[str]:
    """The reasons actually present, for the filter on the page."""
    return sorted(db[COLLECTION].distinct("evidence.held_because"))
