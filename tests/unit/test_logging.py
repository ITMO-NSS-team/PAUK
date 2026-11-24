import logging
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pauk.logging import LineFormatter, Throttled, configure_logging, set_run


def record(name="pauk.pipeline.stages.dedup", level=logging.INFO, msg="merged %d", args=(3,)):
    return logging.LogRecord(name, level, __file__, 1, msg, args, None)


class LineFormatterTest(unittest.TestCase):
    def tearDown(self):
        set_run(None)

    def test_full_line_has_fixed_columns(self):
        line = LineFormatter("worker", full=True).format(record(level=logging.WARNING))
        date, rest = line[:23], line[23:]
        self.assertRegex(date, r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d{3}$")
        self.assertEqual(rest, "  worker  WARN   dedup         merged 3")

    def test_run_tag_goes_before_the_message(self):
        set_run("pipeline 812")
        line = LineFormatter("worker", full=True).format(record())
        self.assertTrue(line.endswith("dedup         [pipeline 812] merged 3"))

    def test_terminal_line_is_short_and_colors_only_the_level(self):
        line = LineFormatter("cli", full=False, color=True).format(record(level=logging.ERROR))
        self.assertRegex(line, r"^\d\d:\d\d:\d\d  \x1b\[31mERROR\x1b\[0m  dedup         merged 3$")

    def test_third_party_logger_keeps_its_full_name(self):
        line = LineFormatter("admin", full=False).format(record(name="uvicorn.access", msg="GET /", args=()))
        self.assertIn("uvicorn.access GET /", line)

    def test_traceback_is_indented_under_its_line(self):
        try:
            raise ValueError("boom")
        except ValueError:
            entry = logging.LogRecord("pauk.jobs.worker", logging.ERROR, __file__, 1, "job failed", (), sys.exc_info())
        lines = LineFormatter("worker", full=True).format(entry).splitlines()
        self.assertIn("job failed", lines[0])
        self.assertTrue(all(row.startswith("    ") for row in lines[1:]))
        self.assertIn("ValueError: boom", lines[-1])

    def test_secrets_are_redacted(self):
        with patch.dict(os.environ, {"NEO4J_PASSWORD": "hunter2-secret"}):
            line = LineFormatter("worker", full=True).format(record(msg="password %s", args=("hunter2-secret",)))
        self.assertNotIn("hunter2-secret", line)


class ConfigureLoggingTest(unittest.TestCase):
    def setUp(self):
        root = logging.getLogger()
        self.saved = (list(root.handlers), root.level)

    def tearDown(self):
        root = logging.getLogger()
        for handler in root.handlers:
            handler.close()
        root.handlers[:] = self.saved[0]
        root.setLevel(self.saved[1])

    def test_writes_a_file_named_after_the_service_when_a_log_dir_is_set(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"PAUK_LOG_DIR": tmp, "PAUK_SERVICE": "worker"}):
                configure_logging()
            logging.getLogger("pauk.jobs.worker").info("worker started")
            for handler in logging.getLogger().handlers:
                handler.flush()
            text = (Path(tmp) / "worker.log").read_text(encoding="utf-8")
        self.assertIn("  worker  INFO   worker        worker started", text)

    def test_no_file_without_a_log_dir(self):
        env = {key: value for key, value in os.environ.items() if key != "PAUK_LOG_DIR"}
        with patch.dict(os.environ, env, clear=True):
            configure_logging()
        self.assertEqual(len(logging.getLogger().handlers), 1)


class ThrottledTest(unittest.TestCase):
    def test_first_and_every_nth_are_warnings_the_rest_debug(self):
        logger = logging.getLogger("pauk.test.throttled")
        logger.setLevel(logging.DEBUG)
        throttled = Throttled(logger, every=3)
        with self.assertLogs(logger, level="DEBUG") as logs:
            for _ in range(6):
                throttled.warning("parse", "could not parse %s", "x")
        levels = [entry.split(":", 1)[0] for entry in logs.output]
        self.assertEqual(levels, ["WARNING", "DEBUG", "WARNING", "DEBUG", "DEBUG", "WARNING"])
        self.assertTrue(logs.output[2].endswith("could not parse x (3 so far)"))

    def test_each_kind_is_counted_on_its_own(self):
        logger = logging.getLogger("pauk.test.throttled.kinds")
        throttled = Throttled(logger, every=100)
        with self.assertLogs(logger, level="WARNING") as logs:
            throttled.warning("parse", "parse failed")
            throttled.warning("http", "http failed")
        self.assertEqual(len(logs.output), 2)


if __name__ == "__main__":
    unittest.main()
