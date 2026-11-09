"""Questions the pipeline could not answer, and the answers people give.

Three stages reach a point where the evidence runs out. Refusing there is
not a bug: two "A. V. Yulin" with no shared coauthor look identical to the
rules. A refusal is stored as a question, and a person's answer outlives the
run that asked. The answer is consulted *before* the algorithm decides
rather than patched over its result. A fold can be taken apart afterwards
(`pauk.graph.unmerge`), but only by rebuilding the record from the prepared
row it was published from, and only for as long as that row is there.

Four shapes of question:

- a **pair** the person merge rules would not fold, answered "same" or
  "different";
- a **group** those rules refused whole, because it spans two ORCIDs or two
  addresses. It is answered by naming which of its records are one person,
  and `record_split` turns that into the pair answers the rules read;
- a **github account** the matcher could not place, asked once with every
  author still in the running. It is answered by choosing the author it
  belongs to (`record_account_choice`) or "different" for none of them; with
  a single author, "same" or "different";
- a **catalog record**, where the staff directory holds several people under
  one name. Both namesakes are plausible and exactly one is right, so
  `record_choice` names the record instead of taking a side.

Storage sits beside the prepared rows rather than in `pauk/graph/`: these are
written long before anything is published, and `pauk/pipeline/` imports
nothing from the graph layer.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from hashlib import sha256
from itertools import combinations

from pymongo.database import Database

logger = logging.getLogger(__name__)

COLLECTION = "review_pairs"

PAIR = "person_pair"
GROUP = "person_group"
#: An account against the author it may belong to: a login and a person.
GITHUB = "github_person"
#: A person against the catalog records their name matches; answered by choice.
STAFF = "staff_record"
KINDS = (PAIR, GROUP, GITHUB, STAFF)

#: What the merge rules read: the kinds that pair two records of one person.
PERSON_KINDS = (PAIR, GROUP)

SAME = "same"
DIFFERENT = "different"
VERDICTS = (SAME, DIFFERENT)
DISPUTE_FIELDS = ("disputed_at", "disputed_rule", "disputed_members", "disputed_token")

#: Which pass asked. The same pair can come from both, and it is one question.
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
    # The key joins on ":" and the form on ",": fail loudly, not silently.
    bad = [member for member in unique if ":" in member or "," in member]
    if bad:
        raise ReviewError(f"an id cannot contain ':' or ',': {', '.join(bad)}")
    return ":".join([kind, *unique])


def members_of(row: dict) -> list[str]:
    """What one held report row is about, whichever shape it has.

    Not always people: a catalog question pairs a person with directory
    rows, and a github question with an account. Callers should not have to
    know which shape they are holding, so the four are read in one place,
    together with `kind_of` and `names_of` just below, which split on the
    same fields.
    """
    if "records" in row:
        return sorted({row["person"], *row["records"]})
    if "login" in row:
        return sorted({row["login"], row["person"],
                       *(candidate["person"] for candidate in row.get("candidates") or [])})
    if "persons" in row:
        return sorted(set(row["persons"]))
    return sorted({row["person_a"], row["person_b"]})


def kind_of(row: dict) -> str:
    """Which question a report row is, read off the fields it carries.

    Each producer names its subjects its own way: the catalog check a
    `person` and `records`, the github matcher a `login`, a `person` and the
    `candidates` beside them, a refused group a list of `persons`, a held
    pair a `person_a` and a `person_b`.
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
        by_id = {row["login"]: row["login"], row["person"]: row.get("name_raw"),
                 **{candidate["person"]: candidate.get("name_raw")
                    for candidate in row.get("candidates") or []}}
    elif "persons" in row:
        by_id = dict(zip(row["persons"], row.get("names") or [], strict=False))
    else:
        by_id = {row["person_a"]: row.get("name_a"), row["person_b"]: row.get("name_b")}
    return [by_id.get(member) for member in members]


