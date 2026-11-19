"""Export PAUK commit history to CSV, attributed to the branch the work was done on.

A commit counts toward the branch that was checked out when it was written, not
toward wherever it ended up after merges. `main` is the base; every other
branch's own work is `git rev-list main..<branch>`. Commits reachable from two
branches (a fix branch that continues an earlier feature branch) are counted
once, in the branch that was born first.

Identities: git records one person under several emails. A
`<id>+<login>@users.noreply.github.com` address names its GitHub login, and a
login that matches another address' author name is the same person - that is a
proof, not a guess. Anything left ambiguous goes to `identity_review.csv`
instead of being merged silently.

Run: python scripts/git_activity_to_csv.py [--out data/cache/git_activity]
"""

from __future__ import annotations

import argparse
import csv
import subprocess
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASE = "main"
NOREPLY = "users.noreply.github.com"


def git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout


# --- branches ----------------------------------------------------------------

def branches() -> list[str]:
    """main first, then every remote branch. origin/main just mirrors local main."""
    found = []
    for line in git(
        "for-each-ref", "--format=%(refname:short)", "refs/heads", "refs/remotes/origin"
    ).splitlines():
        name = line.strip()
        if not name or name in {"origin", "origin/HEAD", "origin/main"}:
            continue
        found.append(BASE if name == "main" else name)
    return found


def revs(ref: str) -> list[str]:
    args = ["rev-list", ref] if ref == BASE else ["rev-list", f"{BASE}..{ref}"]
    return git(*args).split()


def birth_date(commits: list[str]) -> str:
    """Earliest authored date among the branch's own commits.

    `git log <shas>` would answer "earliest commit in the repository", because it
    walks every ancestor of each sha; `--no-walk` keeps it to the listed commits.
    """
    if not commits:
        return ""
    dates = []
    for start in range(0, len(commits), 200):
        dates += git("log", "--no-walk=unsorted", "--format=%aI",
                     *commits[start:start + 200]).splitlines()
    return min(dates) if dates else ""


# --- identity ----------------------------------------------------------------

def author_rows() -> list[tuple[str, str]]:
    raw = git("log", "--all", "--format=%an%x09%ae").splitlines()
    pairs = {tuple(line.split("\t")) for line in raw if "\t" in line}
    return sorted(pairs)


def github_login(email: str) -> str | None:
    """`12345+some-login@users.noreply.github.com` -> `some-login`."""
    marker = "@" + NOREPLY
    if not email.endswith(marker):
        return None
    local = email[: -len(marker)]
    return local.split("+", 1)[1] if "+" in local else local


def gmail_fold(email: str) -> str:
    """Gmail ignores dots and case in the local part, so nicl.nno@ == Niclnno@."""
    local, _, domain = email.partition("@")
    return (local.replace(".", "").casefold() + "@" + domain) if domain == "gmail.com" else email


# Not proven by anything in the repo, so not merged automatically. Flip an entry
# to its canonical address once you know the two are the same person.
EXTRA_MERGES: dict[str, str] = {
    # "pyro@macbook.local": "pyrolusilicate@icloud.com",   # "Артём" == Artem Sidorov
    # "nicl.nno@yandex.ru": "nicl.nno@gmail.com",          # same name, same local part
    # "yaroslavdeoscar@gmail.com": "fl1pcoin",              # Remezov vs Perchick?
}


