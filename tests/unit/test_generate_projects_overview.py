import copy
import io
import json
import random
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts.generate_projects_overview import (
    END_MARKER,
    START_MARKER,
    collect_projects,
    main,
    render_overview,
    replace_projects_block,
)


def snapshot():
    return {
        "persons": [{"id": "person", "is_itmo": True}],
        "publications": [{"id": "publication", "title": "Study", "year": 2024, "doi": "10.1234/study"}],
        "repositories": [{"id": "repo", "name": "study", "url": "https://github.com/lab/study"}],
        "authorship": [{"pid": "publication", "per": "person"}],
        "repo_pubs": [{"rid": "repo", "pid": "publication"}],
        "repo_persons": [],
        "repo_depts": [],
        "departments": [],
    }


class CollectProjectsTest(unittest.TestCase):
    def projects(self, graph):
        projects, rejected = collect_projects(graph)
        self.assertEqual(rejected, 0)
        return projects

    def test_authored_itmo_project_and_minimal_markdown(self):
        self.assertEqual(
            render_overview(self.projects(snapshot())),
            "### 2024\n\n- [Study](https://github.com/lab/study) — **study**, "
            "[статья](https://doi.org/10.1234/study)\n",
        )

    def test_unknown_external_and_citation_only_do_not_qualify(self):
        for persons in (
            [],
            [{"id": "person"}],
            [{"id": "person", "is_itmo": False}],
            [{"id": "person", "is_itmo": "true"}],
        ):
            with self.subTest(persons=persons):
                graph = snapshot()
                graph["persons"] = persons
                self.assertEqual(self.projects(graph), [])
        graph = snapshot()
        graph["repo_pubs"] = []
        graph["mentions_repos"] = [{"rid": "repo", "pid": "publication", "is_relevant": True}]
        self.assertEqual(self.projects(graph), [])

    def test_standalone_owner_or_department_qualifies_but_contributor_does_not(self):
        graph = snapshot()
        graph["publications"] = []
        graph["repo_pubs"] = []
        graph["persons"].append({"id": "external", "is_itmo": False})
        graph["departments"] = [{"id": "department"}]
        graph["repositories"] = [
            {"id": key, "name": key, "url": f"https://github.com/lab/{key}"}
            for key in ("owner", "contributor", "department", "external", "missing")
        ]
        graph["repo_persons"] = [
            {"rid": "owner", "per": "person", "role": "owner"},
            {"rid": "contributor", "per": "person", "role": "contributor"},
            {"rid": "external", "per": "external", "role": "owner"},
            {"rid": "missing", "per": "unknown", "role": "owner"},
        ]
        graph["repo_depts"] = [
            {"rid": "department", "did": "department"},
            {"rid": "missing", "did": "unknown"},
        ]
        projects = self.projects(graph)
        self.assertEqual({project.name for project in projects}, {"owner", "department"})
        self.assertTrue(all(project.year is None for project in projects))
        self.assertIn("### Без года", render_overview(projects))

    def test_metadata_does_not_exclude_projects(self):
        graph = snapshot()
        graph["repositories"][0].update(
            {
                "license": None,
                "archived": True,
                "is_fork": True,
                "stars_num": 0,
                "owner": "not-an-itmo-organization",
                "topics": ["biology"],
                "language": None,
            }
        )
        self.assertEqual(len(self.projects(graph)), 1)

    def test_dangling_publication_and_repository_edges_are_ignored(self):
        graph = snapshot()
        graph["repo_pubs"].extend([{"rid": "repo", "pid": "missing"}, {"rid": "missing", "pid": "publication"}])
        graph["authorship"].append({"pid": "missing", "per": "person"})
        self.assertEqual(len(self.projects(graph)), 1)
        graph["publications"] = []
        self.assertEqual(self.projects(graph), [])

    def test_duplicate_urls_and_edges_merge_papers_and_use_first_year(self):
        graph = snapshot()
        graph["publications"].append({"id": "older", "title": "Earlier study", "year": 2020})
        graph["repositories"].extend(
            [
                {"id": "renamed", "url": "http://WWW.GITHUB.COM/LAB/STUDY.git", "name": "study"},
                {"id": "repo", "url": "https://github.com/lab/study/", "name": "study"},
            ]
        )
        graph["repo_pubs"].extend(
            [
                {"rid": "renamed", "pid": "older"},
                {"rid": "repo", "pid": "publication"},
            ]
        )
        [project] = self.projects(graph)
        self.assertEqual(project.year, 2020)
        self.assertEqual(project.title, "Earlier study")
        self.assertEqual(project.url, "https://github.com/lab/study")
        self.assertEqual(render_overview([project]).count("- ["), 1)

    def test_same_name_at_different_urls_is_not_deduplicated(self):
        graph = snapshot()
        graph["repositories"].append({"id": "other", "name": "study", "url": "https://github.com/other/study"})
        graph["repo_pubs"].append({"rid": "other", "pid": "publication"})
        self.assertEqual(len(self.projects(graph)), 2)

    def test_non_github_case_sensitive_paths_stay_distinct(self):
        graph = snapshot()
        graph["repositories"] = [
            {"id": "repo", "url": "https://example.org/Repo"},
            {"id": "other", "url": "https://example.org/repo"},
        ]
        graph["repo_pubs"].append({"rid": "other", "pid": "publication"})
        self.assertEqual(len(self.projects(graph)), 2)

    def test_year_validation_and_publication_date_fallback(self):
        for value, publication_date, expected in (
            (2021, None, 2021),
            ("2022", None, 2022),
            (True, None, None),
            (0, None, None),
            (-1, None, None),
            (10000, None, None),
            (2024.5, None, None),
            (None, "2018-04-02", 2018),
            (None, "2018-99-99", None),
            (None, "2018", None),
        ):
            with self.subTest(value=value, date=publication_date):
                graph = snapshot()
                graph["publications"][0].update({"year": value, "publication_date": publication_date})
                graph["repositories"][0]["created_at"] = "2026-01-01"
                [project] = self.projects(graph)
                self.assertEqual(project.year, expected)

    def test_earliest_titled_paper_is_used_even_if_first_year_has_no_title(self):
        graph = snapshot()
        graph["publications"].extend(
            [
                {"id": "untitled", "year": 2019},
                {"id": "titled", "year": 2020, "title": "First named study", "doi": "10.1234/first"},
            ]
        )
        graph["repo_pubs"].extend([{"rid": "repo", "pid": "untitled"}, {"rid": "repo", "pid": "titled"}])
        [project] = self.projects(graph)
        self.assertEqual(project.year, 2019)
        self.assertEqual(project.title, "First named study")
        self.assertEqual(project.paper_url, "https://doi.org/10.1234/first")

    def test_repository_name_and_description_fallback(self):
        graph = snapshot()
        graph["publications"][0].update({"title": None, "doi": None})
        graph["repositories"][0]["description"] = "  Description\nwith  spaces. "
        markdown = render_overview(self.projects(graph))
        self.assertIn("- [study](https://github.com/lab/study) — Description with spaces\\.", markdown)
        self.assertNotIn("[статья]", markdown)
        graph["repositories"][0].update({"name": None, "description": "a" * 300})
        [project] = self.projects(graph)
        self.assertEqual(project.name, "study")
        self.assertEqual(len(project.description), 160)
        self.assertTrue(project.description.endswith("…"))
        graph["repositories"][0]["description"] = None
        self.assertIn("- [study](https://github.com/lab/study)\n", render_overview(self.projects(graph)))

    def test_bibliographic_urls_and_optional_json_metadata(self):
        cases = [
            ({"doi": "https://dx.doi.org/10.1234/ABC(test)"}, "https://doi.org/10.1234/abc%28test%29"),
            ({"doi": "DOI:10.1234/ABC"}, "https://doi.org/10.1234/abc"),
            (
                {"identifiers": json.dumps([{"scheme": "doi", "value": "10.1234/from-id"}])},
                "https://doi.org/10.1234/from-id",
            ),
            (
                {
                    "source_records": json.dumps([{"source_url": "https://source.example/paper"}]),
                    "openalex_url": "https://openalex.org/W123",
                },
                "https://source.example/paper",
            ),
            (
                {"identifiers": [{"scheme": "other", "url": "https://source.example/identifier"}]},
                "https://source.example/identifier",
            ),
            (
                {"source_records": "invalid-json", "openalex_url": "https://openalex.org/W123"},
                "https://openalex.org/W123",
            ),
            ({"doi": "not-a-doi", "openalex_url": "javascript:bad"}, None),
            ({"source_records": [{"source_url": "https://private:secret@source.example/paper"}]}, None),
            ({}, None),
        ]
        for metadata, expected in cases:
            with self.subTest(metadata=metadata):
                graph = snapshot()
                graph["publications"][0].update({"id": "opaque-publication-id", "doi": None, **metadata})
                for table in ("authorship", "repo_pubs"):
                    graph[table][0]["pid"] = "opaque-publication-id"
                [project] = self.projects(graph)
                self.assertEqual(project.paper_url, expected)
                self.assertNotIn("opaque-publication-id", render_overview([project]))

    def test_markdown_html_and_url_delimiters_are_escaped(self):
        graph = snapshot()
        graph["publications"][0]["title"] = "Study [one]\n<script> *x*"
        graph["repositories"][0]["url"] = "https://github.com/lab/repo_(demo)"
        markdown = render_overview(self.projects(graph))
        self.assertIn("Study \\[one\\] &lt;script&gt; \\*x\\*", markdown)
        self.assertIn("https://github.com/lab/repo_%28demo%29", markdown)
        self.assertNotIn("<script>", markdown)
        self.assertEqual(markdown.count("\n- "), 1)

    def test_missing_and_unsafe_repository_urls_are_rejected(self):
        for url in (
            None,
            "",
            "javascript:alert(1)",
            "file:///tmp/repo",
            "https://u:p@github.com/repo",
            "https://github.com:bad/repo",
            "https://git hub.com/repo",
            "https://github.com/repo\nother",
        ):
            with self.subTest(url=url):
                graph = snapshot()
                graph["repositories"][0]["url"] = url
                projects, rejected = collect_projects(graph)
                self.assertEqual(projects, [])
                self.assertEqual(rejected, 1)

    def test_missing_or_malformed_tables_and_ids_fail_without_dumping_records(self):
        for table in snapshot():
            with self.subTest(table=table):
                graph = snapshot()
                del graph[table]
                with self.assertRaisesRegex(ValueError, table):
                    collect_projects(graph)
        for bad in (None, {}, ["PRIVATE-RECORD"], [{"id": "", "email": "PRIVATE-EMAIL"}]):
            with self.subTest(bad=bad):
                graph = snapshot()
                graph["persons"] = bad
                with self.assertRaises(ValueError) as caught:
                    collect_projects(graph)
                self.assertNotIn("PRIVATE", str(caught.exception))
        graph = snapshot()
        graph["publications"].append(dict(graph["publications"][0]))
        with self.assertRaisesRegex(ValueError, "repeats an entity ID"):
            collect_projects(graph)

    def test_private_fields_do_not_reach_markdown(self):
        graph = snapshot()
        secrets = ("PRIVATE-NAME", "private@example.org", "PRIVATE-AFFILIATION", "PRIVATE-AUTHOR-ID")
        graph["persons"][0].update(
            {
                "id": secrets[3],
                "name_ru": secrets[0],
                "email": secrets[1],
                "affiliations": [secrets[2]],
            }
        )
        graph["authorship"][0]["per"] = secrets[3]
        graph["publications"][0]["source_records"] = json.dumps([{"authors": [{"name": secrets[0]}]}])
        markdown = render_overview(self.projects(graph))
        for secret in secrets:
            self.assertNotIn(secret, markdown)
        self.assertNotIn("source_records", markdown)

    def test_result_is_invariant_to_table_row_order(self):
        graph = snapshot()
        graph["persons"].append({"id": "other-person", "is_itmo": False})
        graph["publications"].extend(
            [
                {"id": "earlier-a", "title": "A study", "year": 2020},
                {"id": "earlier-b", "title": "B study", "year": 2020},
            ]
        )
        graph["repositories"].extend(
            [
                {"id": "alias", "name": "study", "url": "https://github.com/LAB/STUDY.git"},
                {"id": "undated", "name": "standalone", "url": "https://github.com/lab/standalone"},
                {"id": "latest", "name": "latest", "url": "https://github.com/lab/latest"},
            ]
        )
        graph["repo_pubs"].extend(
            [
                {"rid": "alias", "pid": "earlier-b"},
                {"rid": "repo", "pid": "earlier-a"},
                {"rid": "latest", "pid": "publication"},
            ]
        )
        graph["repo_persons"].append({"rid": "undated", "per": "person", "role": "owner"})
        expected = render_overview(self.projects(graph))
        rng = random.Random(42)
        for _ in range(10):
            shuffled = copy.deepcopy(graph)
            for rows in shuffled.values():
                rng.shuffle(rows)
            self.assertEqual(render_overview(self.projects(shuffled)), expected)
        self.assertLess(expected.index("### 2024"), expected.index("### 2020"))
        self.assertLess(expected.index("### 2020"), expected.index("### Без года"))
        self.assertIn("[A study]", expected)

    def test_empty_complete_snapshot_is_valid(self):
        graph = {table: [] for table in snapshot()}
        self.assertEqual(render_overview(self.projects(graph)), "Проекты не найдены.\n")