def record_held(db: Database, report: list[dict], source: str = STAGE,
                *, aliases: dict[str, str] | None = None) -> int:
    """Store what a run refused to decide, as questions.

    Only held rows create questions. Automatic separations retire an older
    unanswered pair, without pretending that a human answered it.

    An answer already given is never touched. The evidence and the time it
    was last seen are refreshed, because a pair the rules keep re-examining
    may have gained a shared coauthor since somebody looked at it, and the
    conflict screen compares the two.

    Args:
        db: Mongo database.
        report: Rows any of the producers held back: `plan_person_merges`,
            the github matcher, or the catalog check. See `kind_of` for the
            shapes.
        source: Which pass asked, `STAGE` or `GRAPH`.

    Returns:
        How many questions were written or refreshed.
    """
    moment = _now()
    # Look up the existing queue once, not once per automatically rejected
    # candidate. Large runs can contain millions of rejected pairs.
    pending: dict[str, list[str]] = {}
    aliases = aliases or {}
    for question in db[COLLECTION].find(
        {"kind": PAIR, "verdict": {"$exists": False}}, {"_id": 1, "members": 1}
    ):
        members = {aliases.get(member, member) for member in question["members"]}
        if len(members) == 2:
            pending.setdefault(question_id(PAIR, list(members)), []).append(question["_id"])
    for row in report:
        if row.get("status") != "separate":
            continue
        key = question_id(PAIR, members_of(row))
        for pending_id in pending.get(key, ()):
            db[COLLECTION].update_one(
                {"_id": pending_id, "verdict": {"$exists": False}},
                {"$set": {"auto_resolved_at": moment, "auto_resolution": row,
                          "seen_at": moment, "source": source}})
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
             "$unset": {"auto_resolved_at": "", "auto_resolution": ""},
             "$setOnInsert": {"kind": kind, "members": members}},
            upsert=True)
        written += 1
    if written:
        logger.info("review: %d question(s) from the %s pass", written, source)
    return written


def record_disputed(db: Database, report: list[dict], *, aliases: dict[str, str] | None = None,
                    github_scope: dict[str, set[str]] | None = None) -> int:
    """Note where the rules have changed their mind about a settled pair.

    Somebody said two records are two people, or that an account is not
    theirs; the evidence has moved since, and a rule that had nothing to
    stand on now fires. The answer stays in force, which is the point of
    storing it, but the disagreement is worth a person's eye, exactly like
    a source that starts contradicting a hand edit (see
    `pauk.graph.overrides`).

    GitHub disputes follow the effective answer through person aliases and
    mark its original question, keeping the answer's members and history.
    github_scope identifies evaluated accounts and selected authors; an
    omitted profile cannot retract an earlier discrepancy.

    Returns:
        How many disagreements were noted.
    """
    moment = _now()
    noted = 0
    refresh_github = github_scope is not None or aliases is not None or any("login" in row for row in report)
    aliases = aliases or {}
    github_answers = _github_answers(db, aliases) if refresh_github else None
    person_answers = None
    if github_answers is not None:
        active_pairs = {frozenset(aliases.get(member, member) for member in (row["person"], row["login"]))
                        for row in report if row.get("status") == "disputed" and "login" in row}
        _retire_github_disputes(db, github_answers, aliases, github_scope, active_pairs)
    for row in report:
        if row.get("status") != "disputed":
            continue
        kind, members = kind_of(row), members_of(row)
        if kind == GITHUB:
            if github_answers is None:
                github_answers = _github_answers(db, aliases)
            subjects = {aliases.get(member, member) for member in (row["person"], row["login"])}
            effective = _effective_github_answer(github_answers, subjects)
            if effective is None:
                continue
            verdict, question = effective
            if verdict != DIFFERENT:
                continue
            found = {"_id": question["_id"], "verdict": question["verdict"],
                     "chosen": question.get("chosen"), "decided_at": question.get("decided_at"),
                     "acknowledged_dispute_token": question.get("acknowledged_dispute_token")}
            original_members = sorted(member for member in question["members"]
                                      if aliases.get(member, member) in subjects)
            token = _github_dispute_token(question["_id"], original_members, row)
            if question.get("acknowledged_dispute_token") == token:
                db[COLLECTION].update_one(
                    {**found, "disputed_token": question.get("disputed_token")},
                    {"$unset": dict.fromkeys(DISPUTE_FIELDS, "")})
                continue
            changes = {"disputed_at": moment, "disputed_rule": row.get("rule"),
                       "disputed_members": original_members, "disputed_token": token, "login": row["login"]}
        else:
            if kind in PERSON_KINDS:
                if person_answers is None:
                    person_answers = _person_answers(db, aliases, PERSON_KINDS)
                effective = person_answers.get(frozenset(aliases.get(member, member) for member in members))
                if effective is None:
                    continue
                question = effective[1]
                found = {"_id": question["_id"], "verdict": question["verdict"],
                         "decided_at": question.get("decided_at")}
            else:
                found = {"_id": question_id(kind, members)}
            changes = {"disputed_at": moment, "disputed_rule": row.get("rule")}
        result = db[COLLECTION].update_many(
            found, {"$set": changes})
        noted += result.matched_count
    if noted:
        logger.warning("review: %d answered pair(s) the rules would now merge", noted)
    return noted


