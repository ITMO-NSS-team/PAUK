"""Questions the pipeline could not answer, and the answers people give.

Three stages reach a point where the evidence runs out. Refusing there is
not a bug: two "A. V. Yulin" with no shared coauthor look identical to the
rules, and there is nothing to decide on. Until now every refusal went to a
JSONL journal nobody read back, and the next run refused the same thing
again.

Kept here instead, a refusal becomes a question, and a person's answer
outlives the run that asked. That is the whole point: the answer is
consulted *before* the algorithm decides rather than patched over its
result. A fold can be taken apart afterwards (`pauk.graph.unmerge`), but
only by rebuilding the record from the prepared row it was published from,
and only for as long as that row is there.

Four shapes of question, because the three producers refuse in four ways:

- a **pair** the person merge rules would not fold, answered "same" or
  "different";
- a **group** those rules refused whole, because it spans two ORCIDs or two
  addresses. Seven "Andrey Bogdanov" under two addresses are not seven
  people and not one, so the group cannot be answered as a whole. It is
  answered by naming which of its records are one person, and `record_split`
  turns that into the pair answers the rules read;
- a **github account** the matcher could not place, answered the same two
  ways: this is them, or it is not;
- a **catalog record**, where the staff directory holds several people under
  one name. Not a yes or no — both namesakes are plausible and exactly one
  is right — so `record_choice` names the record instead of taking a side.

Storage sits beside the prepared rows rather than in `pauk/graph/`, unlike
`graph_overrides`: these are written long before anything is published, and
`pauk/pipeline/` imports nothing from the graph layer.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from itertools import combinations

from pymongo.database import Database

logger = logging.getLogger(__name__)

COLLECTION = "review_pairs"

PAIR = "person_pair"
GROUP = "person_group"
#: An account against the author it may belong to. A pair like the others,
#: only its two halves are different things: a GitHub login and a person.
GITHUB = "github_person"
#: A person against the catalog records their name cannot be told apart
#: from. Answered by choosing one, or by saying none of them fits.
STAFF = "staff_record"
KINDS = (PAIR, GROUP, GITHUB, STAFF)

#: What the person merge rules read. GITHUB and STAFF pair a person with an
#: account or a directory row, which is not two records of one researcher.
PERSON_KINDS = (PAIR, GROUP)

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
    # The key joins on ":" and the form that answers it on ",". Nothing that
    # lands here carries either today, but a LinkCandidate id turned out to
    # be a URL once already: fail loudly rather than collide quietly.
    bad = [member for member in unique if ":" in member or "," in member]
    if bad:
        raise ReviewError(f"an id cannot contain ':' or ',': {', '.join(bad)}")
    return ":".join([kind, *unique])


def members_of(row: dict) -> list[str]:
    """What one held report row is about, whichever shape it has.

    Not always people: a catalog question pairs a person with directory
    rows, and a github question with an account. Callers should not have to
    know which shape they are holding, so the four are read in one place —
    together with `kind_of` and `names_of` just below, which split on the
    same fields.
    """
    if "records" in row:
        return sorted({row["person"], *row["records"]})
    if "login" in row:
        return sorted({row["login"], row["person"]})
    if "persons" in row:
        return sorted(set(row["persons"]))
    return sorted({row["person_a"], row["person_b"]})


def kind_of(row: dict) -> str:
    """Which question a report row is, read off the fields it carries.

    Each producer names its subjects its own way: the catalog check a
    `person` and `records`, the github matcher a `login` and a `person`, a
    refused group a list of `persons`, a held pair a `person_a` and a
    `person_b`.
    """
    if "records" in row:
        return STAFF
    if "login" in row:
        return GITHUB
    if "persons" in row:
        return GROUP
    return PAIR


def names_of(row: dict, members: list[str]) -> list[str | None]:
    """The names, in the order `members` are in.

    A pair arrives as person_a/name_a and person_b/name_b, in whatever
    order the blocking happened to emit it, while members are sorted. Lined
    up here rather than in the page: the page has only the members and the
    names, and pairing the wrong name with the wrong id is not a mistake a
    reader can spot.
    """
    if "records" in row:
        by_id = {row["person"]: row.get("name_raw"),
                 **dict(zip(row["records"], row.get("record_names") or [], strict=False))}
    elif "login" in row:
        # The account is named by its login; there is nothing else to call it.
        by_id = {row["login"]: row["login"], row["person"]: row.get("name_raw")}
    elif "persons" in row:
        by_id = dict(zip(row["persons"], row.get("names") or [], strict=False))
    else:
        by_id = {row["person_a"]: row.get("name_a"), row["person_b"]: row.get("name_b")}
    return [by_id.get(member) for member in members]


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
        report: Rows any of the producers held back — `plan_person_merges`,
            the github matcher, or the catalog check. See `kind_of` for the
            shapes.
        source: Which pass asked, `STAGE` or `GRAPH`.

    Returns:
        How many questions were written or refreshed.
    """
    moment = _now()
    # One upsert per question, like PreparedStore.upsert_models: a few
    # hundred rows once per run, so a bulk write would buy nothing.
    written = 0
    for row in report:
        if row.get("status") != "held":
            continue
        kind = kind_of(row)
        members = members_of(row)
        evidence = {name: value for name, value in row.items()
                    if name not in ("status", "person_a", "person_b", "persons",
                                    "name_a", "name_b")}
        evidence["names"] = names_of(row, members)
        db[COLLECTION].update_one(
            {"_id": question_id(kind, members)},
            {"$set": {"evidence": evidence, "seen_at": moment, "source": source},
             "$setOnInsert": {"kind": kind, "members": members}},
            upsert=True)
        written += 1
    if written:
        logger.info("review: %d question(s) from the %s pass", written, source)
    return written


