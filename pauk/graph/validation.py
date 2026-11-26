"""Required values at graph publication and ordinary editing boundaries."""


class CsvNodeError(ValueError):
    """A CSV node cannot be safely applied to its matched graph records."""


class RepositoryNameError(CsvNodeError):
    """A repository row would leave the node without a usable name."""


def validate_repository_name(value: object) -> None:
    """Require a supplied name without inventing or rewriting its value."""
    if not isinstance(value, str) or not value.strip():
        raise RepositoryNameError("Repository.name must be a non-empty string")
