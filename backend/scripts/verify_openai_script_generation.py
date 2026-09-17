"""Make one approved, text-only OpenAI script-generation request without a Workflow.

The fixed plan below is not loaded from the DB and the script never creates a
DB Session, Workflow, media asset, Publication, or SNS request. It makes at
most one OpenAI request when all preconditions pass.
"""

from app.config import settings
from app.content_planning import ContentPlanningResult
from app.llm_provider import LLMError, LLMProviderType, MultiVendorLLMProvider
from app.openai_client import OpenAIStructuredClient
from app.script_generation import ScriptGenerationService


def _fixed_korean_planning() -> ContentPlanningResult:
    return ContentPlanningResult.model_validate(
        {
            "concept": "20대 직장인을 위한 텀블러 소개 쇼츠",
            "hook": "출근길, 텀블러 하나로 달라집니다.",
            "key_message": "가볍고 실용적인 텀블러를 소개합니다.",
            "cta": "나에게 맞는 텀블러를 골라보세요.",
            "visual_style": "밝고 간결한 제품 중심 화면",
            "bgm_direction": "경쾌하고 산뜻한 리듬",
            "scenes": [
                {
                    "scene_id": "e2e-scene-1",
                    "purpose": "hook",
                    "main_objects": ["텀블러"],
                    "description": "출근 준비 중 텀블러를 드는 장면",
                    "duration_seconds": 5,
                    "source_asset_id": None,
                    "visual_direction": "손과 제품을 가까이 보여주는 화면",
                    "transition_to_next": "CUT",
                },
                {
                    "scene_id": "e2e-scene-2",
                    "purpose": "benefit",
                    "main_objects": ["텀블러", "커피"],
                    "description": "가방에 넣어도 편한 텀블러를 보여주는 장면",
                    "duration_seconds": 5,
                    "source_asset_id": None,
                    "visual_direction": "제품 특징을 빠르게 보여주는 화면",
                    "transition_to_next": "CUT",
                },
                {
                    "scene_id": "e2e-scene-3",
                    "purpose": "cta",
                    "main_objects": ["텀블러"],
                    "description": "텀블러를 들고 출근하는 마무리 장면",
                    "duration_seconds": 5,
                    "source_asset_id": None,
                    "visual_direction": "밝은 표정과 제품을 함께 보여주는 화면",
                    "transition_to_next": None,
                },
            ],
        }
    )


def _contains_korean(text: str) -> bool:
    return any("가" <= character <= "힣" for character in text)


def main() -> int:
    if settings.e2e_fake_providers:
        print("not_run reason=fake_provider_mode")
        return 1
    if settings.llm_provider is not LLMProviderType.OPENAI or not settings.openai_api_key:
        print("not_run reason=openai_configuration_missing")
        return 1

    planning = _fixed_korean_planning()
    expected_ids = [scene.scene_id for scene in planning.scenes]
    try:
        result = ScriptGenerationService(
            MultiVendorLLMProvider(
                {LLMProviderType.OPENAI: OpenAIStructuredClient(settings.openai_api_key)}
            ),
            LLMProviderType.OPENAI,
            settings.script_generation_model,
        ).generate(planning.model_dump(mode="json"))
    except (LLMError, ValueError):
        # The provider boundary deliberately keeps request and response bodies private.
        print("script_generation_failed")
        return 1

    scripts = result.data.scenes
    has_korean_script = (
        [scene.planning_scene_id for scene in scripts] == expected_ids
        and len(scripts) == 3
        and all(scene.narration and scene.subtitle for scene in scripts)
        and all(
            _contains_korean(scene.narration or "") and _contains_korean(scene.subtitle or "")
            for scene in scripts
            if scene.narration is not None and scene.subtitle is not None
        )
    )
    if not has_korean_script:
        print("script_generation_failed")
        return 1
    print(
        "script_generation_succeeded "
        f"scene_count={len(scripts)} korean_script_generated=True "
        f"input_tokens={result.metadata.input_tokens} output_tokens={result.metadata.output_tokens}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