def record_disputed(db: Database, report: list[dict]) -> int:
    """Note where the rules have changed their mind about a settled pair.

    Somebody said two records are two people, or that an account is not
    theirs; the evidence has moved since, and a rule that had nothing to
    stand on now fires. The answer stays in force — that is the point of
    storing it — but the disagreement is worth a person's eye, exactly like
    a source that starts contradicting a hand edit (see
    `pauk.graph.overrides`).

    Returns:
        How many disagreements were noted.
    """
    moment = _now()
    noted = 0
    for row in report:
        if row.get("status") != "disputed":
            continue
        members = members_of(row)
        result = db[COLLECTION].update_one(
            {"_id": question_id(kind_of(row), members)},
            {"$set": {"disputed_at": moment, "disputed_rule": row.get("rule")}})
        noted += result.matched_count
    if noted:
        logger.warning("review: %d answered pair(s) the rules would now merge", noted)
    return noted


def record_verdict(db: Database, kind: str, members: list[str], verdict: str,
                   actor: str = "unknown", note: str = "",
                   names: list[str | None] | None = None) -> dict:
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
    if kind == STAFF and verdict == SAME:
        # "Yes" to a question that asks which of two namesakes this is
        # names nobody. Recorded, it would leave the queue looking answered
        # and change nothing at all.
        raise ReviewError("a catalog question is answered by choosing a record; "
                          "see record_choice")
    key = question_id(kind, members)
    moment = _now()
    db[COLLECTION].update_one(
        {"_id": key},
        {"$set": {"verdict": verdict, "actor": actor, "note": note,
                  "decided_at": moment},
         # Answering settles what a skip only postponed, and answers the
         # disagreement the rules raised, whichever way it is answered.
         "$unset": {"skipped_at": "", "skipped_by": "",
                    "disputed_at": "", "disputed_rule": ""},
         "$setOnInsert": {"kind": kind, "members": sorted(set(members)),
                          # Names when the caller has them: a pair answered
                          # before any run held it has no evidence of its
                          # own, and a row of bare ids asks nothing.
                          "evidence": {"names": names} if names else {},
                          "seen_at": moment, "source": STAGE}},
        upsert=True)
    logger.info("review: %s answered %s by %s", key, verdict, actor)
    return db[COLLECTION].find_one({"_id": key})


