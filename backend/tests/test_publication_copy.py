from pydantic import BaseModel

from app.llm_provider import (
    LLMMetadata,
    LLMProviderCallError,
    LLMProviderType,
    LLMResult,
)
from app.publication_copy import PublicationCopyService


class CopyProvider:
    def generate_structured(self, prompt: str, response_model: type[BaseModel], provider: LLMProviderType, model: str, images: tuple[str, ...] = ()) -> LLMResult:
        return LLMResult(response_model(youtube_title="Title", youtube_description="Description", instagram_caption="Caption"), LLMMetadata(provider=provider, model=model))


class FailingCopyProvider:
    def generate_structured(self, *args: object, **kwargs: object) -> LLMResult:
        raise LLMProviderCallError("unavailable")


def test_publication_copy_uses_llm_and_has_safe_fallback() -> None:
    service = PublicationCopyService(CopyProvider(), LLMProviderType.OPENAI, "test")  # type: ignore[arg-type]
    assert service.generate({"concept": "Plan"}, {"scenes": []}).youtube_title == "Title"
    fallback = PublicationCopyService(FailingCopyProvider(), LLMProviderType.OPENAI, "test")  # type: ignore[arg-type]
    assert fallback.generate({"concept": "Plan", "key_message": "Message"}, {}).instagram_caption == "Message"
