"""Classify forks, judge independence, and build the repository-year panel."""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from pathlib import Path

DATA = Path("/analysis/forks/data")
OUT = Path("/analysis/forks/out")
OUT.mkdir(parents=True, exist_ok=True)

SOURCE_EXT = {
    ".py", ".pyx", ".ipynb", ".js", ".jsx", ".ts", ".tsx", ".java", ".kt", ".scala",
    ".c", ".h", ".cc", ".cpp", ".cxx", ".hpp", ".cu", ".f90", ".f", ".for",
    ".go", ".rs", ".rb", ".php", ".jl", ".r", ".m", ".swift", ".cs", ".sh", ".lua",
    ".sql", ".proto", ".v", ".sv", ".vhd", ".tex",
}
NON_CODE = re.compile(
    r"(^|/)(docs?|examples?/docs|\.github|tests?|test_data|benchmarks?|ci)(/|$)"
    r"|(^|/)(README|LICENSE|CHANGELOG|CONTRIBUTING|CITATION|AUTHORS|NOTICE|Makefile|Dockerfile"
    r"|\.gitignore|\.gitattributes|\.pre-commit-config\.yaml|requirements[\w.-]*\.txt"
    r"|environment\.ya?ml|setup\.cfg|pyproject\.toml|poetry\.lock|\.travis\.yml|tox\.ini)(\.|$|/)",
    re.IGNORECASE,
)
ITMO_TEXT = re.compile(r"(?<!\w)itmo(?!\w)", re.IGNORECASE)
NON_ITMO_ORG = re.compile(
    r"(university|universit|institute|inc\.?|ltd|llc|gmbh|corp|laboratory|lab\b|college|school)",
    re.IGNORECASE,
)
NON_FUNCTIONAL = re.compile(
    r"(^|/)(examples?|experiments?|cases?|tutorials?|docs?|tests?|test|benchmarks?|notebooks?)(/|$)",
    re.IGNORECASE,
)


def load_json(name: str):
    return json.loads((DATA / name).read_text(encoding="utf-8"))


def load_lines(name: str):
    with (DATA / f"{name}.jsonl").open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def is_code_file(path: str) -> bool:
    lowered = path.lower()
    if NON_CODE.search(lowered):
        return False
    return Path(lowered).suffix in SOURCE_EXT


def is_functional_file(path: str) -> bool:
    """Code that carries the project's functionality, not experiments or docs."""
    lowered = path.lower()
    if not is_code_file(lowered):
        return False
    if Path(lowered).suffix == ".ipynb" or NON_FUNCTIONAL.search(lowered):
        return False
    return True


