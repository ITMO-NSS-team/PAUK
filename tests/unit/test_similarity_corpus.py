import base64
import json
import unittest
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from pauk.cli import _build_parser, _dispatch
from pauk.models import Repository
from pauk.pipeline.stages.repositories import RepositoriesStage
from pauk.settings import Settings
from pauk.similarity.corpus import collect
from pauk.similarity.documents import prepare_document
from pauk.sources.base import HttpRequestError
from pauk.sources.github import MAX_README_BYTES, GitHubClient, Readme
from pauk.storage.readme import cached_readme


def contents(body=b"Converts documents to text.", path=".github/README.rst"):
    return {"path": path, "sha": "a" * 40, "size": len(body),
            "content": base64.encodebytes(body).decode(), "encoding": "base64"}


class GitHubReadmeTests(unittest.TestCase):
    def setUp(self):
        self.client = GitHubClient(5)
        self.addCleanup(self.client.session.close)
        self.client.get_json = Mock()

    def test_enrichment_caches_the_same_response_under_canonical_url(self):
        with TemporaryDirectory() as folder:
            settings = Settings(data_dir=Path(folder))
            stage = RepositoriesStage(Mock(), Mock(), config=settings)
            url = "https://github.com/lab/new"
            repo = Repository(id="old", name="old", url="https://github.com/lab/old")
            self.client.get_json.side_effect = [{"html_url": url, "name": "new"}, contents()]
            availability, _ = stage._enrich_repository(self.client, repo, "lab", "old", repo.url, {}, None, set())
            self.assertEqual(availability, "available")
            self.assertTrue(repo.has_readme)
            self.assertEqual(self.client.get_json.call_count, 2)
            readme = cached_readme(settings.cache_dir / "readmes", url)
            self.assertEqual(readme.body, b"Converts documents to text.")
            self.assertEqual(readme.path, ".github/README.rst")
            snapshot = Path(folder) / "snapshot.json"
            snapshot.write_text(json.dumps({"repositories": [{"id": "new", "url": url}]}), encoding="utf-8")
            results = collect(snapshot, Path(folder) / "out", settings.cache_dir / "readmes", self.client)
            self.assertEqual(results["prepared"], 1)
            self.assertEqual(self.client.get_json.call_count, 2)

    def test_exists_even_when_empty_omitted_invalid_or_too_large(self):
        for payload, status in [(contents(b""), "empty"), (dict(contents(), encoding="none"), "unavailable"),
                                (dict(contents(), content="!"), "invalid"),
                                (dict(contents(), size=MAX_README_BYTES + 1), "too_large")]:
            with self.subTest(status=status):
                self.client.get_json.return_value = payload
                save = Mock()
                self.assertTrue(self.client.has_readme("lab", "repo", on_result=save))
                self.assertEqual(save.call_args.args[0].status, status)

    def test_only_readme_404_means_missing(self):
        for code in (404, 403, 429, 502):
            with self.subTest(code=code):
                self.client.get_json.side_effect = HttpRequestError("GET", "https://api.github.com", status_code=code)
                if code == 404:
                    self.assertFalse(self.client.has_readme("lab", "repo"))
                else:
                    with self.assertRaises(HttpRequestError):
                        self.client.has_readme("lab", "repo")

    def test_cache_error_preserves_presence(self):
        self.client.get_json.return_value = contents()
        with self.assertLogs("pauk.sources.github", level="WARNING"):
            self.assertTrue(self.client.has_readme("lab", "repo", on_result=Mock(side_effect=OSError("full"))))
        self.client.get_json.assert_called_once()

    def test_only_explicit_backfill_fetches_missing_inline_bytes(self):
        inline = dict(contents(), content="", encoding="none")
        self.client.get_json.side_effect = [inline, contents()]
        result = self.client.get_readme("lab", "repo", allow_blob=True)
        self.assertEqual(result.body, b"Converts documents to text.")
        self.assertEqual(self.client.get_json.call_args.args[0], "https://api.github.com/repos/lab/repo/git/blobs/" + inline["sha"])

    def test_blob_metadata_mismatch_does_not_label_other_bytes_with_requested_sha(self):
        inline = dict(contents(), content="", encoding="none")
        self.client.get_json.side_effect = [inline, dict(contents(), sha="b" * 40)]
        self.assertEqual(self.client.get_readme("lab", "repo", allow_blob=True).status, "invalid")


