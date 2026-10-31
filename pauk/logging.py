import logging
import os
import sys
import threading
from logging.handlers import WatchedFileHandler
from pathlib import Path

from pauk.redaction import configured_secret_values, redact_text

QUIET_THIRD_PARTY_LOGGERS = ("neo4j", "requests")
HTTP_TRACE_LOGGER = "urllib3.connectionpool"
DEFAULT_SERVICE = "pauk"

_LEVELS = {"WARNING": "WARN", "CRITICAL": "CRIT"}
_COLORS = {"DEBUG": "\033[2m", "WARN": "\033[33m", "ERROR": "\033[31m", "CRIT": "\033[1;31m"}
_RESET = "\033[0m"

_run_lock = threading.Lock()
_run: str | None = None


def set_run(tag: str | None) -> None:
    """Tag every following line with the run it belongs to; None clears it.

    Process-wide rather than per thread: a worker does one job at a time, and
    the stages fan out into thread pools that would not inherit a contextvar.
    """
    global _run
    with _run_lock:
        _run = tag


class LineFormatter(logging.Formatter):
    """One record per line in fixed columns; secrets are redacted last.

    `full` is the file and container layout: date, service, level, module,
    run. The terminal layout drops what the person running it already knows
    (date, service) and colors the level.
    """

    def __init__(self, service: str, *, full: bool, color: bool = False) -> None:
        super().__init__()
        self.service = service
        self.full = full
        self.color = color
        self.secret_values = configured_secret_values()

    def format(self, record: logging.LogRecord) -> str:
        level = _LEVELS.get(record.levelname, record.levelname)
        shown = f"{level:<5}"
        if self.color and level in _COLORS:
            shown = f"{_COLORS[level]}{shown}{_RESET}"
        module = record.name.rsplit(".", 1)[-1] if record.name.startswith("pauk.") else record.name
        run = f"[{_run}] " if _run else ""
        clock = (self.formatTime(record, "%Y-%m-%d %H:%M:%S") + f".{int(record.msecs):03d}"
                 if self.full else self.formatTime(record, "%H:%M:%S"))
        head = f"{clock}  {self.service:<6}  " if self.full else f"{clock}  "
        line = f"{head}{shown}  {module:<13} {run}{record.getMessage()}"
        if record.exc_info:
            record.exc_text = record.exc_text or self.formatException(record.exc_info)
        if record.exc_text:
            line += "\n" + "\n".join(f"    {row}" for row in record.exc_text.splitlines())
        return redact_text(line, self.secret_values)


def configure_logging(verbose: bool = False, service: str | None = None) -> None:
    """Console output always; a file in PAUK_LOG_DIR as well when it is set.

    The file handler reopens its file after logrotate moves it away, so the
    rotation needs no signal to the process. Neo4j driver notifications are
    suppressed below ERROR unless verbose logging is enabled.
    """
    service = service or os.getenv("PAUK_SERVICE") or DEFAULT_SERVICE
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    root.setLevel(logging.INFO)

    console = logging.StreamHandler()
    tty = sys.stderr.isatty()
    console.setFormatter(LineFormatter(service, full=not tty, color=tty))
    root.addHandler(console)

    log_dir = os.getenv("PAUK_LOG_DIR")
    if log_dir:
        Path(log_dir).mkdir(parents=True, exist_ok=True)
        file = WatchedFileHandler(Path(log_dir) / f"{service}.log", encoding="utf-8")
        file.setFormatter(LineFormatter(service, full=True))
        root.addHandler(file)

    logging.getLogger("pauk").setLevel(logging.DEBUG if verbose else logging.INFO)
    logging.getLogger(HTTP_TRACE_LOGGER).setLevel(logging.DEBUG if verbose else logging.WARNING)
    for name in QUIET_THIRD_PARTY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
    if not verbose:
        logging.getLogger("neo4j.notifications").setLevel(logging.ERROR)


class Throttled:
    """A warning that repeats per item, shown once and then every `every` times.

    The rest go to DEBUG, so `--verbose` still has each one. Counted per kind,
    so one noisy failure does not hide the first of another.
    """

    def __init__(self, logger: logging.Logger, every: int = 100) -> None:
        self.logger = logger
        self.every = every
        self._counts: dict[str, int] = {}
        self._lock = threading.Lock()

    def warning(self, kind: str, message: str, *args: object) -> None:
        with self._lock:
            count = self._counts.get(kind, 0) + 1
            self._counts[kind] = count
        if count == 1 or count % self.every == 0:
            suffix = f" ({count} so far)" if count > 1 else ""
            self.logger.warning(message + suffix, *args)
        else:
            self.logger.debug(message, *args)