def skip(db: Database, kind: str, members: list[str], actor: str = "unknown") -> bool:
    """Mark a question as looked at and not settled.

    Not a verdict, so `decisions` never returns it and the rules never see
    it. It only separates "nobody has read this" from "somebody read it and
    could not tell", which is the difference between a queue that can be
    worked through and one that cannot.
    """
    result = db[COLLECTION].update_one(
        {"_id": question_id(kind, members)},
        {"$set": {"skipped_at": _now(), "skipped_by": actor}})
    return result.matched_count > 0


def record_split(db: Database, members: list[str], same: list[str],
                 actor: str = "unknown", note: str = "") -> int:
    """Resolve a refused group by naming which of its records are one person.

    A group is refused because its members disagree about an identity field,
    so it describes more than one person and cannot be answered as a whole.
    What it can be answered with is a split: these of you are one person,
    the rest are somebody else.

    Written as ordinary pair answers, because that is what the rules read.
    Saying only "these two are one person" is not enough on its own — the
    rules would rebuild the same group through the members left over, and
    refuse it again for the same reason. So the pairs across the split are
    recorded as "different" too, and the group's own question is answered
    "different", which is now plainly true of it.

    Args:
        members: Everyone the group holds.
        same: The subset that is one person.

    Returns:
        How many pair answers were written.

    Raises:
        ReviewError: The subset is not part of the group, is smaller than a
            pair, or is the whole group — which is the answer the conflict
            rules out.
    """
    members = sorted(set(members))
    same = sorted(set(same))
    if not set(same) <= set(members):
        raise ReviewError("those records are not in this group")
    if len(same) < 2:
        raise ReviewError("name at least two records as one person")
    if len(same) == len(members):
        raise ReviewError("the group was refused precisely because all of it "
                          "cannot be one person")
    rest = [member for member in members if member not in same]
    # The group knows what its members are called; the pairs it writes would
    # otherwise be rows of bare ids.
    asked = db[COLLECTION].find_one({"_id": question_id(GROUP, members)}) or {}
    known = dict(zip(members, (asked.get("evidence") or {}).get("names") or [],
                     strict=False))

    def named(*people: str) -> list[str | None]:
        return [known.get(person) for person in people]

    # Inside the subset first, so a write that stops halfway merges nothing:
    # the rules rebuild the whole group and refuse it again.
    written = 0
    for first, second in combinations(same, 2):
        record_verdict(db, PAIR, [first, second], SAME, actor=actor, note=note,
                       names=named(first, second))
        written += 1
    for first in same:
        for second in rest:
            record_verdict(db, PAIR, [first, second], DIFFERENT, actor=actor,
                           note=note, names=named(first, second))
            written += 1
    record_verdict(db, GROUP, members, DIFFERENT, actor=actor, note=note)
    logger.info("review: group %s split by %s, %d pair(s) written",
                members, actor, written)
    return written


def withdraw(db: Database, kind: str, members: list[str]) -> bool:
    """Take an answer back, leaving the question in the queue.

    The question itself is kept: it was asked by a real run, and deleting it
    would only mean the next run asks it again from scratch.

    Unless no run ever asked it. A split writes answers about pairs the
    rules never held, and withdrawing one left a row in the queue with no
    reason and nothing to decide on. A held question always says why it was
    held; that is what tells the two apart.
    """
    key = question_id(kind, members)
    asked = db[COLLECTION].find_one({"_id": key})
    if asked is not None and not (asked.get("evidence") or {}).get("held_because"):
        return db[COLLECTION].delete_one({"_id": key}).deleted_count > 0
    result = db[COLLECTION].update_one(
        {"_id": key},
        {"$unset": {"verdict": "", "actor": "", "note": "",
                    "decided_at": "", "applied_at": "",
                    # The catalog record chosen goes with the answer that
                    # chose it. Left behind, it kept being applied by every
                    # later run while the panel showed the question as open.
                    "chosen": "",
                    # Nothing left to disagree with once the answer is gone.
                    "disputed_at": "", "disputed_rule": ""}})
    return result.matched_count > 0


