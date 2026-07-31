from __future__ import annotations

import os
import time
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


class GroupLock:
    """Inter-process lock for a data group during read-modify-write work."""

    def __init__(self, data_dir: Path, group: str, timeout: float = 30.0) -> None:
        self.path = data_dir / ".locks" / f"{group}.lock"
        self.timeout = timeout
        self._acquired = False

    def __enter__(self) -> "GroupLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                with self.path.open("x", encoding="utf-8") as fh:
                    fh.write(f"pid={os.getpid()}\n")
                self._acquired = True
                return self
            except FileExistsError:
                try:
                    pid_line = self.path.read_text(encoding="utf-8").strip()
                    pid = int(pid_line.removeprefix("pid="))
                    os.kill(pid, 0)
                except (FileNotFoundError, ProcessLookupError, ValueError):
                    self.path.unlink(missing_ok=True)
                    continue
                except PermissionError:
                    pass
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"group is locked by another process: {self.path}")
                time.sleep(0.1)

    def __exit__(self, exc_type, exc, traceback) -> bool:
        if self._acquired:
            self.path.unlink(missing_ok=True)
        return False
