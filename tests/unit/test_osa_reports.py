"""`pauk reports build`: one OSA run per repository, atomic PDFs, the site index, the CLI."""

import contextlib
import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from pauk.cache.graph_snapshot import write_snapshot
from pauk.cli import _build_parser, _cmd_reports
from pauk.reports import builder, osa
from pauk.settings import Settings
from pauk.sources.base import HttpRequestError

# Stands in for OSA: prints what OSA logs and writes what a real run leaves
# in its working directory, by mode.
FAKE_OSA = """
import os, signal, subprocess, sys, time
mode = sys.argv[1]
if mode == "stubborn":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    open(sys.argv[2], "w").write(str(child.pid))
    time.sleep(60)
git = {os.environ[f"GIT_CONFIG_KEY_{i}"]: os.environ[f"GIT_CONFIG_VALUE_{i}"]
       for i in range(int(os.environ.get("GIT_CONFIG_COUNT", 0)))}
print("key=" + os.environ.get("LLM_SERVICE_KEY", ""), "lfs=" + git.get("filter.lfs.smudge", ""))
if mode == "hang":
    time.sleep(30)
if mode == "fallback":
    print("WARNING Model 'anthropic/claude-haiku-5.5' failed. Falling back to model 'google/gemini-3-flash-preview'")
if mode == "unparsed":
    print("WARNING Parsing failed, fallback applied: 1 validation error")
if mode in ("ok", "crash", "fallback", "unparsed"):
    open("Repo_report.pdf", "wb").write(b"%PDF-1.4 report")
if mode == "garbage":
    open("Repo_report.pdf", "wb").write(b"<html>rate limited</html>")
if mode == "two":
    open("A_report.pdf", "wb").write(b"%PDF-1.4 a")
    open("B_report.pdf", "wb").write(b"%PDF-1.4 b")
open("Repo_work_summary.pdf", "wb").write(b"%PDF-1.4 summary")
sys.exit(1 if mode == "crash" else 0)
"""


