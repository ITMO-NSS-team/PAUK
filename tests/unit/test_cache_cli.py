"""`pauk cache export --only`: parsing of the group list."""

import contextlib
import io
import unittest

from pauk.cli import _build_parser


class CacheExportOnlyTest(unittest.TestCase):
    def parse(self, *argv: str):
        return _build_parser().parse_args(["cache", "export", *argv])

    def test_without_only_exports_everything(self):
        self.assertIsNone(self.parse().only)

    def test_comma_separated_groups_tolerate_spaces(self):
        self.assertEqual(self.parse("--only", "repos, persons").only, ["repos", "persons"])

    def test_unknown_group_is_rejected_by_argparse(self):
        with contextlib.redirect_stderr(io.StringIO()) as err, self.assertRaises(SystemExit):
            self.parse("--only", "repositories")
        self.assertIn("choose from", err.getvalue())


if __name__ == "__main__":
    unittest.main()
