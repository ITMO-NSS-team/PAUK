import unittest

from pauk.graph.extract import NODE_REGISTRY, extract_node, extract_relationships

DEPARTMENT_WITH_SCHOOL = {"id": "d1", "name_en": "Faculty of Y", "school_id": "school_abc"}
SCHOOL_ROW = {"id": "school_abc", "name_en": "School of X", "name_ru": "Школа X"}


class SchoolHierarchyExtractTest(unittest.TestCase):
    def test_school_node(self):
        labels, (node_id, props) = extract_node(SCHOOL_ROW, NODE_REGISTRY["school"])
        self.assertEqual(labels, "School")
        self.assertEqual(node_id, "school_abc")
        self.assertEqual(props, {"name_en": "School of X", "name_ru": "Школа X"})

    def test_department_part_of_school_edge(self):
        _labels, (_id, props) = extract_node(DEPARTMENT_WITH_SCHOOL, NODE_REGISTRY["department"])
        self.assertEqual(props.get("school_id"), "school_abc")
        rels = extract_relationships(DEPARTMENT_WITH_SCHOOL, NODE_REGISTRY["department"])
        self.assertEqual(rels[("Department", "School", "PART_OF", "id")], [("d1", "school_abc", {})])

    def test_department_without_school_emits_no_edge(self):
        # school_id absent → scalar RelSpec skips it, so the old flat-department
        # behaviour is unchanged.
        rels = extract_relationships({"id": "d1", "name_en": "X"}, NODE_REGISTRY["department"])
        self.assertEqual(rels, {})


if __name__ == "__main__":
    unittest.main()
