from unittest.mock import Mock

import httpx
import pytest
from pydantic import BaseModel

from app.content_planning import ContentPlanningDraft
from app.e2e_providers import DeterministicE2ELLMProvider
from app.llm_provider import (
    LLMProviderCallError,
    LLMProviderType,
    LLMResponseFormatError,
    MultiVendorLLMProvider,
)
from app.openai_client import OpenAIStructuredClient


class Answer(BaseModel):
    value: int


def _response(payload: dict[str, object], status_code: int = 200) -> httpx.Response:
    return httpx.Response(
        status_code,
        json=payload,
        request=httpx.Request("POST", "https://api.openai.com/v1/responses"),
    )


def test_structured_response_returns_data_and_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    post = Mock(
        return_value=_response(
            {
                "status": "completed",
                "output": [{"type": "message", "content": [{"type": "output_text", "text": '{"value":7}'}]}],
                "usage": {"input_tokens": 11, "output_tokens": 4},
            }
        )
    )
    monkeypatch.setattr(httpx, "post", post)

    data, metadata = OpenAIStructuredClient("test-key").generate_structured(
        "short prompt", Answer, "gpt-4o-mini", ()
    )

    assert data == {"value": 7}
    assert metadata == {"input_tokens": 11, "output_tokens": 4}
    assert post.call_count == 1
    request = post.call_args.kwargs["json"]
    assert request["text"]["format"]["type"] == "json_schema"
    assert request["text"]["format"]["strict"] is True


def test_content_planning_schema_is_normalized_for_strict_outputs(monkeypatch: pytest.MonkeyPatch) -> None:
    post = Mock(
        return_value=_response(
            {
                "status": "completed",
                "output": [{"type": "message", "content": [{"type": "output_text", "text": '{"value":7}'}]}],
            }
        )
    )
    monkeypatch.setattr(httpx, "post", post)

    OpenAIStructuredClient("test-key").generate_structured(
        "short prompt", ContentPlanningDraft, "gpt-4o-mini", ()
    )

    schema = post.call_args.kwargs["json"]["text"]["format"]["schema"]
    assert schema["additionalProperties"] is False
    assert schema["required"] == list(schema["properties"])
    scene = schema["$defs"]["ScenePlanDraft"]
    assert scene["additionalProperties"] is False
    assert scene["required"] == list(scene["properties"])
    assert "default" not in scene["properties"]["source_asset_id"]
    assert "minItems" not in schema["properties"]["scenes"]


def test_images_are_not_sent_as_openai_urls(monkeypatch: pytest.MonkeyPatch) -> None:
    post = Mock()
    monkeypatch.setattr(httpx, "post", post)

    with pytest.raises(LLMProviderCallError, match="image input"):
        OpenAIStructuredClient("test-key").generate_structured(
            "prompt", Answer, "gpt-4o-mini", ["123"]
        )

    post.assert_not_called()


@pytest.mark.parametrize(
    "payload, error_type",
    [
        ({"status": "incomplete", "output": []}, LLMProviderCallError),
        ({"status": "completed", "output": [{"type": "message", "content": [{"type": "refusal"}]}]}, LLMResponseFormatError),
    ],
)
def test_incomplete_or_refused_response_is_sanitized(
    monkeypatch: pytest.MonkeyPatch, payload: dict[str, object], error_type: type[Exception]
) -> None:
    monkeypatch.setattr(httpx, "post", Mock(return_value=_response(payload)))

    with pytest.raises(error_type) as error:
        OpenAIStructuredClient("test-key").generate_structured("prompt", Answer, "gpt-4o-mini", ())

    assert "test-key" not in str(error.value)


def test_http_error_is_sanitized(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(httpx, "post", Mock(return_value=_response({"error": "sensitive"}, 429)))

    with pytest.raises(LLMProviderCallError) as error:
        OpenAIStructuredClient("test-key").generate_structured("prompt", Answer, "gpt-4o-mini", ())

    assert str(error.value) == "OpenAI request failed"


def test_missing_api_key_is_rejected_before_request() -> None:
    with pytest.raises(ValueError, match="not configured"):
        OpenAIStructuredClient(None)


def test_real_mode_registers_openai_client(monkeypatch: pytest.MonkeyPatch) -> None:
    from app import main

    monkeypatch.setattr(main.settings, "e2e_fake_providers", False)
    monkeypatch.setattr(main.settings, "openai_api_key", "test-key")

    main._configure_default_providers()

    provider = main.get_llm_provider()
    assert isinstance(provider, MultiVendorLLMProvider)
    assert isinstance(provider._clients[LLMProviderType.OPENAI], OpenAIStructuredClient)


def test_fake_mode_stays_prioritized(monkeypatch: pytest.MonkeyPatch) -> None:
    from app import main

    monkeypatch.setattr(main.settings, "e2e_fake_providers", True)
    monkeypatch.setattr(main.settings, "openai_api_key", "test-key")

    main._configure_default_providers()

    assert isinstance(main.get_llm_provider(), DeterministicE2ELLMProvider)
