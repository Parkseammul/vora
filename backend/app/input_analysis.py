import enum
import re
from collections.abc import Mapping

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
    previous_output = input_data.get("previous_output")
    if isinstance(previous_output, Mapping):
        # Revisions start from the persisted analysis, not from fresh defaults.
        baseline = InputAnalysisResult.model_validate(previous_output)
        return _apply_revision(baseline, str(input_data.get("revision_request", "")))

    request_text = str(input_data["request_text"])
    revision_request = str(input_data.get("revision_request", ""))
    analysis_text = f"{request_text} {revision_request}".lower()
    content_goal = (
        ContentGoal.PRODUCT_AD
        if any(keyword in analysis_text for keyword in ("광고", "ad", "product"))
        else ContentGoal.GENERAL_SHORTFORM
    )
    return InputAnalysisResult(
        content_goal=content_goal,
        target_audience=TargetAudience(
            age_group=(
                AgeGroup.TWENTIES
                if any(keyword in analysis_text for keyword in ("20대", "twenties"))
                else AgeGroup.ALL
            )
        ),
        source_asset_ids=input_data["source_asset_ids"],  # type: ignore[arg-type]
    )


def _apply_revision(
    baseline: InputAnalysisResult, revision_request: str
) -> InputAnalysisResult:
    text = revision_request.lower()
    updates: dict[str, object] = {}

    if any(keyword in text for keyword in ("광고", "ad", "product")):
        updates["content_goal"] = ContentGoal.PRODUCT_AD
    elif any(keyword in text for keyword in ("정보", "informational")):
        updates["content_goal"] = ContentGoal.INFORMATIONAL

    audience = baseline.target_audience
    age_group = _requested_age_group(text)
    if age_group is not None:
        audience = audience.model_copy(update={"age_group": age_group})
    if any(keyword in text for keyword in ("여성", "female")):
        audience = audience.model_copy(update={"gender": Gender.FEMALE})
    elif any(keyword in text for keyword in ("남성", "male")):
        audience = audience.model_copy(update={"gender": Gender.MALE})
    if audience != baseline.target_audience:
        updates["target_audience"] = audience

    duration_match = re.search(r"(\d{1,2})\s*초", text)
    if duration_match is not None:
        updates["duration_seconds"] = int(duration_match.group(1))
    if "차분" in text:
        updates["tone"] = "차분하게"
    elif "밝게" in text or "밝은" in text:
        updates["tone"] = "밝고 자연스럽게"

    # Asset reordering is outside this task; source_asset_ids always remain the baseline value.
    return baseline.model_copy(update=updates)


def _requested_age_group(text: str) -> AgeGroup | None:
    age_keywords = (
        (AgeGroup.TEENS, ("10대", "teens")),
        (AgeGroup.TWENTIES, ("20대", "twenties")),
        (AgeGroup.THIRTIES, ("30대", "thirties")),
        (AgeGroup.FORTIES, ("40대", "forties")),
        (AgeGroup.FIFTIES_PLUS, ("50대", "fifties")),
    )
    for age_group, keywords in age_keywords:
        if any(keyword in text for keyword in keywords):
            return age_group
    return None
