import enum

from pydantic import BaseModel, Field, field_validator


class ContentGoal(str, enum.Enum):
    GENERAL_SHORTFORM = "GENERAL_SHORTFORM"
    PRODUCT_AD = "PRODUCT_AD"
    INFORMATIONAL = "INFORMATIONAL"


class AgeGroup(str, enum.Enum):
    ALL = "ALL"
    TEENS = "TEENS"
    TWENTIES = "TWENTIES"
    THIRTIES = "THIRTIES"
    FORTIES = "FORTIES"
    FIFTIES_PLUS = "FIFTIES_PLUS"


class Gender(str, enum.Enum):
    ALL = "ALL"
    FEMALE = "FEMALE"
    MALE = "MALE"


class TargetAudience(BaseModel):
    age_group: AgeGroup = AgeGroup.ALL
    gender: Gender = Gender.ALL
    audience_group: str = "일반인"

    @field_validator("audience_group")
    @classmethod
    def audience_group_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("audience_group must not be blank")
        return value


class InputAnalysisResult(BaseModel):
    content_goal: ContentGoal = ContentGoal.GENERAL_SHORTFORM
    target_audience: TargetAudience = Field(default_factory=TargetAudience)
    duration_seconds: int = Field(default=30, ge=5, le=60)
    tone: str = "밝고 자연스럽게"
    source_asset_ids: list[int] = Field(default_factory=list, max_length=6)

    @field_validator("tone")
    @classmethod
    def tone_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("tone must not be blank")
        return value

    @field_validator("source_asset_ids")
    @classmethod
    def source_asset_ids_must_be_unique(cls, value: list[int]) -> list[int]:
        if len(value) != len(set(value)):
            raise ValueError("source_asset_ids must not contain duplicates")
        return value


def analyze_input(input_data: dict[str, object]) -> InputAnalysisResult:
    request_text = str(input_data["request_text"])
    content_goal = (
        ContentGoal.PRODUCT_AD
        if any(keyword in request_text.lower() for keyword in ("광고", "ad", "product"))
        else ContentGoal.GENERAL_SHORTFORM
    )
    return InputAnalysisResult(
        content_goal=content_goal,
        source_asset_ids=input_data["source_asset_ids"],  # type: ignore[arg-type]
    )
