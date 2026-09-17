"""OpenAI Responses API adapter for VORA's structured LLM boundary."""

import json
import re
from collections.abc import Mapping, Sequence
from copy import deepcopy
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel

from app.llm_provider import LLMProviderCallError, LLMResponseFormatError

ResponseT = TypeVar("ResponseT", bound=BaseModel)


class OpenAIStructuredClient:
    """One-shot Structured Outputs client; request and response bodies never reach errors."""

    _RESPONSES_URL = "https://api.openai.com/v1/responses"

    def __init__(self, api_key: str | None) -> None:
        if not api_key:
            raise ValueError("OpenAI API key is not configured")
        self._api_key = api_key

    def generate_structured(
        self,
        prompt: str,
        response_model: type[ResponseT],
        model: str,
        images: Sequence[str],
    ) -> tuple[ResponseT | Mapping[str, Any], Mapping[str, Any]]:
        if images:
            # Asset IDs are not URLs. Image input needs a separate, explicit design.
            raise LLMProviderCallError("OpenAI image input is not implemented")
        try:
            response = httpx.post(
                self._RESPONSES_URL,
                headers={"Authorization": f"Bearer {self._api_key}"},
                json={
                    "model": model,
                    "input": [{"role": "user", "content": [{"type": "input_text", "text": prompt}]}],
                    "text": {
                        "format": {
                            "type": "json_schema",
                            "name": self._schema_name(response_model),
                            "strict": True,
                            "schema": self._strict_schema(response_model),
                        }
                    },
                },
                timeout=30.0,
            )
        except httpx.HTTPError as exc:
            raise LLMProviderCallError("OpenAI request failed") from exc
        if response.is_error:
            raise LLMProviderCallError("OpenAI request failed")
        try:
            payload = response.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise LLMResponseFormatError("OpenAI returned an invalid response") from exc
        if not isinstance(payload, dict) or payload.get("status") != "completed":
            raise LLMProviderCallError("OpenAI response was incomplete")
        output = payload.get("output")
        if not isinstance(output, list):
            raise LLMResponseFormatError("OpenAI response did not contain structured output")
        for item in output:
            if not isinstance(item, dict) or item.get("type") != "message":
                continue
            content = item.get("content")
            if not isinstance(content, list):
                continue
            for part in content:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "refusal":
                    raise LLMResponseFormatError("OpenAI response was refused")
                if part.get("type") == "output_text" and isinstance(part.get("text"), str):
                    try:
                        return json.loads(part["text"]), self._usage(payload)
                    except json.JSONDecodeError as exc:
                        raise LLMResponseFormatError(
                            "OpenAI structured output was invalid"
                        ) from exc
        raise LLMResponseFormatError("OpenAI response did not contain structured output")

    @staticmethod
    def _schema_name(response_model: type[BaseModel]) -> str:
        return re.sub(r"[^A-Za-z0-9_-]", "_", response_model.__name__)[:64] or "response"

    @classmethod
    def _strict_schema(cls, response_model: type[BaseModel]) -> dict[str, Any]:
        """Normalize Pydantic's permissive schema to OpenAI's strict subset.

        VORA validates the returned data with the original Pydantic model after
        the request, so removing unsupported JSON Schema constraints does not
        relax the application's business validation.
        """
        schema = deepcopy(response_model.model_json_schema())
        cls._normalize_schema(schema)
        return schema

    @classmethod
    def _normalize_schema(cls, node: Any) -> None:
        if isinstance(node, dict):
            properties = node.get("properties")
            if isinstance(properties, dict):
                node["additionalProperties"] = False
                node["required"] = list(properties)
            # Strict Structured Outputs requires explicit null unions for
            # optional fields, which Pydantic already emits. Defaults and
            # array-length constraints are not required for transport safety.
            for keyword in ("default", "minItems", "maxItems"):
                node.pop(keyword, None)
            for value in node.values():
                cls._normalize_schema(value)
        elif isinstance(node, list):
            for value in node:
                cls._normalize_schema(value)

    @staticmethod
    def _usage(payload: Mapping[str, Any]) -> Mapping[str, Any]:
        usage = payload.get("usage")
        if not isinstance(usage, Mapping):
            return {}
        return {
            "input_tokens": usage.get("input_tokens"),
            "output_tokens": usage.get("output_tokens"),
        }
