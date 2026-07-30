"""Самопроверка extract.py — без живого Neo4j, чистые dict -> tuple функции.

Запуск: uv run python -m pauk.graph.extract_test
"""

from .extract import NODE_REGISTRY, extract_node, extract_relationships

PERSON_ROW = {
    "id": "p1",
    "name_en": "Ivan Petrov",
    "email": "ivan@itmo.ru",
    "department_ids": ["d1", "d2"],
    "authored": [
        {"publication_id": "pub1", "position": 1, "affiliation": "ITMO", "is_corresponding": True}
    ],
    "contributed_to": [{"repository_id": "r1", "role": "maintainer"}],
    # мусорные рабочие поля PipelinePerson (conveyor.py::to_json() их не
    # исключает, см. pauk-graph doc) — не должны попасть в свойства узла
    "orcid": "0000-0000",
    "affiliation": "какое-то текстовое предположение",
    "email_candidates": [["a@b.com", "page1"]],
    "profile": {"login": "ivanp"},
}

DEPARTMENT_ROW = {"id": "d1", "name_en": "ISU", "name_variants": ["ISU", "ИСиТ"]}

REPOSITORY_ROW = {
    "id": "r1",
    "name": "repo",
    "url": "https://github.com/x/repo",
    "owner_login": "someorg",
}

PUBLICATION_ROW = {
    "id": "pub1",
    "title": "Some paper",
    "mentions_links": [
        {"target_kind": "repository", "repository_url": "https://github.com/x/repo", "context": "see code"},
        {"target_kind": "candidate", "candidate_id": "https://example.com/maybe-repo"},
    ],
}


def test_person_node_drops_stray_fields():
    labels, (node_id, props) = extract_node(PERSON_ROW, NODE_REGISTRY["itmo_person"])
    assert labels == "Person:Itmo"
    assert node_id == "p1"
    assert props.get("name_en") == "Ivan Petrov"
    assert props.get("email") == "ivan@itmo.ru"
    for stray in ("orcid", "affiliation", "email_candidates", "profile",
                  "department_ids", "authored", "contributed_to", "id"):
        assert stray not in props, f"{stray} просочился в свойства узла"


def test_person_relationships():
    rels = extract_relationships(PERSON_ROW, NODE_REGISTRY["itmo_person"])

    belongs = rels[("Person:Itmo", "Department", "BELONGS_TO", "id")]
    assert sorted(belongs) == [("p1", "d1", {}), ("p1", "d2", {})]

    authored = rels[("Person:Itmo", "Publication", "AUTHORED", "id")]
    assert authored == [("p1", "pub1", {"position": 1, "affiliation": "ITMO", "is_corresponding": True})]

    contributed = rels[("Person:Itmo", "Repository", "CONTRIBUTED_TO", "id")]
    assert contributed == [("p1", "r1", {"role": "maintainer"})]


def test_department_node_no_relationships():
    labels, (node_id, props) = extract_node(DEPARTMENT_ROW, NODE_REGISTRY["department"])
    assert labels == "Department"
    assert node_id == "d1"
    assert props == {"name_en": "ISU", "name_variants": ["ISU", "ИСиТ"]}
    assert extract_relationships(DEPARTMENT_ROW, NODE_REGISTRY["department"]) == {}


def test_repository_owned_by_is_scalar_and_matches_by_login():
    rels = extract_relationships(REPOSITORY_ROW, NODE_REGISTRY["repository"])
    owned_by = rels[("Repository", "GitHubProfile", "OWNED_BY", "login")]
    assert owned_by == [("r1", "someorg", {})]
    # свойств у OWNED_BY нет, других связей тут не должно быть (нет
    # department_ids/publication_ids в фикстуре)
    assert set(rels) == {("Repository", "GitHubProfile", "OWNED_BY", "login")}


def test_publication_mentions_links_split_by_discriminator():
    rels = extract_relationships(PUBLICATION_ROW, NODE_REGISTRY["publication"])

    to_repo = rels[("Publication", "Repository", "MENTIONS_LINK", "url")]
    assert to_repo == [("pub1", "https://github.com/x/repo", {"context": "see code"})]

    to_candidate = rels[("Publication", "LinkCandidate", "MENTIONS_LINK", "id")]
    assert to_candidate == [("pub1", "https://example.com/maybe-repo", {})]

    # каждый элемент mentions_links должен попасть ровно в один бакет
    total_edges = sum(len(v) for v in rels.values() if v and v[0][1] in
                       ("https://github.com/x/repo", "https://example.com/maybe-repo"))
    assert total_edges == 2


def run_all():
    tests = [v for k, v in globals().items() if k.startswith("test_") and callable(v)]
    for test in tests:
        test()
        print(f"ok  {test.__name__}")
    print(f"\n{len(tests)} проверок прошли")


if __name__ == "__main__":
    run_all()
