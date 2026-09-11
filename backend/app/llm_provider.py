import enum
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel, ValidationError


class LLMProviderType(str, enum.Enum):
    OPENAI = "OPENAI"
    GEMINI = "GEMINI"
    CLAUDE = "CLAUDE"


class LLMError(RuntimeError):
    """Base error exposed by the provider boundary."""


class LLMProviderCallError(LLMError):
    pass


class LLMResponseFormatError(LLMError):
    pass


class LLMMetadata(BaseModel):
    provider: LLMProviderType
    model: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost: float | None = None


ResponseT = TypeVar("ResponseT", bound=BaseModel)


@dataclass(frozen=True)
class LLMResult:
    data: BaseModel
    metadata: LLMMetadata


class LLMProvider(Protocol):
    def generate_structured(
        self,
        prompt: str,
        response_model: type[ResponseT],
        provider: LLMProviderType,
        model: str,
        images: Sequence[str] = (),
    ) -> LLMResult: ...


class VendorStructuredClient(Protocol):
    def generate_structured(
        self,
        prompt: str,
        response_model: type[ResponseT],
        model: str,
        images: Sequence[str],
    ) -> tuple[ResponseT | Mapping[str, Any], Mapping[str, Any]]: ...


class MultiVendorLLMProvider:
    """Dispatches vendor calls and normalizes their data, metadata, and errors."""

    def __init__(self, clients: Mapping[LLMProviderType, VendorStructuredClient]) -> None:
        self._clients = clients

    def generate_structured(
        self,
        prompt: str,
        response_model: type[ResponseT],
        provider: LLMProviderType,
        model: str,
        images: Sequence[str] = (),
    ) -> LLMResult:
        try:
            client = self._clients[provider]
        except KeyError as exc:
            raise LLMProviderCallError(f"LLM provider is not configured: {provider.value}") from exc

        try:
            raw_data, raw_metadata = client.generate_structured(
                prompt, response_model, model, images
            )
        except LLMError:
            raise
        except Exception as exc:
            raise LLMProviderCallError(f"{provider.value} request failed") from exc

        try:
            data = (
                raw_data
                if isinstance(raw_data, response_model)
                else response_model.model_validate(raw_data)
            )
            metadata = LLMMetadata(
                provider=provider,
                model=model,
                input_tokens=raw_metadata.get("input_tokens"),
                output_tokens=raw_metadata.get("output_tokens"),
                cost=raw_metadata.get("cost"),
            )
        except (ValidationError, TypeError, ValueError) as exc:
            raise LLMResponseFormatError("LLM returned invalid structured output") from exc
        return LLMResult(data=data, metadata=metadata)