class TemplateTest(unittest.TestCase):
    def test_preserves_surrounding_text_newlines_and_eof(self):
        for newline in ("\n", "\r\n"):
            for eof in ("", newline):
                with self.subTest(newline=newline, eof=eof):
                    prefix = "# Page" + newline * 2 + START_MARKER
                    suffix = END_MARKER + newline * 2 + "Footer" + eof
                    original = prefix + newline + "Old list" + newline + suffix
                    updated = replace_projects_block(original, "### 2024\n\n- Project\n")
                    expected = prefix + newline * 2 + f"### 2024{newline}{newline}- Project" + newline * 2 + suffix
                    self.assertEqual(updated, expected)
                    self.assertEqual(replace_projects_block(updated, "### 2024\n\n- Project\n"), expected)

    def test_missing_duplicate_inline_or_reversed_markers_are_invalid(self):
        for template in (
            "No markers",
            f"{START_MARKER}\n",
            f"{END_MARKER}\n{START_MARKER}\n",
            f"{START_MARKER}\n{START_MARKER}\n{END_MARKER}",
            f"{START_MARKER}\n{END_MARKER}\n{END_MARKER}",
            f"prefix {START_MARKER}\n{END_MARKER}",
            f"{START_MARKER}\n{END_MARKER} suffix",
        ):
            with self.subTest(template=template), self.assertRaises(ValueError):
                replace_projects_block(template, "New list\n")


