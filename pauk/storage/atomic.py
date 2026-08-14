from __future__ import annotations

import os
from pathlib import Path
from tempfile import NamedTemporaryFile


class AtomicWriter:
    """Write beside the target and replace it only after a successful write."""

    def __init__(self, target: Path) -> None:
        self.target = target
        self._tmp: Path | None = None
        self.file = None

    def __enter__(self):
        self.target.parent.mkdir(parents=True, exist_ok=True)
        handle = NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="\n", delete=False,
            dir=self.target.parent, prefix=f".{self.target.name}.", suffix=".tmp",
        )
        self._tmp = Path(handle.name)
        self.file = handle
        return handle

    def __exit__(self, exc_type, exc, _traceback) -> bool:
        assert self.file is not None and self._tmp is not None
        self.file.close()
        if exc_type is None:
            os.replace(self._tmp, self.target)
        else:
            self._tmp.unlink(missing_ok=True)
        return False


def atomic_write_bytes(target: Path, data: bytes) -> None:
    """Binary counterpart to AtomicWriter, for content already fully in memory
    (e.g. a downloaded PDF) - no streaming writer needed, just one safe swap.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(
        mode="wb", delete=False,
        dir=target.parent, prefix=f".{target.name}.", suffix=".tmp",
    ) as handle:
        tmp = Path(handle.name)
        try:
            handle.write(data)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
    os.replace(tmp, target)