def build_identities(pairs: list[tuple[str, str]], email_counts: Counter) -> tuple[dict[str, str], list[dict]]:
    """Map every author email to one identity key, merging only on proof."""
    by_name: dict[str, list[str]] = defaultdict(list)
    for name, email in pairs:
        by_name[name.casefold()].append(email)

    owner = {email: email for _name, email in pairs}

    # A GitHub login that is also another address' author name is the same person.
    for _name, email in pairs:
        login = github_login(email)
        if not login:
            continue
        for other in by_name.get(login.casefold(), []):
            if other != email:
                owner[other] = email

    # Same Gmail account written two ways: nicl.nno@ and Niclnno@ are one inbox.
    by_fold: dict[str, str] = {}
    for _name, email in pairs:
        by_fold.setdefault(gmail_fold(email), email)
    for _name, email in pairs:
        canonical = by_fold[gmail_fold(email)]
        if canonical != email:
            owner[email] = canonical

    for email, canonical in EXTRA_MERGES.items():
        owner[email] = canonical

    # Anything still standing alone, next to the identities it might belong to.
    proven = set(owner.values())
    names_by_email: dict[str, set[str]] = defaultdict(set)
    for name, email in pairs:
        names_by_email[email].add(name)

    def local_part(email: str) -> str:
        return gmail_fold(email).split("@")[0]

    review = []
    for email, commits in sorted(email_counts.items()):
        # An address that was folded into another is settled; one that maps to
        # itself, has no GitHub login to anchor it, is a guess waiting to be made.
        if owner.get(email, email) != email or github_login(email):
            continue
        candidates = {owner.get(o, o) for o in proven if o != email
                      and (names_by_email[o] & names_by_email[email]
                           or local_part(o) == local_part(email))}
        if candidates:
            verdict = "probably the same person as candidate_identity"
        elif len(names_by_email[email]) > 1:
            verdict = "two names on one address - split account or shared inbox"
        else:
            verdict = "standalone, nothing to merge"
        review.append({
            "author_names": " / ".join(sorted(names_by_email[email])),
            "author_email": email, "author_commits": commits,
            "candidate_identity": "; ".join(sorted(candidates)),
            "verdict": verdict,
        })
    return owner, review


# --- commits -----------------------------------------------------------------

def commit_meta(sha: str) -> dict:
    fmt = "%H%x1f%an%x1f%ae%x1f%aI%x1f%cn%x1f%ce%x1f%cI%x1f%P%x1f%s"
    out = git("show", "-s", f"--format={fmt}", sha).rstrip("\n").split("\x1f")
    return {
        "sha": out[0], "author_name": out[1], "author_email": out[2],
        "authored_at": out[3], "committer_name": out[4], "committer_email": out[5],
        "committed_at": out[6], "parents": out[7].split(), "subject": out[8],
    }


def numstat(sha: str) -> list[tuple[str, int, int]]:
    rows = []
    for line in git("show", "--numstat", "--format=", sha).splitlines():
        parts = line.split("\t")
        if len(parts) == 3:
            add, rem = parts[0], parts[1]
            rows.append((parts[2], int(add) if add.isdigit() else 0,
                         int(rem) if rem.isdigit() else 0))
    return rows


def assign_branches(per_branch: dict[str, list[str]],
                    born: dict[str, str]) -> dict[str, str]:
    """One branch per commit.

    A commit reachable from several branches went into the one that existed when
    the work was written - the earlier `born` date - so the per-branch totals sum
    to the real commit count instead of counting shared history twice.
    """
    seen_in: dict[str, list[str]] = defaultdict(list)
    for name, shas in per_branch.items():
        for sha in shas:
            seen_in[sha].append(name)
    return {sha: sorted(names, key=lambda n: (born[n], n))[0]
            for sha, names in seen_in.items()}


# --- output ------------------------------------------------------------------

