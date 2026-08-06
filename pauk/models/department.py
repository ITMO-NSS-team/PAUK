from pydantic import BaseModel, Field


class Department(BaseModel):
    id: str
    name_en: str
    name_ru: str | None = None
    name_variants: list[str] = Field(default_factory=list)
    # Generic names (e.g. "Department of Physics") that also occur at foreign
    # organisations. Unlike name_variants they match ONLY inside an affiliation
    # segment that carries an ITMO marker, so they cannot pull in co-affiliations.
    context_aliases: list[str] = Field(default_factory=list)
    # Top-level unit (megafaculty/institute); becomes a PART_OF edge in the graph.
    # None when the unit's school is unknown. See School.
    school_id: str | None = None
