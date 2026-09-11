from collections.abc import Mapping, Sequence
from typing import Any

import pytest
from pydantic import BaseModel

from app.llm_provider import (
    LLMProviderCallError,
    LLMProviderType,
    LLMResponseFormatError,
    MultiVendorLLMProvider,
)


class Answer(BaseModel):
    value: int


class Client:
    def __init__(self, data: object = None, error: Exception | None = None) -> None:
        self.data = {"value": 7} if data is None else data
        self.error = error

    def generate_structured(
        self,
        prompt: str,
        response_model: type[BaseModel],
        model: str,
        images: Sequence[str],
    ) -> tuple[Any, Mapping[str, Any]]:
        if self.error:
            raise self.error
        return self.data, {"input_tokens": 3, "output_tokens": 2, "cost": 0.01}


def test_provider_returns_validated_data_and_normalized_metadata() -> None:
    provider = MultiVendorLLMProvider({LLMProviderType.OPENAI: Client()})

    result = provider.generate_structured("prompt", Answer, LLMProviderType.OPENAI, "model")

    assert result.data == Answer(value=7)
    assert result.metadata.provider is LLMProviderType.OPENAI
    assert result.metadata.model == "model"
    assert result.metadata.input_tokens == 3


def test_provider_rejects_invalid_structured_output() -> None:
    provider = MultiVendorLLMProvider({LLMProviderType.GEMINI: Client(data={"bad": 1})})

    with pytest.raises(LLMResponseFormatError):
        provider.generate_structured("prompt", Answer, LLMProviderType.GEMINI, "model")


def test_provider_normalizes_vendor_failure() -> None:
    provider = MultiVendorLLMProvider(
        {LLMProviderType.CLAUDE: Client(error=TimeoutError("timeout"))}
    )

    with pytest.raises(LLMProviderCallError, match="CLAUDE request failed"):
        provider.generate_structured("prompt", Answer, LLMProviderType.CLAUDE, "model")