def year_of(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return int(value[:4])
    except ValueError:
        return None


def main() -> None:
    repos = {row["repo_id"]: row for row in load_json("forks_by_repo.json")}
    compares = load_json("fork_compare.json")
    pulls = {(row["upstream"], row["fork_owner"]): row for row in load_json("fork_pulls.json")}
    owners = {(row["login"] or "").casefold(): row for row in load_json("fork_owners.json")}
    mongo_repos = {row["id"]: row for row in load_lines("repositories")}
    itmo_repos = {row["id"]: row for row in load_json("repo_ownership.json") if row["is_itmo"]}
    pauk_logins = {row["github"].casefold() for row in load_lines("graph_persons_github") if row.get("github")}
    orgs = {
        item["login"].casefold()
        for item in json.loads(Path("/analysis/itmo_github_orgs.json").read_text(encoding="utf-8"))["organizations"]
    }

    github_of_person = {
        row["id"]: row["github"].casefold() for row in load_lines("graph_persons_github") if row.get("github")
    }
    authored_by_pub: dict[str, set[str]] = defaultdict(set)
    for row in load_lines("authored"):
        login = github_of_person.get(row["person_id"])
        if login:
            authored_by_pub[row["publication_id"]].add(login)

    upstream_authors: dict[str, set[str]] = defaultdict(set)
    for repo_id, doc in mongo_repos.items():
        for pub_id in doc.get("publication_ids") or []:
            upstream_authors[repo_id] |= authored_by_pub.get(pub_id, set())

    # A repository counts as ITMO's own scientific code when it is not itself a
    # fork and its owner is either an ITMO organization or an author of a
    # publication that implements it. A bare Person.github match is weaker: a
    # cited third-party tool can belong to someone who merely co-authored a paper.
    itmo_repos = {
        repo_id: repo
        for repo_id, repo in itmo_repos.items()
        if not (mongo_repos.get(repo_id) or {}).get("is_fork")
        and (
            "org_catalog" in repo["itmo_sources"]
            or repo["owner"] in upstream_authors.get(repo_id, set())
        )
    }

    forks = []
    for row in compares:
        repo_id = row["repo_id"]
        doc = mongo_repos.get(repo_id) or {}
        owner = (row["fork_owner"] or "").casefold()
        status = row.get("status")
        ahead = row.get("ahead_by") or 0
        files = row.get("files") or []
        commits = row.get("commits") or []
        code_files = [f for f in files if is_code_file(f.get("filename") or "")]
        functional = [
            f for f in files
            if is_functional_file(f.get("filename") or "")
            and f.get("status") in ("added", "modified")
        ]
        functional_additions = sum(int(f.get("additions") or 0) for f in functional)
        own_commits = [c for c in commits if (c.get("login") or "").casefold() == owner]
        added = [f for f in code_files if f.get("status") == "added"]
        code_additions = sum(int(f.get("additions") or 0) for f in code_files)
        pull = pulls.get((row["upstream"], row["fork_owner"])) or {}
        pull_items = pull.get("pulls") or []
        merged = [p for p in pull_items if p.get("merged_at")]
        open_pr = [p for p in pull_items if p.get("state") == "open"]

        if status != 200:
            category, reason = "unknown", f"сравнение недоступно, HTTP {status}"
        elif not ahead:
            category, reason = "inactive", "нет коммитов сверх исходной ветки"
        elif pull_items:
            category = "contribution"
            state = "слит" if merged else "открыт" if open_pr else "закрыт"
            reason = f"есть pull request #{pull_items[0]['number']} ({state})"
        elif not own_commits:
            category, reason = "unknown", "расхождение есть, но коммитов владельца форка нет"
        elif functional and functional_additions >= 20:
            category = "derivative"
            reason = (
                f"собственных коммитов {len(own_commits)}, файлов функциональности {len(functional)}, "
                f"добавлено в них строк {functional_additions}"
            )
        else:
            category = "modified"
            reason = (
                f"собственных коммитов {len(own_commits)}, файлов кода {len(code_files)}, "
                f"функциональных правок {functional_additions}"
            )

        profile = (owners.get(owner) or {}).get("user") or {}
        login = profile.get("login") or ""
        is_bot = (
            profile.get("type") == "Bot"
            or login.endswith("[bot]")
            or owner.endswith("bot")
            or "bot" in owner
        )
        profile_text = " ".join(
            str(profile.get(key) or "") for key in ("company", "location", "bio", "name")
        )
        contributors = {(c or "").casefold() for c in doc.get("contributors") or []}
        if owner in contributors or owner in upstream_authors.get(repo_id, set()):
            independence = "internal"
            ind_reason = "владелец входит в исходную команду: участники репозитория или авторы публикаций"
        elif owner in pauk_logins or owner in orgs or "itmo" in owner or ITMO_TEXT.search(profile_text):
            independence = "itmo_other_team"
            ind_reason = "владелец связан с ИТМО по данным PAUK или профилю, но не участник исходной команды"
        elif is_bot:
            independence = "bot"
            ind_reason = "учётная запись автоматизирована"
        elif profile.get("company") and NON_ITMO_ORG.search(str(profile.get("company"))):
            independence = "external"
            ind_reason = f"профиль называет другую организацию: {profile['company']}"
        else:
            independence = "unknown"
            ind_reason = "сведений о принадлежности владельца нет"

        first_commit = min((c.get("date") for c in own_commits if c.get("date")), default=None)
        forks.append({
            "repo_id": repo_id,
            "upstream": row["upstream"],
            "upstream_owner": row["upstream_owner"],
            "fork_owner": row["fork_owner"],
            "fork_name": row["fork_name"],
            "fork_url": f"https://github.com/{row['fork_owner']}/{(row.get('fork_name') or '').split('/')[-1]}",
            "fork_created_at": row.get("fork_created_at"),
            "fork_pushed_at": row.get("fork_pushed_at"),
            "fork_year": year_of(row.get("fork_created_at")),
            "status": status,
            "ahead_by": ahead,
            "behind_by": row.get("behind_by"),
            "files_total": len(files),
            "code_files": [f.get("filename") for f in code_files],
            "code_additions": code_additions,
            "functional_files": [f.get("filename") for f in functional],
            "functional_additions": functional_additions,
            "added_files": len(added),
            "own_commits": len(own_commits),
            "first_own_commit": first_commit,
            "prs": pull_items,
            "pr_merged": bool(merged),
            "category": category,
            "category_reason": reason,
            "independence": independence,
            "independence_reason": ind_reason,
            "is_bot": is_bot,
        })

    (OUT / "population.json").write_text(
        json.dumps({"repo_ids": sorted(itmo_repos)}, ensure_ascii=False), encoding="utf-8"
    )
    (OUT / "forks_classified.json").write_text(json.dumps(forks, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"population repositories: {len(itmo_repos)}")
    print(f"forks: {len(forks)}")
    print("categories:", dict(Counter(f["category"] for f in forks)))
    print("independence:", dict(Counter(f["independence"] for f in forks)))
    deriv = [f for f in forks if f["category"] == "derivative"]
    print(f"derivative: {len(deriv)}")
    print("derivative independence:", dict(Counter(f["independence"] for f in deriv)))

    panel = []
    for repo_id, repo in itmo_repos.items():
        created = year_of((repos.get(repo_id) or {}).get("created_at")) or 2020
        rows = [f for f in forks if f["repo_id"] == repo_id and f["fork_year"]]
        for year in range(created, 2027):
            in_year = [f for f in rows if f["fork_year"] == year]
            derivatives = [f for f in in_year if f["category"] == "derivative"]
            independent = [
                f for f in derivatives if f["independence"] in ("external", "itmo_other_team", "unknown")
            ]
            next_year = [
                f for f in rows
                if f["fork_year"] == year + 1 and f["category"] == "derivative"
                and f["independence"] in ("external", "itmo_other_team", "unknown")
            ]
            panel.append({
                "repo_id": repo_id,
                "repo": f"{repo['owner']}/{repo['name']}",
                "year": year,
                "created": year == created,
                "forks": len(in_year),
                "changed_forks": sum(1 for f in in_year if f["category"] in ("derivative", "modified", "contribution")),
                "derivative_forks": len(derivatives),
                "independent_derivative_forks": len(independent),
                "target_next_year": 1 if next_year else 0,
            })
    (OUT / "panel.json").write_text(json.dumps(panel, ensure_ascii=False, indent=1), encoding="utf-8")
    positives = [p for p in panel if p["target_next_year"] == 1]
    print(f"panel rows: {len(panel)}; positive rows: {len(positives)}; repos with a positive year: {len({p['repo_id'] for p in positives})}")


if __name__ == "__main__":
    main()
