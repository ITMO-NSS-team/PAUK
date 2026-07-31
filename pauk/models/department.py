from pydantic import BaseModel, Field


class Department(BaseModel):
    id: str
    name_en: str
    name_ru: str | None = None
    name_variants: list[str] = Field(default_factory=list)