def write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=str(ROOT / "data" / "cache" / "git_activity"))
    args = parser.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    names = branches()
    per_branch = {name: revs(name) for name in names}
    born = {name: birth_date(per_branch[name]) for name in names}

    commit_branch = assign_branches(per_branch, born)

    owner, review = build_identities(
        author_rows(), Counter(git("log", "--all", "--format=%ae").split()))

    commits = []
    files_by_sha: dict[str, list[tuple[str, int, int]]] = {}
    for sha, branch in commit_branch.items():
        meta = commit_meta(sha)
        is_merge = len(meta["parents"]) > 1
        # A merge records who integrated the branch, not who did the work, so it
        # gets no file stats - otherwise merges would outrank real commits.
        rows = [] if is_merge else numstat(sha)
        files_by_sha[sha] = rows
        when = datetime.fromisoformat(meta["authored_at"])
        email = meta["author_email"]
        commits.append({
            "sha": meta["sha"], "branch": branch,
            "author_name": meta["author_name"], "author_email": email,
            "identity": owner.get(email, email),
            "committer_name": meta["committer_name"],
            "is_merge": int(is_merge),
            "authored_at": meta["authored_at"],
            "month": when.strftime("%Y-%m"), "weekday": when.strftime("%a"),
            "hour": when.hour,
            "files_changed": len(rows),
            "insertions": sum(r[1] for r in rows),
            "deletions": sum(r[2] for r in rows),
            "subject": meta["subject"],
        })
    commits.sort(key=lambda r: (r["authored_at"], r["sha"]), reverse=True)
    write_csv(out / "commits.csv", commits)

    # Who worked on what: one row per (branch, identity, file).
    contrib: dict[tuple, dict] = {}
    for row in commits:
        if row["is_merge"]:
            continue
        for path, added, removed in files_by_sha[row["sha"]]:
            key = (row["branch"], row["identity"], row["author_email"], path)
            entry = contrib.setdefault(key, {
                "branch": key[0], "identity": key[1], "author_email": key[2],
                "file": key[3], "commits": 0, "insertions": 0, "deletions": 0,
                "first_commit": row["authored_at"], "last_commit": row["authored_at"]})
            entry["commits"] += 1
            entry["insertions"] += added
            entry["deletions"] += removed
            entry["first_commit"] = min(entry["first_commit"], row["authored_at"])
            entry["last_commit"] = max(entry["last_commit"], row["authored_at"])
    write_csv(out / "contributions.csv",
              sorted(contrib.values(), key=lambda r: (-r["commits"], r["file"])))

    # Per-identity rollup, for the charts.
    rollup: dict[str, dict] = {}
    for row in commits:
        entry = rollup.setdefault(row["identity"], {
            "identity": row["identity"], "emails": set(),
            "commits": 0, "merge_commits": 0, "insertions": 0, "deletions": 0,
            "branches": set(), "files": set(), "days": set(), "weeks": set(),
            "first_commit": row["authored_at"], "last_commit": row["authored_at"]})
        entry["emails"].add(row["author_email"])
        entry["commits"] += 1
        entry["merge_commits"] += row["is_merge"]
        entry["insertions"] += row["insertions"]
        entry["deletions"] += row["deletions"]
        entry["branches"].add(row["branch"])
        entry["files"].update(p for p, _a, _r in files_by_sha[row["sha"]])
        when = datetime.fromisoformat(row["authored_at"])
        entry["days"].add(when.date())
        entry["weeks"].add(when.strftime("%Y-W%V"))
        entry["first_commit"] = min(entry["first_commit"], row["authored_at"])
        entry["last_commit"] = max(entry["last_commit"], row["authored_at"])
    summary = [{
        "identity": e["identity"], "emails": " ".join(sorted(e["emails"])),
        "commits": e["commits"], "merge_commits": e["merge_commits"],
        "work_commits": e["commits"] - e["merge_commits"],
        "insertions": e["insertions"], "deletions": e["deletions"],
        "branches_touched": len(e["branches"]), "files_touched": len(e["files"]),
        "active_days": len(e["days"]), "active_weeks": len(e["weeks"]),
        "first_commit": e["first_commit"], "last_commit": e["last_commit"],
    } for e in rollup.values()]
    write_csv(out / "activity_summary.csv",
              sorted(summary, key=lambda r: -r["work_commits"]))

    branch_rows = [{
        "branch": name,
        "commits": sum(1 for r in commits if r["branch"] == name),
        "merge_commits": sum(r["is_merge"] for r in commits if r["branch"] == name),
        "authors": len({r["identity"] for r in commits if r["branch"] == name}),
        "insertions": sum(r["insertions"] for r in commits if r["branch"] == name),
        "deletions": sum(r["deletions"] for r in commits if r["branch"] == name),
        "born": born[name],
        "first_commit": min((r["authored_at"] for r in commits if r["branch"] == name),
                            default=""),
        "last_commit": max((r["authored_at"] for r in commits if r["branch"] == name),
                           default=""),
    } for name in names]
    write_csv(out / "branches.csv",
              sorted(branch_rows, key=lambda r: -r["commits"]))

    write_csv(out / "identity_review.csv", review)

    print(f"branches {len(names)}  commits {len(commits)}  "
          f"identities {len(rollup)}  needs-review {len(review)}")
    print(f"-> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
