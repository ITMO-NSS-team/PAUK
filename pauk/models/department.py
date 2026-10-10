from pydantic import BaseModel, Field


class Department(BaseModel):
    id: str
    name_en: str
    name_ru: str | None = None
    name_variants: list[str] = Field(default_factory=list)
    # Generic names (e.g. "Department of Physics") that also occur at foreign
    # organisations. Unlike name_variants they match only inside an affiliation
    # segment that carries an ITMO marker, so they cannot pull in co-affiliations.
    context_aliases: list[str] = Field(default_factory=list)
    # A sub-unit points at its parent Department via parent_id; a top-level unit
    # points at its Organization via organization_id. At most one is set.
    parent_id: str | None = None
    organization_id: str | None = None
    # megafaculty | school | faculty | institute | center | department | lab | unit.
    # Used for graph styling.
    kind: str | None = None