class DocumentTests(unittest.TestCase):
    def test_formats_keep_function_and_remove_boilerplate_with_provenance(self):
        feature = "Преобразует таблицы в текст."
        fixtures = [
            ("README.md", ["[![Code coverage](https://img.shields.io/x)](https://codecov.io/x)",
                           feature, "# Installation", "pip install project", "~~~shell", "echo Hello world", "~~~",
                           "## Citation", "irrelevant journal title"]),
            ("README.rst", ["|docs| |build|", feature, "Contacts", "========", "Someone Smith, Team lead",
                            ".. |docs| image:: https://example.org/badge", "    :alt: Documentation Status"]),
            ("README.adoc", ["= Tool", "Alice Smith <alice@example.org>", "ifdef::env-github[]", feature,
                             "image::logo.png[Project Logo]", "[source,sh]", "----", "echo Hello world", "----"]),
            ("README", ["Please cite the following paper:", "@article{a,", " author={Someone Smith}}", feature]),
        ]
        for path, lines in fixtures:
            with self.subTest(path=path):
                parts, warning = prepare_document("", path, "\r\n".join(lines).encode())
                self.assertIsNone(warning)
                self.assertEqual([(f.text, f.line) for f in parts], [(feature, lines.index(feature) + 1)])

    def test_short_description_handles_absent_and_non_text_readmes(self):
        for body, warning in [(b"", None), (b"\xff", "non_utf8_readme"), (b"a\0b", "binary_readme")]:
            parts, actual = prepare_document("Extracts PDF tables", "", body)
            self.assertEqual(actual, warning)
            self.assertEqual([(part.source, part.text) for part in parts], [("description", "Extracts PDF tables")])
        self.assertEqual(prepare_document("Python library", "", b"")[0], [])

    def test_live_readme_patterns(self):
        # Small excerpts from the corpus smoke check capture each project's markup.
        fixtures = [
            ("README", "ZLIB DATA COMPRESSION LIBRARY", "ZLIB DATA COMPRESSION LIBRARY"),
            ("README.md", "# pdfplumber\nPlus: Table extraction and visual debugging.",
             "Table extraction and visual debugging"),
            (".github/README.rst", "|docs| |build|\n**Flexibility.** FEDOT can be used to automate\nContacts\n========\nSomeone, Team lead",
             "Flexibility. FEDOT can be used to automate"),
            ("README.adoc", "= Asciidoctor\nifndef::env-github[:icons: font]\n{url-project}[Asciidoctor] is a fast, open source, Ruby-based text processor",
             "Asciidoctor is a fast, open source, Ruby-based text processor"),
        ]
        for path, excerpt, expected in fixtures:
            with self.subTest(path=path):
                parts, _ = prepare_document("", path, excerpt.encode())
                self.assertEqual(len(parts), 1)
                self.assertIn(expected, parts[0].text)

    def test_citation_paragraph_and_inline_comment(self):
        text = "Please cite this paper:\nSome Journal of Technology, 2020\n\nMeasures snow <!-- note --> depth."
        parts, _ = prepare_document("", "README.md", text.encode())
        self.assertEqual([part.text for part in parts], ["Measures snow depth."])


class CorpusTests(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.snapshot = self.root / "snapshot.json"
        self.out = self.root / "out"
        self.cache = self.root / "cache" / "readmes"
        self.client = Mock()

    def snapshot_rows(self, rows):
        self.snapshot.write_text(json.dumps({"repositories": rows}), encoding="utf-8")

    def build(self, **kwargs):
        return collect(self.snapshot, self.out, self.cache, self.client, **kwargs)

    def test_cli_old_snapshot_uses_body_despite_stale_flag_and_round_trips_bytes(self):
        self.snapshot_rows([{"id": "../opaque", "url": "https://github.com/Org/App.git", "has_readme": False}])
        body = b"Detects ice concentration.\r\n"
        self.client.get_readme.return_value = Readme(status="available", path="README.md", body=body)
        parser = _build_parser()
        args = parser.parse_args(["similarity", "collect", "--snapshot", str(self.snapshot), "--out", str(self.out)])
        with patch("pauk.cli.settings", Settings(data_dir=self.root)), patch("pauk.sources.github.GitHubClient", return_value=self.client):
            _dispatch(args, parser)
        self.client.get_readme.assert_called_once_with("org", "app", allow_blob=True)
        record = json.loads((self.out / "manifest.json").read_bytes())["repositories"][0]
        self.assertEqual(record["id"], "../opaque")
        self.assertEqual((self.out / record["readme"]["blob"]).read_bytes(), body)
        self.assertEqual(record["text_sha256"], sha256(record["text"].encode()).hexdigest())
        self.assertEqual(record["fragments"][0]["line"], 1)
        self.build()
        self.client.get_readme.assert_called_once()

    def test_interrupted_run_resumes_and_refresh_preserves_previous_version(self):
        self.snapshot_rows([{"id": i, "url": f"https://github.com/org/{i}"} for i in "ab"])
        missing = Readme(status="missing")
        self.client.get_readme.side_effect = [missing, KeyboardInterrupt()]
        with self.assertRaises(KeyboardInterrupt):
            self.build()
        self.assertFalse((self.out / "manifest.json").exists())
        self.client.get_readme.side_effect = [missing]
        version = self.build()["version"]
        saved = self.out / "versions" / f"{version}.json"
        first = saved.read_bytes()
        self.client.get_readme.side_effect = [missing, Readme(status="available", body=b"Extracts text from PDFs")]
        self.assertNotEqual(version, self.build(refresh=True)["version"])
        self.assertEqual(saved.read_bytes(), first)

    def test_transient_failure_retried_and_missing_url_uses_description(self):
        self.snapshot_rows([{"id": "a", "url": "https://github.com/org/a"},
                            {"id": "b", "description": "Converts printed text"}])
        self.client.get_readme.side_effect = [HttpRequestError("GET", "url", status_code=503), Readme(status="empty")]
        first = self.build()
        self.assertEqual((first["prepared"], first["http_503"], first["unsupported_url"]), (1, 1, 1))
        self.assertEqual(self.build()["empty"], 1)

    def test_conflicts_rejected_before_network(self):
        a = {"id": "a", "url": "https://github.com/org/a"}
        for b in (a, dict(a, id="b", url="https://www.github.com/Org/A.git")):
            self.snapshot_rows([a, b])
            with self.assertRaises(ValueError):
                self.build()
        self.client.get_readme.assert_not_called()
