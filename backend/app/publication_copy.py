"""LLM-backed, editable publication copy; it is not a Workflow node."""

import json

from pydantic import BaseModel, Field

from app.llm_provider import LLMError, LLMProvider, LLMProviderType


class PublicationCopyDraft(BaseModel):
    youtube_title: str = Field(min_length=1, max_length=100)
    youtube_description: str = Field(min_length=1)
    instagram_caption: str = Field(min_length=1)


class PublicationCopyService:
    def __init__(self, llm_provider: LLMProvider, provider: LLMProviderType, model: str) -> None:
        self._llm_provider, self._provider, self._model = llm_provider, provider, model

    def generate(self, planning: dict[str, object], script: dict[str, object]) -> PublicationCopyDraft:
        prompt = (
            "Create editable social publication copy from this approved VORA planning and script. "
            "Return a concise YouTube title, YouTube description, and Instagram caption. "
            "Do not claim facts not present in the input. Input: "
            + json.dumps({"planning": planning, "script": script}, ensure_ascii=False)
        )
        try:
            result = self._llm_provider.generate_structured(
                prompt, PublicationCopyDraft, self._provider, self._model
            )
            return PublicationCopyDraft.model_validate(result.data)
        except LLMError:
            return self.fallback(planning)

    @staticmethod
    def fallback(planning: dict[str, object]) -> PublicationCopyDraft:
        concept = str(planning.get("concept") or "VORA marketing video")
        message = str(planning.get("key_message") or concept)
        return PublicationCopyDraft(
            youtube_title=concept[:100],
            youtube_description=message,
            instagram_caption=message,
        )