class OverviewCliTest(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.cache = self.root / "snapshot.json"
        self.cache.write_text(json.dumps(snapshot()), encoding="utf-8")

    def run_main(self, args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(args)
        self.assertEqual(code, 0)
        return out.getvalue(), err.getvalue()

    def assert_failure(self, args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err), self.assertRaises(SystemExit) as caught:
            main(args)
        self.assertEqual(caught.exception.code, 2)
        self.assertEqual(out.getvalue(), "")
        return err.getvalue()

    def test_stdout_and_output_file_modes(self):
        stdout, stderr = self.run_main(["--cache", str(self.cache)])
        self.assertTrue(stdout.startswith("### 2024\n"))
        self.assertNotIn("Snapshot:", stdout)
        self.assertIn("projects: 1", stderr)
        output = self.root / "nested" / "projects.md"
        stdout2, _ = self.run_main(["--cache", str(self.cache), "--output", str(output)])
        self.assertEqual(stdout2, "")
        self.assertEqual(output.read_text(encoding="utf-8"), stdout)

    def test_default_uses_latest_snapshot_filename_not_modification_time(self):
        older = self.root / "graph_snapshot_31-12-2025.json"
        newer = self.root / "graph_snapshot_01-01-2026.json"
        newer.write_bytes(self.cache.read_bytes())
        older.write_text(json.dumps({table: [] for table in snapshot()}), encoding="utf-8")
        with patch("scripts.generate_projects_overview.settings", SimpleNamespace(cache_dir=self.root)):
            stdout, stderr = self.run_main([])
        self.assertIn("[Study]", stdout)
        self.assertIn(newer.name, stderr)

    def test_template_updates_in_place_and_second_run_does_not_rewrite(self):
        target = self.root / "page.md"
        prefix = f"# Page\r\n\r\n{START_MARKER}"
        suffix = f"{END_MARKER}\r\n\r\nFooter"
        target.write_bytes((prefix + "\r\nOld\r\n" + suffix).encode())
        args = ["--cache", str(self.cache), "--template", str(target)]
        stdout, _ = self.run_main(args)
        self.assertEqual(stdout, "")
        updated = target.read_bytes().decode()
        self.assertTrue(updated.startswith(prefix + "\r\n\r\n"))
        self.assertTrue(updated.endswith(suffix))
        self.assertIn("[Study]", updated)
        with patch("scripts.generate_projects_overview.AtomicWriter") as writer:
            self.run_main(args)
        writer.assert_not_called()
        self.assertEqual(target.read_bytes().decode(), updated)

    def test_invalid_template_and_snapshot_leave_target_unchanged(self):
        target = self.root / "page.md"
        target.write_bytes(b"Original page without markers")
        before = target.read_bytes()
        self.assert_failure(["--cache", str(self.cache), "--template", str(target)])
        self.assertEqual(target.read_bytes(), before)
        for invalid in ("not-json", "[]", json.dumps({"repositories": []})):
            self.cache.write_text(invalid, encoding="utf-8")
            self.assert_failure(["--cache", str(self.cache), "--output", str(target)])
            self.assertEqual(target.read_bytes(), before)

    def test_output_and_template_conflict_and_snapshot_cannot_be_overwritten(self):
        before = self.cache.read_bytes()
        self.assert_failure(["--cache", str(self.cache), "--output", "out.md", "--template", "template.md"])
        for flag in ("--output", "--template"):
            self.assert_failure(["--cache", str(self.cache), flag, str(self.cache)])
        self.assertEqual(self.cache.read_bytes(), before)
        alias = self.root / "alias.md"
        alias.symlink_to(self.cache)
        self.assert_failure(["--cache", str(self.cache), "--output", str(alias)])
        self.assertEqual(self.cache.read_bytes(), before)

    def test_missing_snapshot_and_template_are_errors_without_creating_files(self):
        self.assertIn("No such file", self.assert_failure(["--cache", str(self.root / "missing.json")]))
        target = self.root / "missing.md"
        self.assert_failure(["--cache", str(self.cache), "--template", str(target)])
        self.assertFalse(target.exists())
        with (
            tempfile.TemporaryDirectory() as empty,
            patch(
                "scripts.generate_projects_overview.settings",
                SimpleNamespace(cache_dir=Path(empty)),
            ),
        ):
            self.assertIn("pauk cache export", self.assert_failure([]))

    def test_shipped_template_can_be_updated(self):
        shipped = Path(__file__).resolve().parents[2] / "scripts/templates/projects_overview.md"
        target = self.root / "page.md"
        target.write_bytes(shipped.read_bytes())
        before = target.read_text(encoding="utf-8")
        self.run_main(["--cache", str(self.cache), "--template", str(target)])
        after = target.read_text(encoding="utf-8")
        self.assertEqual(before.split(START_MARKER)[0], after.split(START_MARKER)[0])
        self.assertEqual(before.split(END_MARKER)[1], after.split(END_MARKER)[1])
        self.assertIn("[Study]", after)
        self.assertEqual(after.count(START_MARKER), 1)


if __name__ == "__main__":
    unittest.main()
