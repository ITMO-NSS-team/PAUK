"""Юнит-тесты для `is_fresh()` — три случая: файла нет, файл свежий, файл устарел."""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from new_cache.freshness import is_fresh


class IsFreshTest(unittest.TestCase):
    def test_missing_file_is_never_fresh(self):
        """Отсутствующий путь — это не 'бесконечно старый' файл, который надо
        было бы как-то по-особому обрабатывать в вызывающем коде, а просто
        гарантированно не свежий."""
        self.assertFalse(is_fresh(Path("/несуществующий/путь/snapshot.json"), timedelta(days=1)))

    def test_freshly_written_file_is_fresh(self):
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as fh:
            fh.write(b"{}")
            path = Path(fh.name)
        try:
            self.assertTrue(is_fresh(path, timedelta(hours=1)))
        finally:
            path.unlink()

    def test_old_file_is_not_fresh(self):
        """Подделываем mtime файла в прошлое вместо реального ожидания — тест
        детерминирован и не тратит время выполнения на настоящий sleep."""
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as fh:
            fh.write(b"{}")
            path = Path(fh.name)
        try:
            two_days_ago = (Path(fh.name).stat().st_mtime) - 2 * 24 * 3600
            os.utime(path, (two_days_ago, two_days_ago))
            self.assertFalse(is_fresh(path, timedelta(days=1)))
        finally:
            path.unlink()
