from typing import Any, Literal

from pydantic import BaseModel, model_validator

from .processing import ClassificationStatus


class Authorship(BaseModel):
    publication_id: str
    position: int | None = None
    affiliation: str | None = None
    # None while the affiliation is the one the work itself states; set when
    # it had none and the persons stage filled it from the author's own
    # records ("openalex" / "orcid").
    affiliation_source: str | None = None
    is_corresponding: bool = False


class Contribution(BaseModel):
    repository_id: str
    role: str | None = None


class MentionsLink(BaseModel):
    target_kind: Literal["repository", "candidate"]
    repository_url: str | None = None
    candidate_id: str | None = None
    context: str | None = None
    page_number: int | None = None
    classification_status: ClassificationStatus = ClassificationStatus.PENDING
    is_relevant: bool | None = None
    llm_confidence: float | None = None
    llm_reason: str | None = None

    @model_validator(mode="before")
    @classmethod
    def infer_legacy_classification_status(cls, data: Any) -> Any:
        if not isinstance(data, dict) or data.get("classification_status") is not None:
            return data
        values = dict(data)
        verdict_fields = ("is_relevant", "llm_confidence", "llm_reason")
        if any(values.get(field) is not None for field in verdict_fields):
            values["classification_status"] = ClassificationStatus.CLASSIFIED
        return values

