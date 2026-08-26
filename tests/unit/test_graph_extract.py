import unittest

from pauk.graph.extract import NODE_REGISTRY, extract_node, extract_relationships
from pauk.models.department import Department
from pauk.models.person import Person

PERSON_ROW = {
    "id": "p1",
    "name_raw": "Ivan Petrov",
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
        self.assertEqual(props.get("name_raw"), "Ivan Petrov")
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

    def test_github_profile_publishes_the_employer_but_not_the_matching_data(self):
        # company sits beside location and description as something the
        # account states about itself. The emails and commit names behind
        # it are evidence the matcher weighs, not facts about the account,
        # and they are addresses of living people — they stay out of the
        # graph.
        labels, (node_id, props) = extract_node({
            "id": "github_ipetrov", "login": "ipetrov", "name": "Ivan Petrov",
            "company": "ITMO University", "location": "Saint-Petersburg",
            "type": "user", "emails": ["ivan@itmo.ru"],
            "commit_names": ["Ivan Petrov"], "repos": ["https://github.com/x/repo"],
        }, NODE_REGISTRY["github_profile"])
        self.assertEqual(labels, "GitHubProfile")
        self.assertEqual(node_id, "github_ipetrov")
        self.assertEqual(props.get("company"), "ITMO University")
        for stray in ("emails", "commit_names", "repos", "id"):
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

        # Person relationships match their source by the base :Person label:
        # the Itmo/External label can be upgraded by a later group, while
        # relationships published from any group must still resolve.
        belongs = rels[("Person", "Department", "BELONGS_TO", "id")]
        self.assertEqual(sorted(belongs), [("p1", "d1", {}), ("p1", "d2", {})])

        authored = rels[("Person", "Publication", "AUTHORED", "id")]
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

        contributed = rels[("Person", "Repository", "CONTRIBUTED_TO", "id")]
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


class GraphFieldCoverageTest(unittest.TestCase):
    """Every model field either reaches Neo4j or is excluded on purpose.

    Nothing forces a new Person/Department field into prop_fields - it is
    easy to add a field the pipeline computes and never notice the graph
    (and everything downstream of it: cache/export.py, the GUI) never sees
    it. This test is the safety net: a field must be in prop_fields
    somewhere, or listed below with a reason it deliberately isn't.
    """

    PERSON_EXCLUDED = {
        "id": "used as the node id, not a prop",
        "is_itmo": "determines the Person:Itmo/Person:External label, not a prop",
        "department_ids": "published as a BELONGS_TO relationship",
        "authored": "published as an AUTHORED relationship",
        "contributed_to": "published as a CONTRIBUTED_TO relationship",
        "processing": "per-stage pipeline bookkeeping, never meant for the graph",
    }
    DEPARTMENT_EXCLUDED = {
        "id": "used as the node id, not a prop",
        "parent_id": "published as a PART_OF relationship",
        "organization_id": "published as a PART_OF relationship",
    }

    def test_every_person_field_reaches_the_graph_or_is_excluded_with_a_reason(self):
        covered = set(NODE_REGISTRY["itmo_person"].prop_fields) | set(
            NODE_REGISTRY["external_person"].prop_fields
        )
        for field in Person.model_fields:
            if field in self.PERSON_EXCLUDED:
                continue
            self.assertIn(
                field, covered,
                f"Person.{field} reaches no graph node's prop_fields and has no exclusion "
                "reason - add it to itmo_person/external_person prop_fields or to "
                "PERSON_EXCLUDED above",
            )

    def test_every_department_field_reaches_the_graph_or_is_excluded_with_a_reason(self):
        covered = set(NODE_REGISTRY["department"].prop_fields)
        for field in Department.model_fields:
            if field in self.DEPARTMENT_EXCLUDED:
                continue
            self.assertIn(
                field, covered,
                f"Department.{field} reaches no graph node's prop_fields and has no "
                "exclusion reason - add it to department prop_fields or to "
                "DEPARTMENT_EXCLUDED above",
            )


if __name__ == "__main__":
    unittest.main()
