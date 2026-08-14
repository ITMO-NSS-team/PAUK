import logging

from pauk.redaction import configured_secret_values, redact_text

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"
QUIET_THIRD_PARTY_LOGGERS = ("neo4j", "requests")
HTTP_TRACE_LOGGER = "urllib3.connectionpool"


class RedactingFormatter(logging.Formatter):
    """Last-resort protection for diagnostics emitted by application code."""

    def __init__(self, fmt: str) -> None:
        super().__init__(fmt)
        self.secret_values = configured_secret_values()

    def format(self, record: logging.LogRecord) -> str:
        return redact_text(super().format(record), self.secret_values)


def configure_logging(verbose: bool = False) -> None:
    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    formatter = RedactingFormatter(LOG_FORMAT)
    for handler in root.handlers:
        handler.setLevel(logging.NOTSET)
        handler.setFormatter(formatter)

    logging.getLogger("pauk").setLevel(logging.DEBUG if verbose else logging.INFO)
    logging.getLogger(HTTP_TRACE_LOGGER).setLevel(logging.DEBUG if verbose else logging.WARNING)
    for name in QUIET_THIRD_PARTY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
    # neo4j's own driver notifications (Cypher deprecation/perf hints) are
    # noisier than the base "neo4j" logger quieted above - drop them further.
    if not verbose:
        logging.getLogger("neo4j.notifications").setLevel(logging.ERROR)

