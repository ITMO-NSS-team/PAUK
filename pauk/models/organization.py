from pydantic import BaseModel


class Organization(BaseModel):
    """A top-level organisation (university, institute, company) — the root of an
    org hierarchy. Departments hang off it via Department-[:PART_OF]->Organization;
    several organisations can coexist in one graph (ITMO plus co-affiliations).
    """

    id: str
    name_en: str
    name_ru: str | None = None
    ror_id: str | None = None
    country: str | None = None
    type: str | None = None
