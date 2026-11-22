"""Required values at graph publication and ordinary editing boundaries."""


def validate_repository_name(value: object) -> None:
    """Require a supplied name without inventing or rewriting its value."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Repository.name must be a non-empty string")