def _effective_github_answer(answers: dict[frozenset[str], tuple[str, dict]],
                             subjects: set[str]) -> tuple[str, dict] | None:
    effective = None
    for members, answer in answers.items():
        if subjects <= members:
            effective = answer
    return effective


def _github_dispute_token(question_id: str, members: list[str], row: dict) -> str:
    """Fingerprint evidence independently of run times and current alias ids."""
    details = {"question": question_id, "members": members, "rule": row.get("rule"),
               "signals": row.get("signals") or [], "evidence": row.get("evidence") or {}}
    return sha256(json.dumps(details, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _retire_github_disputes(db: Database, answers: dict[frozenset[str], tuple[str, dict]],
                            aliases: dict[str, str], scope: dict[str, set[str]] | None,
                            active_pairs: set[frozenset[str]]) -> None:
    for question in db[COLLECTION].find({"kind": GITHUB, "disputed_at": {"$exists": True}}):
        members = question.get("disputed_members")
        if members:
            pairs = [members]
        elif len(question["members"]) == 2:
            pairs = [question["members"]]
        else:
            # Older group disputes do not identify which refusal was challenged.
            login = question.get("login") or (question.get("evidence") or {}).get("login")
            if not login or login not in question["members"]:
                continue
            pairs = [[login, member] for member in question["members"] if member != login]
        subjects = [{aliases.get(member, member) for member in pair} for pair in pairs]
        effective = [_effective_github_answer(answers, pair) for pair in subjects]
        if any(answer is not None and answer[0] == DIFFERENT and answer[1]["_id"] == question["_id"]
               for answer in effective):
            login = question.get("login") or (question.get("evidence") or {}).get("login")
            covered = (scope or {}).get(login)
            if covered is None or any(pair - {aliases.get(login, login)} - covered for pair in subjects):
                continue
            if any(frozenset(pair) in active_pairs for pair in subjects):
                continue
        db[COLLECTION].update_one(
            {"_id": question["_id"], "disputed_at": question["disputed_at"],
             "disputed_token": question.get("disputed_token")},
            {"$unset": dict.fromkeys(DISPUTE_FIELDS, "")})


def _write_answer(db: Database, key: str, update: dict, *, dispute_token: str = "",
                  chosen: str | None = None) -> None:
    query = {"_id": key}
    update["$unset"].update(dict.fromkeys(DISPUTE_FIELDS, ""))
    if dispute_token:
        query.update(disputed_token=dispute_token, verdict=update["$set"]["verdict"])
        if chosen is not None:
            query["chosen"] = chosen
        update["$set"]["acknowledged_dispute_token"] = dispute_token
    else:
        update["$unset"]["acknowledged_dispute_token"] = ""
    result = db[COLLECTION].update_one(query, update, upsert=not bool(dispute_token))
    if dispute_token and not result.matched_count:
        current = db[COLLECTION].find_one({"_id": key}) or {}
        if (not current.get("disputed_at") and current.get("acknowledged_dispute_token") == dispute_token
                and current.get("verdict") == update["$set"]["verdict"]
                and (chosen is None or current.get("chosen") == chosen)):
            # A replay must not make this answer newer than another question's answer.
            return
        raise ReviewError("вопрос или доказательства изменились; откройте его заново")


def retire_github(db: Database, outcomes: dict[str, dict | None], people: set[str],
                  aliases: dict[str, str] | None = None, *,
                  account_scopes: dict[str, set[str]] | None = None) -> int:
    """Close open account questions a run has settled without a person.

    An account is asked about once, so any other open question about it is
    either answered by the rules or replaced by the one asked now, which
    names a new set of authors. Only questions the run could judge: a login
    it settled and authors it covers, directly or through a fold. Closed by
    `auto_resolved_at`, never a verdict.

    Args:
        outcomes: Each login the run settled, with its decision row, or None
            when no author fitted. A row still for review is the question
            asked now, and stays open.
        people: The persons the run covers; a scoped run covers its selection only.
        aliases: Folded-away person id to the id that survived.
        account_scopes: Authors actually covered for each account, including
            counterparts changed by a cross-group handoff.

    Returns:
        How many questions were closed.
    """
    aliases = aliases or {}
    moment = _now()
    asked = {question_id(GITHUB, members_of(row))
             for row in outcomes.values() if row and row["decision"] == "review"}
    retired = 0
    for question in db[COLLECTION].find({"kind": GITHUB, "verdict": {"$exists": False},
                                         "auto_resolved_at": {"$exists": False},
                                         "evidence.login": {"$in": list(outcomes)}}):
        if question["_id"] in asked:
            continue
        login = question["evidence"]["login"]
        covered = account_scopes.get(login, set()) if account_scopes is not None else people
        if any(aliases.get(member, member) not in covered
               for member in question["members"] if member != login):
            continue
        result = db[COLLECTION].update_one(
            {"_id": question["_id"], "verdict": {"$exists": False}},
            {"$set": {"auto_resolved_at": moment,
                      "auto_resolution": outcomes[login] or {"login": login, "decision": "rejected"}}})
        retired += result.modified_count
    if retired:
        logger.info("review: %d account question(s) settled by the rules", retired)
    return retired


def record_verdict(db: Database, kind: str, members: list[str], verdict: str,
                   actor: str = "unknown", note: str = "",
                   names: list[str | None] | None = None, *, dispute_token: str = "") -> dict:
    """Write down what a person decided about one question.

    The question need not exist yet. Somebody may answer from the CLI about
    a pair the current data no longer produces, and the answer still has to
    hold when it does.

    Raises:
        ReviewError: Unknown verdict, or "same" on a group. A group is
            refused because its members disagree about an identity field,
            so "these are all one person" would be a decision to ignore
            two different ORCIDs, which the merge itself would refuse.
    """
    if verdict not in VERDICTS:
        raise ReviewError(f"unknown verdict: {verdict!r} (known: {', '.join(VERDICTS)})")
    if kind == GROUP and verdict == SAME:
        raise ReviewError("a refused group cannot be answered 'same'; "
                          "answer the pairs inside it instead")
    if kind == STAFF and verdict == SAME:
        # "Yes" names nobody: the queue would look answered and change nothing.
        raise ReviewError("a catalog question is answered by choosing a record; "
                          "see record_choice")
    if kind == GITHUB and verdict == SAME and len(set(members)) > 2:
        raise ReviewError("an account asked about several authors is answered by "
                          "choosing one; see record_account_choice")
    key = question_id(kind, members)
    moment = _now()
    _write_answer(
        db, key,
        {"$set": {"verdict": verdict, "actor": actor, "note": note,
                  "decided_at": moment},
         # An answer settles what a skip postponed and what the rules disputed.
         "$unset": {"skipped_at": "", "skipped_by": "",
                    # A binary answer replaces a choice made from an older form.
                    "chosen": "",
                    "auto_resolved_at": "", "auto_resolution": ""},
         "$setOnInsert": {"kind": kind, "members": sorted(set(members)),
                          # A pair answered before any run held it has no evidence.
                          "evidence": {"names": names} if names else {},
                          "seen_at": moment, "source": STAGE}},
        dispute_token=dispute_token)
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
    Saying only "these two are one person" is not enough on its own: the
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
            pair, or is the whole group, which is the answer the conflict
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
    # The group knows the names; without them the pairs are rows of bare ids.
    asked = db[COLLECTION].find_one({"_id": question_id(GROUP, members)}) or {}
    known = dict(zip(members, (asked.get("evidence") or {}).get("names") or [],
                     strict=False))

    def named(*people: str) -> list[str | None]:
        return [known.get(person) for person in people]

    # Inside the subset first: a write that stops halfway merges nothing.
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
    rules never held, and keeping one of those after withdrawal would leave
    a row in the queue with no reason and nothing to decide on. A held
    question always says why it was held; that is what tells the two apart.

    Raises:
        ReviewError: The answer has already folded two records into one.
    """
    key = question_id(kind, members)
    asked = db[COLLECTION].find_one({"_id": key})
    if asked is not None and asked.get("applied_at"):
        # They are one node by now: dropping the answer strands both.
        raise ReviewError(f"{key} has been folded; take it apart instead (record_undo)")
    if asked is not None and not (asked.get("evidence") or {}).get("held_because"):
        return db[COLLECTION].delete_one({"_id": key}).deleted_count > 0
    result = db[COLLECTION].update_one(
        {"_id": key},
        {"$unset": {"verdict": "", "actor": "", "note": "",
                    "decided_at": "", "applied_at": "",
                    # The record chosen goes with the answer that chose it.
                    "chosen": "",
                    # Nothing left to disagree with once the answer is gone.
                    **dict.fromkeys((*DISPUTE_FIELDS, "acknowledged_dispute_token"), "")}})
    return result.matched_count > 0


def mark_applied_merges(db: Database, merged_into: dict[str, str]) -> int:
    """Record every answered pair a run has just folded into one record.

    `mark_applied` is for the panel, which folds one pair and knows which.
    A run folds hundreds at once and knows only what became what, so the
    answers are matched against that map instead: a "same" whose two ids
    now land on one record has been applied, whoever applied it.

    Without this the queue would keep promising "will merge on the next run"
    for pairs the last run had already merged, and offer to take the answer
    back, which would leave the records folded and the question open.

    Args:
        merged_into: Folded-away id to the record that survived. What
            `folded_ids` builds from the prepared rows and
            `fetch_merged_id_map` reads off the graph.

    Returns:
        How many answers were marked.
    """
    moment = _now()
    marked = 0
    for asked in db[COLLECTION].find({"kind": PAIR, "verdict": SAME,
                                      "applied_at": {"$exists": False}}):
        landed = {merged_into.get(member, member) for member in asked["members"]}
        if len(landed) == 1:
            db[COLLECTION].update_one({"_id": asked["_id"]}, {"$set": {"applied_at": moment}})
            marked += 1
    if marked:
        logger.info("review: %d answered pair(s) are now one record", marked)
    return marked


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
    next run, and the answer would undo the undo.

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
    rides along with the verdict: "same" plus the record chosen, or
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
        # On the document, not in the evidence: an answer can come first.
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


def record_account_choice(db: Database, login: str, persons: list[str], chosen: str,
                          actor: str = "unknown", note: str = "", *, dispute_token: str = "") -> dict:
    """Say which of the authors asked about an account it belongs to.

    Like `record_choice`: several authors fit and at most one is right. "None
    of them" is a plain "different" on the same question, see `record_verdict`.

    Raises:
        ReviewError: The chosen author is not one of the ones asked about.
    """
    if chosen not in persons:
        raise ReviewError("that author is not one of the ones asked about")
    members = [login, *persons]
    key = question_id(GITHUB, members)
    moment = _now()
    _write_answer(
        db, key,
        # The login is kept on the document: the members alone do not say which one it is.
        {"$set": {"verdict": SAME, "chosen": chosen, "login": login,
                  "actor": actor, "note": note, "decided_at": moment},
         "$unset": {"skipped_at": "", "skipped_by": "",
                    "auto_resolved_at": "", "auto_resolution": ""},
         "$setOnInsert": {"kind": GITHUB, "members": sorted(set(members)),
                          "evidence": {}, "seen_at": moment, "source": STAGE}},
        dispute_token=dispute_token, chosen=chosen)
    logger.info("review: %s belongs to %s, said %s", login, chosen, actor)
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
    """Answers about accounts, keyed by the login and the persons they are about.

    Read off `members`, not off the evidence. An answer can be given before
    the question exists (from the CLI, or about an account this run has not
    reached yet), and such a document carries no evidence at all, so keying
    on it would lose the answer exactly when it mattered.

    The caller knows which member is the account, so the members need no
    order. A "different" about several authors refuses each of them; a choice
    is a "yes" to the author chosen and a "no" to the others it was chosen over.

    Args:
        aliases: Merged-away id to the id that survived, as for `decisions`.
            An account answered about somebody who has since been folded is
            stored under an id nothing carries; without this the answer is
            not found, and the matcher goes on to link an account a person
            had refused.

    Returns:
        Members to verdict, oldest answer first: where two answers cover one
        pair, a caller applying them in order keeps the later one.
    """
    return {members: verdict for members, (verdict, _question) in _github_answers(db, aliases or {}).items()}


def _github_answers(db: Database, aliases: dict[str, str]) -> dict[frozenset[str], tuple[str, dict]]:
    """Keep the source question beside each answer, in application order."""
    found: dict[frozenset[str], tuple[str, dict]] = {}
    for row in db[COLLECTION].find({"kind": GITHUB, "verdict": {"$exists": True}}
                                   ).sort("decided_at", 1):
        answers = [(row["members"], row["verdict"])]
        if row["verdict"] == SAME and row.get("chosen"):
            # The "yes" last: an author folded into the chosen one is the chosen one.
            answers = [*(([row["login"], member], DIFFERENT) for member in row["members"]
                         if member not in (row["login"], row["chosen"])),
                       ([row["login"], row["chosen"]], SAME)]
        for members, verdict in answers:
            key = frozenset(aliases.get(member, member) for member in members)
            # Popped first, so a repeated key moves to the place of its later answer.
            found.pop(key, None)
            found[key] = verdict, row
    return found


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
            than two records of one researcher; the merge rules would look
            such a pair up and never find it, and the stray entries would go
            unnoticed.

    Returns:
        Members to verdict. Members are a frozenset, so the caller does not
        have to sort before looking one up.
    """
    return {members: verdict for members, (verdict, _question) in
            _person_answers(db, aliases or {}, kinds).items()}


def _person_answers(db: Database, aliases: dict[str, str],
                     kinds: tuple[str, ...]) -> dict[frozenset[str], tuple[str, dict]]:
    found: dict[frozenset[str], tuple[str, dict]] = {}
    for row in db[COLLECTION].find({"kind": {"$in": list(kinds)},
                                    "verdict": {"$exists": True}}).sort("decided_at", 1):
        members = frozenset(aliases.get(member, member) for member in row["members"])
        if len(members) < 2:
            # Both sides are the same person by now: the question is moot.
            continue
        found[members] = row["verdict"], row
    return found


#: Reasons worth a person's time: one run held 278 pairs, 18 of them these.
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
    if answered is False:
        query["auto_resolved_at"] = {"$exists": False}
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