def applied(db: Database, kind: str, members: list[str]) -> bool:
    """Whether this answer has already folded two records into one."""
    asked = db[COLLECTION].find_one({"_id": question_id(kind, members)})
    return bool(asked and asked.get("applied_at"))


def record_undo(db: Database, kind: str, members: list[str],
                actor: str = "unknown", note: str = "") -> dict | None:
    """Turn an applied "same" into a "different" after the fold was taken apart.

    Both halves of the undo are needed and neither is enough alone. Clearing
    `applied_at` says the graph no longer holds one node; the verdict has to
    flip because "same" is exactly what would fold the pair again on the
    next run — the answer would undo the undo.

    Raises:
        ReviewError: The question was never folded, so there is nothing to
            record the undoing of.
    """
    key = question_id(kind, members)
    asked = db[COLLECTION].find_one({"_id": key})
    if asked is None or not asked.get("applied_at"):
        raise ReviewError(f"{key} was not folded; there is nothing to take apart")
    record_verdict(db, kind, members, DIFFERENT, actor=actor, note=note)
    db[COLLECTION].update_one({"_id": key}, {"$unset": {"applied_at": ""}})
    logger.info("review: %s taken apart by %s", key, actor)
    return db[COLLECTION].find_one({"_id": key})


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


def record_choice(db: Database, person: str, records: list[str], chosen: str | None,
                  actor: str = "unknown", note: str = "") -> dict:
    """Say which catalog record a person is, or that none of them is.

    Not a verdict like the others, because the question is not yes or no:
    two namesakes are both plausible and exactly one is right. The choice
    rides along with the verdict — "same" plus the record chosen, or
    "different" when the catalog does not hold this person at all.

    Raises:
        ReviewError: The chosen record is not one of the ones asked about.
    """
    if chosen is not None and chosen not in records:
        raise ReviewError("that record is not one of the ones asked about")
    members = [person, *records]
    key = question_id(STAFF, members)
    moment = _now()
    db[COLLECTION].update_one(
        {"_id": key},
        # `person` on the document, not in the evidence: an answer given
        # before the question exists has no evidence to read it from.
        {"$set": {"verdict": SAME if chosen else DIFFERENT, "chosen": chosen,
                  "person": person, "actor": actor, "note": note,
                  "decided_at": moment},
         "$unset": {"skipped_at": "", "skipped_by": "",
                    "disputed_at": "", "disputed_rule": ""},
         "$setOnInsert": {"kind": STAFF, "members": sorted(set(members)),
                          "evidence": {}, "seen_at": moment, "source": STAGE}},
        upsert=True)
    logger.info("review: %s is %s, said %s", person, chosen or "nobody in the catalog", actor)
    return db[COLLECTION].find_one({"_id": key})


def staff_choices(db: Database, aliases: dict[str, str] | None = None) -> dict[str, str]:
    """The catalog record each person was said to be, where somebody said.

    Only the answers that name a record: "none of them" resolves the
    question but gives the merge rules nothing to fold on.

    Args:
        aliases: Merged-away id to the id that survived, as for `decisions`.
            A person folded into another keeps the record chosen for them
            under an id nothing carries, and rule 4 stops folding what the
            answer was meant to fold.
    """
    aliases = aliases or {}
    return {aliases.get(row["person"], row["person"]): row["chosen"]
            for row in db[COLLECTION].find({"kind": STAFF, "verdict": SAME,
                                            "chosen": {"$ne": None}})
            if row.get("chosen") and row.get("person")}