class FakeOsaTestCase(unittest.TestCase):
    """A temp dir and `OsaRunner.command` pointed at FAKE_OSA."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        script = self.root / "fake_osa.py"
        script.write_text(FAKE_OSA)
        self.mode = "ok"
        patcher = patch.object(
            osa.OsaRunner, "command", staticmethod(lambda url, language: [sys.executable, str(script), self.mode, str(self.child_pid_file)])
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.osa_dir = self.root / "osa"
        self.child_pid_file = self.root / "child.pid"
        self.out_dir = self.root / "reports"


class GitConfigEnvTest(unittest.TestCase):
    def test_numbers_every_pair_for_git(self):
        self.assertEqual(
            osa.git_config_env({"a.b": "1", "c.d": ""}),
            {"GIT_CONFIG_COUNT": "2", "GIT_CONFIG_KEY_0": "a.b", "GIT_CONFIG_VALUE_0": "1",
             "GIT_CONFIG_KEY_1": "c.d", "GIT_CONFIG_VALUE_1": ""},
        )

    def test_no_lfs_config_replaces_the_filter_with_a_plain_copy(self):
        self.assertEqual(osa.NO_LFS_GIT_CONFIG["filter.lfs.process"], "")
        self.assertEqual(osa.NO_LFS_GIT_CONFIG["filter.lfs.smudge"], "cat")
        self.assertEqual(osa.NO_LFS_GIT_CONFIG["filter.lfs.required"], "false")


class OsaCommandTest(unittest.TestCase):
    def test_report_only_through_openrouter_with_the_pinned_tool_model_and_window(self):
        command = osa.OsaRunner.command("https://github.com/a/one", "English")
        self.assertEqual(command[:4], ["uvx", "--from", osa.OSA_PACKAGE, "osa-tool"])
        self.assertTrue(osa.OSA_PACKAGE.startswith("osa_tool=="))
        for flag in ("--report", "--web-mode", "--no-fork", "--no-pull-request", "--delete-dir"):
            self.assertIn(flag, command)
        self.assertEqual(command[command.index("-r") + 1], "https://github.com/a/one")
        self.assertEqual(command[command.index("--mode") + 1], "advanced")
        self.assertEqual(command[command.index("--api") + 1], "openai")
        self.assertEqual(command[command.index("--model") + 1], osa.MODEL)
        self.assertEqual(command[command.index("--context-window") + 1], str(osa.CONTEXT_WINDOW))
        self.assertEqual(command[command.index("--artefacts-language") + 1], "English")

    def test_context_window_stays_under_the_haiku_price_step(self):
        self.assertLess(osa.CONTEXT_WINDOW, 100_000)


class OsaRunnerTest(FakeOsaTestCase):
    def run_osa(self):
        runner = osa.OsaRunner("secret", self.osa_dir)
        return runner.run("github_a_one", "https://github.com/a/one", "en", "English", self.out_dir / "en" / "a.pdf")

    def test_a_successful_run_moves_only_the_report_and_cleans_up(self):
        self.assertTrue(self.run_osa())
        self.assertEqual((self.out_dir / "en" / "a.pdf").read_bytes(), b"%PDF-1.4 report")
        self.assertEqual(list(self.out_dir.rglob("*summary*")), [])
        self.assertFalse((self.osa_dir / "tmp" / "en" / "github_a_one").exists())

    def test_osa_gets_the_key_lfs_off_and_a_wide_console_and_its_output_is_logged(self):
        self.assertEqual(osa.OsaRunner("secret", self.osa_dir).env["COLUMNS"], "400")
        self.run_osa()
        log = (self.osa_dir / "logs" / "en" / "github_a_one.log").read_text()
        self.assertIn("key=secret lfs=cat", log)

    def test_failed_empty_doubled_non_pdf_or_degraded_runs_leave_nothing(self):
        for self.mode in ("crash", "nothing", "two", "garbage", "fallback", "unparsed"):
            with self.subTest(self.mode), self.assertLogs("pauk.reports.osa", "WARNING"):
                self.assertFalse(self.run_osa())
            self.assertFalse(self.out_dir.exists())

    def test_degraded_runs_are_named_in_the_warning(self):
        self.mode = "fallback"
        with self.assertLogs("pauk.reports.osa", "WARNING") as logs:
            self.run_osa()
        self.assertIn(osa.DEGRADED_RUN_MARKERS[0], logs.output[0])

    def test_a_hung_run_is_stopped_at_the_timeout_and_reported_before_the_stop(self):
        self.mode = "hang"
        terminate = osa.OsaRunner._terminate

        def reported_first(proc):
            self.assertTrue(logs.output, "the timeout must be logged before waiting on the stop")
            terminate(proc)

        with patch.object(osa, "RUN_TIMEOUT", 1), patch.object(osa.OsaRunner, "_terminate", staticmethod(reported_first)), \
                self.assertLogs("pauk.reports.osa", "WARNING") as logs:
            self.assertFalse(self.run_osa())
        self.assertIn("timed out", logs.output[0])

    def test_a_run_ignoring_sigterm_is_killed_with_its_children_after_the_grace(self):
        self.mode = "stubborn"
        started = time.monotonic()
        with patch.object(osa, "RUN_TIMEOUT", 1), patch.object(osa, "STOP_GRACE", 1), \
                self.assertLogs("pauk.reports.osa", "WARNING"):
            self.assertFalse(self.run_osa())
        self.assertLess(time.monotonic() - started, 10)
        child = int(self.child_pid_file.read_text())
        for _ in range(50):
            try:
                os.kill(child, 0)
            except ProcessLookupError:
                break
            time.sleep(0.1)
        else:
            self.fail("the OSA child process survived the stop")

    def test_stop_ends_runs_in_flight_and_refuses_new_ones(self):
        self.mode = "hang"
        runner = osa.OsaRunner("secret", self.osa_dir)
        results = []
        worker = threading.Thread(target=lambda: results.append(
            runner.run("github_a_one", "u", "en", "English", self.out_dir / "en" / "a.pdf")
        ))
        worker.start()
        while not runner._running:
            time.sleep(0.05)
        runner.stop()
        worker.join(timeout=10)
        self.assertEqual(results, [False])
        self.assertFalse(runner.run("github_b_two", "u", "en", "English", self.out_dir / "en" / "b.pdf"))
        self.assertFalse((self.osa_dir / "logs" / "en" / "github_b_two.log").exists())

    def test_the_timeout_leaves_room_for_the_slowest_real_run(self):
        self.assertGreater(osa.RUN_TIMEOUT, 13 * 60)


class IndexTest(unittest.TestCase):
    def test_report_path_is_language_then_repository_id(self):
        self.assertEqual(builder.report_path(Path("r"), "en", "github_a_b"), Path("r/en/github_a_b.pdf"))

    def test_index_lists_language_folders_and_ignores_loose_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            for name in ("en/b.pdf", "en/a.pdf", "ru/a.pdf", "old_report.pdf", "en/notes.txt"):
                (out / name).parent.mkdir(exist_ok=True)
                (out / name).write_bytes(b"%PDF")
            index = builder.write_index(out)
            self.assertEqual(index, {"en": ["a", "b"], "ru": ["a"]})
            self.assertEqual(json.loads((out / "index.json").read_text()), index)

    def test_every_site_language_maps_to_an_osa_locale(self):
        self.assertEqual(builder.LANGUAGES, {"en": "English", "ru": "Russian"})


class ThirdPartyReposTest(unittest.TestCase):
    def test_only_repositories_judged_foreign_everywhere_and_without_itmo_people(self):
        graph = {
            "repo_pubs": [{"rid": "implements", "pid": "P1"}],
            "repo_persons": [{"rid": "itmo_contributor", "per": "A1", "role": "owner"}],
            "mentions_repos": [
                {"rid": "implements", "pid": "P2", "is_relevant": False},
                {"rid": "foreign", "pid": "P1", "is_relevant": False},
                {"rid": "foreign", "pid": "P2", "is_relevant": False},
                {"rid": "undecided", "pid": "P1", "is_relevant": False},
                {"rid": "undecided", "pid": "P2", "is_relevant": None},
                {"rid": "itmo_contributor", "pid": "P1", "is_relevant": False},
            ],
        }
        self.assertEqual(builder.third_party_repos(graph), {"foreign"})

    def test_a_snapshot_without_edge_tables_has_none(self):
        self.assertEqual(builder.third_party_repos({"repositories": []}), set())


class ReportsBuilderTest(FakeOsaTestCase):
    def setUp(self):
        super().setUp()
        self.config = replace(Settings(), data_dir=self.root / "data", openrouter_api_key="key")
        self.snapshot = self.root / "snapshot.json"
        write_snapshot(self.snapshot, {"repositories": [
            {"id": "github_a_one", "url": "https://github.com/a/one"},
            {"id": "github_b_two", "url": "https://github.com/b/two"},
            {"id": "github_c_nourl", "url": None},
        ]})
        self.spend = [0.0, 0.5]
        # One language keeps the counts below readable; both are covered separately.
        for target, value in (
            ("LANGUAGES", {"en": "English"}),
            ("shutil.which", lambda name: f"/usr/bin/{name}"),
            ("OpenRouterClient.usage", lambda client: self.spend.pop(0)),
        ):
            patcher = patch(f"pauk.reports.builder.{target}", value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def build(self, **kwargs):
        return builder.ReportsBuilder(self.config).build(self.snapshot, self.out_dir, workers=2, **kwargs)

    def test_skips_existing_reports_unless_forced_and_indexes_the_disk(self):
        existing = self.out_dir / "en" / "github_a_one.pdf"
        existing.parent.mkdir(parents=True)
        existing.write_bytes(b"%PDF-old")

        self.assertEqual(self.build(), {"generated": 1, "skipped": 1, "failed": 0, "third_party": 0, "spent_usd": 0.5})
        self.assertEqual(existing.read_bytes(), b"%PDF-old")
        self.assertEqual(
            json.loads((self.out_dir / "index.json").read_text()), {"en": ["github_a_one", "github_b_two"]}
        )

        self.spend = [0.5, 0.75]
        result = self.build(keys=frozenset({"github_a_one"}), force=True)
        self.assertEqual((result["generated"], result["skipped"]), (1, 0))
        self.assertEqual(existing.read_bytes(), b"%PDF-1.4 report")

    def test_each_language_gets_its_own_folder_and_index_entry(self):
        with patch.object(builder, "LANGUAGES", {"en": "English", "ru": "Russian"}):
            result = self.build()
        self.assertEqual(result["generated"], 4)
        index = json.loads((self.out_dir / "index.json").read_text())
        self.assertEqual(index, {"en": ["github_a_one", "github_b_two"], "ru": ["github_a_one", "github_b_two"]})

    def test_third_party_repositories_are_left_out_unless_named_and_their_old_reports_stay(self):
        write_snapshot(self.snapshot, {
            "repositories": [
                {"id": "github_a_one", "url": "https://github.com/a/one"},
                {"id": "github_b_two", "url": "https://github.com/b/two"},
            ],
            "repo_pubs": [{"rid": "github_a_one", "pid": "P1"}],
            "repo_persons": [],
            "mentions_repos": [{"rid": "github_b_two", "pid": "P1", "is_relevant": False}],
        })
        old = self.out_dir / "en" / "github_b_two.pdf"
        old.parent.mkdir(parents=True)
        old.write_bytes(b"%PDF-old")

        result = self.build()
        self.assertEqual((result["generated"], result["skipped"], result["third_party"]), (1, 0, 1))
        self.assertEqual(old.read_bytes(), b"%PDF-old")
        self.assertEqual(json.loads((self.out_dir / "index.json").read_text()), {"en": ["github_a_one", "github_b_two"]})

        self.spend = [0.5, 0.75]
        result = self.build(keys=frozenset({"github_b_two"}), force=True)
        self.assertEqual((result["generated"], result["third_party"]), (1, 0))
        self.assertEqual(old.read_bytes(), b"%PDF-1.4 report")

    def test_reports_go_to_the_site_data_by_default(self):
        builder.ReportsBuilder(self.config).build(self.snapshot, keys=frozenset({"github_a_one"}))
        self.assertTrue((self.config.gui_dir / "private" / "reports" / "en" / "github_a_one.pdf").exists())

    def test_failures_are_counted_logged_with_the_log_folder_and_the_index_still_written(self):
        self.mode = "crash"
        with self.assertLogs("pauk.reports", "WARNING") as logs:
            result = self.build()
        self.assertEqual(result["failed"], 2)
        self.assertIn(str(self.config.osa_dir / "logs"), logs.output[-1])
        self.assertEqual(json.loads((self.out_dir / "index.json").read_text()), {})

    def test_an_interrupted_run_still_indexes_what_is_on_disk(self):
        done = self.out_dir / "en" / "github_a_one.pdf"
        done.parent.mkdir(parents=True)
        done.write_bytes(b"%PDF-old")
        with patch.object(osa.OsaRunner, "run", side_effect=KeyboardInterrupt), \
                patch.object(osa.OsaRunner, "stop") as stop, self.assertRaises(KeyboardInterrupt):
            self.build(force=True)
        stop.assert_called_once()
        self.assertEqual(json.loads((self.out_dir / "index.json").read_text()), {"en": ["github_a_one"]})

    def test_nothing_to_generate_needs_no_key_uvx_or_network(self):
        for key in ("github_a_one", "github_b_two"):
            (self.out_dir / "en").mkdir(parents=True, exist_ok=True)
            (self.out_dir / "en" / f"{key}.pdf").write_bytes(b"%PDF")
        self.config = replace(self.config, openrouter_api_key="")
        with patch("pauk.reports.builder.shutil.which", lambda name: None), \
                patch("pauk.reports.builder.OpenRouterClient.usage", side_effect=AssertionError("network")):
            result = self.build()
        self.assertEqual(result, {"generated": 0, "skipped": 2, "failed": 0, "third_party": 0, "spent_usd": 0.0})
        self.assertEqual(json.loads((self.out_dir / "index.json").read_text()), {"en": ["github_a_one", "github_b_two"]})

    def test_an_unreadable_final_spend_still_returns_the_counts(self):
        spends = iter([0.0])

        def usage(client):
            for value in spends:
                return value
            raise HttpRequestError("GET", "https://openrouter.ai/api/v1/key", status_code=502)

        with patch("pauk.reports.builder.OpenRouterClient.usage", usage), \
                self.assertLogs("pauk.reports.builder", "WARNING"):
            result = self.build()
        self.assertEqual((result["generated"], result["spent_usd"]), (2, None))

    def test_preflight_stops_before_any_osa_run(self):
        with self.assertRaisesRegex(ValueError, "github_x_y"):
            self.build(keys=frozenset({"github_x_y"}))
        with patch("pauk.reports.builder.shutil.which", lambda name: None), \
                self.assertRaisesRegex(RuntimeError, "uvx"):
            self.build()

        def unreachable(client):
            raise HttpRequestError("GET", "https://openrouter.ai/api/v1/key", cause_type="ConnectionError")

        with patch("pauk.reports.builder.OpenRouterClient.usage", unreachable), \
                self.assertRaisesRegex(RuntimeError, "not reachable"):
            self.build()
        self.config = replace(self.config, openrouter_api_key="")
        with self.assertRaisesRegex(ValueError, "OPENROUTER_API_KEY"):
            self.build()
        self.assertFalse(self.out_dir.exists())


class ReportsCliTest(unittest.TestCase):
    def parse(self, *argv):
        return _build_parser().parse_args(["reports", "build", *argv])

    def test_defaults_and_scoping(self):
        args = self.parse()
        self.assertEqual((args.keys, args.force, args.workers), ([], False, builder.WORKERS))
        args = self.parse("github_a_b", "--force", "--workers", "2")
        self.assertEqual((args.keys, args.force, args.workers), (["github_a_b"], True, 2))

    def test_errors_and_failed_reports_end_with_a_message_not_a_traceback(self):
        args = self.parse("--cache", "snapshot.json")
        parser = _build_parser()
        with patch("pauk.reports.ReportsBuilder.build", side_effect=ValueError("not in the snapshot: x")), \
                self.assertRaisesRegex(SystemExit, "not in the snapshot"):
            _cmd_reports(args, parser)
        failed = {"generated": 0, "skipped": 0, "failed": 2, "spent_usd": 0.0}
        with patch("pauk.reports.ReportsBuilder.build", return_value=failed), \
                contextlib.redirect_stderr(io.StringIO()), self.assertRaisesRegex(SystemExit, "2 report"):
            _cmd_reports(args, parser)


if __name__ == "__main__":
    unittest.main()
