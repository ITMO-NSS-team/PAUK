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


def _pid_alive(pid: int) -> bool:
    """Whether a process with this pid is still running.

    On Windows os.kill(pid, 0) is not a probe: it terminates the target
    (TerminateProcess with exit code 0) and raises WinError 87 for dead
    pids, so the owner must be queried without sending anything.
    """
    if os.name == "nt":
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        ERROR_ACCESS_DENIED = 5
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            # Access denied means the process exists under another account.
            return ctypes.get_last_error() == ERROR_ACCESS_DENIED
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


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
                except (FileNotFoundError, ValueError):
                    self.path.unlink(missing_ok=True)
                    continue
                if not _pid_alive(pid):
                    self.path.unlink(missing_ok=True)
                    continue
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"group is locked by another process: {self.path}")
                time.sleep(0.1)

    def __exit__(self, exc_type, exc, traceback) -> bool:
        if self._acquired:
            self.path.unlink(missing_ok=True)
        return False
