from pydantic import BaseModel


class School(BaseModel):
    """Top-level unit (megafaculty or standalone institute/centre) that
    departments belong to.

    The hierarchy is stored as a Department-[:PART_OF]->School edge: a
    department carries a school_id (see Department) and School is its own graph
    node. The id is derived deterministically from name_en, so graph links stay
    stable across runs.
    """

    id: str
    name_en: str
    name_ru: str | None = None
