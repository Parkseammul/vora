"""Offline providers used only when VORA_E2E_FAKE_PROVIDERS is explicitly enabled."""

import json
import subprocess
from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel

from app.content_planning import ContentPlanningDraft
from app.llm_provider import LLMMetadata, LLMProviderType, LLMResult
from app.publication_copy import PublicationCopyDraft
from app.revision_impact import CoreNodeKey, RevisionImpactResult
from app.script_generation import ScriptGenerationResult
from app.video_generation import GeneratedSceneVideo, GeneratedSpeech

ResponseT = TypeVar("ResponseT", bound=BaseModel)


class DeterministicE2ELLMProvider:
    """Produces contract-valid, local data without sending a request to an LLM vendor."""

    def generate_structured(
        self,
        prompt: str,
        response_model: type[ResponseT],
        provider: LLMProviderType,
        model: str,
        images: Sequence[str] = (),
    ) -> LLMResult:
        payload = self._payload(prompt, response_model, images)
        return LLMResult(
            data=response_model.model_validate(payload),
            metadata=LLMMetadata(provider=provider, model="e2e-fake"),
        )

    def _payload(
        self, prompt: str, response_model: type[ResponseT], images: Sequence[str]
    ) -> dict[str, Any]:
        if response_model is ContentPlanningDraft:
            analysis = self._input_after(prompt, "Input: ")
            source_asset_ids = [int(asset_id) for asset_id in images]
            durations = self._durations(
                Decimal(str(analysis["duration_seconds"])), len(source_asset_ids) or 1
            )
            is_revision = "Revision request:" in prompt
            content = (
                {
                    "concept": "야간 러닝, 나만의 페이스를 찾는 E2E 콘셉트",
                    "hook": "도시의 불빛 아래, 오늘도 나만의 페이스로 달립니다.",
                    "key_message": "기록 경쟁보다 중요한 것은 나만의 페이스입니다.",
                    "cta": "오늘 밤, 나만의 페이스로 한 걸음 더 나아가세요.",
                    "visual_style": "감성적인 야간 러닝과 네온 조명의 세로형 영상",
                    "bgm_direction": "차분하게 고조되는 야간 러닝 리듬",
                    "description": "야간 도심의 조명 아래 러너가 나만의 페이스로 달린다.",
                    "visual_direction": "moody night running, cinematic neon light",
                }
                if is_revision
                else self._initial_planning_content(str(analysis.get("request_text", "")))
            )
            return {
                **{
                    key: value
                    for key, value in content.items()
                    if key not in {"description", "visual_direction"}
                },
                "scenes": [
                    {
                        "purpose": f"E2E scene {index + 1}",
                        "main_objects": ["VORA"],
                        "description": content["description"],
                        "duration_seconds": float(durations[index]),
                        "source_asset_id": source_asset_ids[index] if source_asset_ids else None,
                        "visual_direction": content["visual_direction"],
                        "transition_to_next": "CUT" if index < len(durations) - 1 else None,
                    }
                    for index in range(len(durations))
                ],
            }
        if response_model is ScriptGenerationResult:
            planning = self._input_after(prompt, "Planning: ")
            return {
                "scenes": [
                    {
                        "planning_scene_id": scene["scene_id"],
                        "narration": "VORA와 함께 시작하세요.",
                        "subtitle": "VORA와 함께 시작하세요",
                        "speaking_style": "밝고 자연스럽게",
                        "emphasis_keywords": ["VORA"],
                    }
                    for scene in planning["scenes"]
                ]
            }
        if response_model is PublicationCopyDraft:
            publication_input = self._input_after(prompt, "Input: ")
            publication_planning = publication_input.get("planning")
            if not isinstance(publication_planning, dict):
                raise TypeError("Expected planning object in publication copy input")
            concept = str(publication_planning.get("concept") or "VORA marketing video")
            message = str(publication_planning.get("key_message") or concept)
            return {
                "youtube_title": concept[:100],
                "youtube_description": message,
                "instagram_caption": message,
            }
        if response_model is RevisionImpactResult:
            request = str(self._input_after(prompt, "Input: ")["revision_request"]).lower()
            if any(
                word in request
                for word in (
                    "콘셉트",
                    "기획",
                    "concept",
                    "planning",
                    "핵심 메시지",
                    "전체",
                    "영상 방향",
                    "타깃",
                )
            ):
                target = CoreNodeKey.CONTENT_PLANNING
            elif any(word in request for word in ("대본", "script", "문장", "내레이션", "자막")):
                target = CoreNodeKey.SCRIPT_GENERATION
            elif any(word in request for word in ("영상", "video", "장면")):
                target = CoreNodeKey.VIDEO_GENERATION
            else:
                target = CoreNodeKey.SCRIPT_GENERATION
            return {"target_node": target.value}
        raise ValueError(f"E2E fake provider does not support {response_model.__name__}")

    @staticmethod
    def _initial_planning_content(request_text: str) -> dict[str, str]:
        request = request_text.lower()
        if "러닝" in request or "running" in request:
            return {
                "concept": "러닝화 광고를 위한 감성적인 야간 러닝",
                "hook": "야간 러닝의 첫 발걸음, 러닝화가 바꿉니다.",
                "key_message": "러닝화와 함께 더 편안한 나만의 러닝을 시작하세요.",
                "cta": "오늘 밤 러닝화를 신고 달려보세요.",
                "visual_style": "감성적인 야간 러닝과 도시 조명의 세로형 광고 영상",
                "bgm_direction": "리듬감 있는 야간 러닝 비트",
                "description": "야간 도심을 달리는 러너와 러닝화를 감성적으로 보여준다.",
                "visual_direction": "cinematic night running, warm city lights",
            }
        return {
            "concept": "E2E 데모 콘셉트",
            "hook": "처음 3초, 제품의 차이를 보여드립니다.",
            "key_message": "간단한 선택이 더 나은 하루를 만듭니다.",
            "cta": "지금 VORA로 다음 영상을 만들어보세요.",
            "visual_style": "밝고 선명한 세로형 광고 영상",
            "bgm_direction": "경쾌하고 가벼운 리듬",
            "description": "밝은 배경 위에 제품과 핵심 메시지를 보여준다.",
            "visual_direction": "clean vertical composition",
        }

    @staticmethod
    def _input_after(prompt: str, marker: str) -> dict[str, Any]:
        fragment = prompt.split(marker, maxsplit=1)[1]
        value, _ = json.JSONDecoder().raw_decode(fragment)
        if not isinstance(value, dict):
            raise TypeError("Expected object input in E2E fake prompt")
        return value

    @staticmethod
    def _durations(total: Decimal, count: int) -> list[Decimal]:
        tenth = Decimal("0.1")
        units = int(total / tenth)
        base, remainder = divmod(units, count)
        return [tenth * (base + (1 if index < remainder else 0)) for index in range(count)]


class FFmpegE2EVideoProvider:
    """Creates valid local scene MP4 files for the real composition pipeline."""

    def generate_scene(
        self, prompt: str, duration_seconds: float, output_path: Path, reference_image: Path | None
    ) -> GeneratedSceneVideo:
        subprocess.run(
            [
                "ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=0x4f46e5:s=320x568",
                "-t", str(duration_seconds), "-c:v", "libx264", "-pix_fmt", "yuv420p", str(output_path),
            ],
            check=True,
            capture_output=True,
        )
        return GeneratedSceneVideo(output_path, {"provider": "e2e-fake-video"})


class FFmpegE2ETTSProvider:
    """Creates a valid local MP3; no text is sent to a speech provider."""

    def synthesize(self, narration: str, output_path: Path) -> GeneratedSpeech:
        subprocess.run(
            [
                "ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
                "-t", "1", "-f", "mp3", str(output_path),
            ],
            check=True,
            capture_output=True,
        )
        return GeneratedSpeech(output_path, [{"start": 0, "end": 1}], {"provider": "e2e-fake-tts"})