def github_decisions(db: Database, aliases: dict[str, str] | None = None
                     ) -> dict[frozenset[str], str]:
    """Answers about accounts, keyed by the login and person they are about.

    Read off `members`, not off the evidence. An answer can be given before
    the question exists — from the CLI, or about an account this run has not
    reached yet — and such a document carries no evidence at all, so keying
    on it lost the answer exactly when it mattered.

    Args:
        aliases: Merged-away id to the id that survived, as for `decisions`.
            An account answered about somebody who has since been folded is
            stored under an id nothing carries; without this the answer is
            not found, and the matcher goes on to link an account a person
            had refused.

    The caller knows which half is the account, so the pair needs no order.
    """
    aliases = aliases or {}
    return {frozenset(aliases.get(member, member) for member in row["members"]): row["verdict"]
            for row in db[COLLECTION].find({"kind": GITHUB,
                                            "verdict": {"$exists": True}})}


def decisions(db: Database, aliases: dict[str, str] | None = None,
              kinds: tuple[str, ...] = PERSON_KINDS) -> dict[frozenset[str], str]:
    """Every answer given, keyed by the people it is about.

    Args:
        aliases: Merged-away id to the id that survived, when the caller
            knows it (`fetch_merged_id_map` for the graph, `merged_ids` on
            prepared rows). A person folded into another keeps the answers
            made about them under an id that no longer exists, and without
            this they would quietly stop applying.
        kinds: Which questions count as answers here. The default leaves
            out GITHUB, whose members are an account and a person rather
            than two records of one researcher — the merge rules would look
            such a pair up and never find it, but the pollution is the kind
            of thing that goes unnoticed until it does not.

    Returns:
        Members to verdict. Members are a frozenset, so the caller does not
        have to sort before looking one up.
    """
    aliases = aliases or {}
    found: dict[frozenset[str], str] = {}
    for row in db[COLLECTION].find({"kind": {"$in": list(kinds)},
                                    "verdict": {"$exists": True}}):
        members = frozenset(aliases.get(member, member) for member in row["members"])
        if len(members) < 2:
            # Both sides ended up the same person, so the question is moot:
            # whatever was decided, they are already one node.
            continue
        found[members] = row["verdict"]
    return found


#: Reasons a person can actually settle. One real run held 278 pairs, and
#: only these 18 were worth an eye. Groups count too, whatever their wording.
PRESSING_REASONS = ("identical name with nothing corroborating it",)


def _query(*, pressing: bool = False, answered: bool | None = None,
           skipped: bool | None = None, disputed: bool | None = None,
           kind: str = "", reason: str = "") -> dict:
    """The filter behind both the queue and its counter.

    Built in one place so a tab and the number on it can never disagree.
    """
    query: dict = {}
    if pressing:
        query["$or"] = [{"kind": {"$in": [GROUP, GITHUB, STAFF]}},
                        {"evidence.held_because": {"$in": list(PRESSING_REASONS)}}]
    if answered is not None:
        query["verdict"] = {"$exists": answered}
    if skipped is not None:
        query["skipped_at"] = {"$exists": skipped}
    if disputed is not None:
        query["disputed_at"] = {"$exists": disputed}
    if kind:
        query["kind"] = kind
    if reason:
        query["evidence.held_because"] = reason
    return query


def questions(db: Database, *, limit: int = 50, skip: int = 0, **filters) -> list[dict]:
    """One page of the queue, the longest-waiting first.

    Args:
        limit: How many to return.
        skip: How many to pass over, for paging.
        **filters: See `_query`. `pressing` narrows to the reasons worth a
            person's time, `answered` and `skipped` take True, False or
            None for both, `kind` and `reason` match exactly.
    """
    rows = db[COLLECTION].find(_query(**filters)).sort(
        [("seen_at", 1), ("_id", 1)]).skip(skip).limit(limit)
    return list(rows)


def count(db: Database, **filters) -> int:
    return db[COLLECTION].count_documents(_query(**filters))


def reasons(db: Database) -> list[str]:
    """The reasons actually present, for the filter on the page."""
    return sorted(db[COLLECTION].distinct("evidence.held_because"))
