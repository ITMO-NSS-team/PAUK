import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pauk.ops.site_files import prepare_site_copy


class SiteCopyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.source = root / "source"
        self.source.mkdir()
        (self.source / "assets").mkdir()
        (self.source / "assets" / "app.js").write_text("complete")
        self.destination = root / "releases" / "release1"

    def test_partial_copy_is_replaced_and_permissions_set(self):
        self.destination.mkdir(parents=True)
        (self.destination / "old").write_text("partial")
        prepare_site_copy(self.source, self.destination)
        self.assertEqual((self.destination / "assets" / "app.js").read_text(), "complete")
        self.assertFalse((self.destination / "old").exists())
        for path, expected in [
            (self.destination, 0o755),
            (self.destination / "assets", 0o755),
            (self.destination / "assets" / "app.js", 0o644),
        ]:
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), expected)

    def test_complete_copy_is_reused_and_permissions_repaired(self):
        prepare_site_copy(self.source, self.destination)
        (self.destination / "assets" / "app.js").chmod(0o600)
        with patch("pauk.ops.site_files.shutil.copytree", side_effect=AssertionError("copied again")):
            prepare_site_copy(self.source, self.destination)
        self.assertEqual(stat.S_IMODE((self.destination / "assets" / "app.js").stat().st_mode), 0o644)

    def test_copy_failure_preserves_existing_copy_and_retry_succeeds(self):
        self.destination.mkdir(parents=True)
        (self.destination / "partial").write_text("old")

        def interrupted_copy(source, target, **kwargs):
            Path(target).mkdir()
            (Path(target) / "partial").write_text("incomplete")
            raise OSError("interrupted")

        with (
            patch("pauk.ops.site_files.shutil.copytree", side_effect=interrupted_copy),
            self.assertRaises(OSError),
        ):
            prepare_site_copy(self.source, self.destination)
        self.assertEqual((self.destination / "partial").read_text(), "old")
        prepare_site_copy(self.source, self.destination)
        self.assertEqual((self.destination / "assets" / "app.js").read_text(), "complete")

    def test_interrupted_replacement_recovers_backup(self):
        self.destination.parent.mkdir(parents=True)
        backup = self.destination.with_name(".release1.previous")
        backup.mkdir()
        (backup / "partial").write_text("old")
        with (
            patch("pauk.ops.site_files.shutil.copytree", side_effect=OSError("interrupted")),
            self.assertRaises(OSError),
        ):
            prepare_site_copy(self.source, self.destination)
        self.assertEqual((self.destination / "partial").read_text(), "old")
        prepare_site_copy(self.source, self.destination)
        self.assertFalse(backup.exists())
        self.assertEqual((self.destination / "assets" / "app.js").read_text(), "complete")

    def test_changed_file_with_same_layout_is_replaced(self):
        prepare_site_copy(self.source, self.destination)
        (self.destination / "assets" / "app.js").write_text("damaged!")
        prepare_site_copy(self.source, self.destination)
        self.assertEqual((self.destination / "assets" / "app.js").read_text(), "complete")

    def test_failed_promotion_restores_previous_copy(self):
        self.destination.mkdir(parents=True)
        (self.destination / "partial").write_text("old")
        original_rename = Path.rename

        def fail_promotion(path, target):
            if path.name == "site" and target == self.destination:
                raise OSError("promotion failed")
            return original_rename(path, target)

        with patch.object(Path, "rename", fail_promotion), self.assertRaises(OSError):
            prepare_site_copy(self.source, self.destination)
        self.assertEqual((self.destination / "partial").read_text(), "old")
        self.assertFalse(self.destination.with_name(".release1.previous").exists())
        prepare_site_copy(self.source, self.destination)
        self.assertEqual((self.destination / "assets" / "app.js").read_text(), "complete")

    def test_symbolic_links_are_rejected(self):
        (self.source / "link").symlink_to(self.source / "assets", target_is_directory=True)
        with self.assertRaises(ValueError):
            prepare_site_copy(self.source, self.destination)
        self.assertFalse(self.destination.exists())

    def test_destination_symbolic_link_is_rejected(self):
        self.destination.parent.mkdir(parents=True)
        self.destination.symlink_to(self.source, target_is_directory=True)
        with self.assertRaises(ValueError):
            prepare_site_copy(self.source, self.destination)
        self.assertTrue(self.destination.is_symlink())
