import unittest

from pauk.graph.extract import NODE_REGISTRY, extract_node, extract_relationships

PERSON_ROW = {
    "id": "p1",
    "name_en": "Ivan Petrov",
    "email": "ivan@itmo.ru",
    "department_ids": ["d1", "d2"],
    "authored": [
        {
            "publication_id": "pub1",
            "position": 1,
            "affiliation": "ITMO",
            "is_corresponding": True,
        }
    ],
    "contributed_to": [{"repository_id": "r1", "role": "maintainer"}],
    "orcid": "0000-0000",
    "affiliation": "some free-text guess",
    "email_candidates": [["a@b.com", "page1"]],
    "profile": {"login": "ivanp"},
}

DEPARTMENT_ROW = {"id": "d1", "name_en": "ISU", "name_variants": ["ISU", "ICST"]}

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
        {
            "target_kind": "repository",
            "repository_url": "https://github.com/x/repo",
            "context": "see code",
        },
        {"target_kind": "candidate", "candidate_id": "https://example.com/maybe-repo"},
    ],
}


class ExtractNodeTest(unittest.TestCase):
    def test_person_node_drops_stray_fields(self):
        labels, (node_id, props) = extract_node(PERSON_ROW, NODE_REGISTRY["itmo_person"])
        self.assertEqual(labels, "Person:Itmo")
        self.assertEqual(node_id, "p1")
        self.assertEqual(props.get("name_en"), "Ivan Petrov")
        self.assertEqual(props.get("email"), "ivan@itmo.ru")
        self.assertEqual(props.get("orcid"), "0000-0000")
        for stray in (
            "affiliation",
            "email_candidates",
            "profile",
            "department_ids",
            "authored",
            "contributed_to",
            "id",
        ):
            self.assertNotIn(stray, props, f"{stray} leaked into node properties")

    def test_department_node_has_no_relationships(self):
        labels, (node_id, props) = extract_node(DEPARTMENT_ROW, NODE_REGISTRY["department"])
        self.assertEqual(labels, "Department")
        self.assertEqual(node_id, "d1")
        self.assertEqual(props, {"name_en": "ISU", "name_variants": ["ISU", "ICST"]})
        self.assertEqual(extract_relationships(DEPARTMENT_ROW, NODE_REGISTRY["department"]), {})


class ExtractRelationshipsTest(unittest.TestCase):
    def test_person_relationships(self):
        rels = extract_relationships(PERSON_ROW, NODE_REGISTRY["itmo_person"])

        belongs = rels[("Person:Itmo", "Department", "BELONGS_TO", "id")]
        self.assertEqual(sorted(belongs), [("p1", "d1", {}), ("p1", "d2", {})])

        authored = rels[("Person:Itmo", "Publication", "AUTHORED", "id")]
        self.assertEqual(
            authored,
            [
                (
                    "p1",
                    "pub1",
                    {"position": 1, "affiliation": "ITMO", "is_corresponding": True},
                )
            ],
        )

        contributed = rels[("Person:Itmo", "Repository", "CONTRIBUTED_TO", "id")]
        self.assertEqual(contributed, [("p1", "r1", {"role": "maintainer"})])

    def test_repository_owned_by_is_scalar_and_matches_by_login(self):
        rels = extract_relationships(REPOSITORY_ROW, NODE_REGISTRY["repository"])
        owned_by = rels[("Repository", "GitHubProfile", "OWNED_BY", "login")]
        self.assertEqual(owned_by, [("r1", "someorg", {})])
        self.assertEqual(set(rels), {("Repository", "GitHubProfile", "OWNED_BY", "login")})

    def test_publication_mentions_links_split_by_discriminator(self):
        rels = extract_relationships(PUBLICATION_ROW, NODE_REGISTRY["publication"])

        to_repo = rels[("Publication", "Repository", "MENTIONS_LINK", "url")]
        self.assertEqual(to_repo, [("pub1", "https://github.com/x/repo", {"context": "see code"})])

        to_candidate = rels[("Publication", "LinkCandidate", "MENTIONS_LINK", "id")]
        self.assertEqual(to_candidate, [("pub1", "https://example.com/maybe-repo", {})])

        self.assertEqual(len(to_repo) + len(to_candidate), len(PUBLICATION_ROW["mentions_links"]))


if __name__ == "__main__":
    unittest.main()
