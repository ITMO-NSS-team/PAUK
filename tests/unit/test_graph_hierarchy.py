import unittest

from pauk.graph.extract import NODE_REGISTRY, extract_node, extract_relationships

SUBUNIT = {"id": "d1", "name_en": "Faculty of Y", "kind": "faculty", "parent_id": "d_school"}
TOP_UNIT = {"id": "d_school", "name_en": "School of X", "kind": "megafaculty", "organization_id": "org_itmo"}
ORG = {
    "id": "org_itmo",
    "name_en": "ITMO University",
    "name_ru": "Университет ИТМО",
    "country": "Russia",
    "type": "university",
}


class HierarchyExtractTest(unittest.TestCase):
    def test_subunit_is_part_of_parent_department(self):
        rels = extract_relationships(SUBUNIT, NODE_REGISTRY["department"])
        self.assertEqual(rels[("Department", "Department", "PART_OF", "id")], [("d1", "d_school", {})])
        # No organisation edge when organization_id is absent.
        self.assertNotIn(("Department", "Organization", "PART_OF", "id"), rels)

    def test_top_unit_is_part_of_organization(self):
        rels = extract_relationships(TOP_UNIT, NODE_REGISTRY["department"])
        self.assertEqual(rels[("Department", "Organization", "PART_OF", "id")], [("d_school", "org_itmo", {})])
        self.assertNotIn(("Department", "Department", "PART_OF", "id"), rels)

    def test_organization_node(self):
        labels, (node_id, props) = extract_node(ORG, NODE_REGISTRY["organization"])
        self.assertEqual(labels, "Organization")
        self.assertEqual(node_id, "org_itmo")
        self.assertEqual(props.get("country"), "Russia")
        self.assertEqual(props.get("type"), "university")
        # An Organization is a root: it carries no PART_OF edge of its own.
        self.assertEqual(extract_relationships(ORG, NODE_REGISTRY["organization"]), {})

    def test_department_props_include_kind_and_parent(self):
        _labels, (_id, props) = extract_node(SUBUNIT, NODE_REGISTRY["department"])
        self.assertEqual(props.get("kind"), "faculty")
        self.assertEqual(props.get("parent_id"), "d_school")


if __name__ == "__main__":
    unittest.main()
