"""Make one approved, text-only OpenAI content-planning request without a Workflow.

This script does not create a DB Session, Workflow, media asset, Publication,
or SNS request. It sends exactly one OpenAI request when all preconditions pass.
"""

from decimal import Decimal

from app.config import settings
from app.content_planning import ContentPlanningService
from app.llm_provider import LLMError, LLMProviderType, MultiVendorLLMProvider
from app.openai_client import OpenAIStructuredClient

_REQUEST_TEXT = "20대 직장인을 위한 텀블러를 소개하는 15초 쇼츠"


def main() -> int:
    if settings.e2e_fake_providers:
        print("not_run reason=fake_provider_mode")
        return 1
    if settings.llm_provider is not LLMProviderType.OPENAI or not settings.openai_api_key:
        print("not_run reason=openai_configuration_missing")
        return 1
    try:
        provider = MultiVendorLLMProvider(
            {LLMProviderType.OPENAI: OpenAIStructuredClient(settings.openai_api_key)}
        )
        result = ContentPlanningService(
            provider, LLMProviderType.OPENAI, settings.content_planning_model
        ).generate(
            {
                "request_text": _REQUEST_TEXT,
                "duration_seconds": 15,
                "source_asset_ids": [],
            }
        )
    except (LLMError, ValueError):
        # The provider boundary deliberately keeps request and response bodies private.
        print("content_planning_failed")
        return 1

    total_duration = sum((Decimal(str(scene.duration_seconds)) for scene in result.data.scenes), Decimal())
    print(
        "content_planning_succeeded "
        f"concept={result.data.concept} hook={result.data.hook} "
        f"scene_count={len(result.data.scenes)} total_duration={total_duration} "
        f"input_tokens={result.metadata.input_tokens} output_tokens={result.metadata.output_tokens}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
