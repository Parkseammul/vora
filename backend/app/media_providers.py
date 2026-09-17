import base64
import json
import subprocess
import time
import urllib.request
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol
from urllib.error import HTTPError

from app.video_generation import (
    CompositionScene,
    GeneratedSceneVideo,
    GeneratedSpeech,
    TTSProvider,
    VideoComposer,
    VideoProvider,
)


class RunwayClient(Protocol):
    def generate(self, prompt: str, model: str, duration_seconds: float, reference_image: Path | None, output_path: Path) -> dict[str, object]: ...


class ElevenLabsClient(Protocol):
    def synthesize(self, narration: str, output_path: Path) -> tuple[list[dict[str, object]], dict[str, object]]: ...


class RunwayTaskTimeoutError(TimeoutError):
    """Preserves only the safe reconciliation identifiers for an existing Runway task."""

    def __init__(self, task_id: str, status: str) -> None:
        super().__init__("Runway task timed out")
        self.task_id = task_id
        self.status = status


class RunwayOperationError(RuntimeError):
    """Safe diagnostic context for a Runway operation without provider payloads."""

    def __init__(
        self,
        phase: str,
        exception_type: str,
        http_status: int | None = None,
        task_id: str | None = None,
        task_status: str | None = None,
    ) -> None:
        super().__init__("Runway operation failed")
        self.phase = phase
        self.exception_type = exception_type
        self.http_status = http_status
        self.task_id = task_id
        self.task_status = task_status


class RunwayHTTPClient:
    """Runway task API client; polling and download stay inside the vendor adapter."""

    def __init__(
        self,
        api_key: str,
        task_poll_interval_seconds: float = 2.0,
        task_poll_max_attempts: int = 180,
    ) -> None:
        if task_poll_interval_seconds <= 0 or task_poll_max_attempts < 1:
            raise ValueError("Runway polling settings must be positive")
        self._api_key = api_key
        self._task_poll_interval_seconds = task_poll_interval_seconds
        self._task_poll_max_attempts = task_poll_max_attempts

    def generate(self, prompt: str, model: str, duration_seconds: float, reference_image: Path | None, output_path: Path) -> dict[str, object]:
        payload: dict[str, object] = {"model": model, "promptText": prompt, "ratio": "720:1280", "duration": max(4, round(duration_seconds))}
        endpoint = "text_to_video"
        if reference_image is not None:
            mime = "image/png" if reference_image.suffix.lower() == ".png" else "image/jpeg"
            encoded = base64.b64encode(reference_image.read_bytes()).decode()
            payload["promptImage"] = f"data:{mime};base64,{encoded}"
            endpoint = "image_to_video"
        try:
            task = self._request(f"/v1/{endpoint}", payload)
            task_id = str(task["id"])
        except Exception as exc:
            raise self._operation_error("generation_request", exc) from exc
        last_status = "UNKNOWN"
        for _ in range(self._task_poll_max_attempts):
            try:
                task = self._request(f"/v1/tasks/{task_id}")
            except Exception as exc:
                raise self._operation_error(
                    "task_status", exc, task_id, last_status
                ) from exc
            status = task.get("status")
            last_status = self._safe_task_status(status)
            if last_status == "SUCCEEDED":
                output = task.get("output", [])
                if not isinstance(output, list) or not output or not isinstance(output[0], str):
                    raise RunwayOperationError(
                        "download", "RunwayOutputMissing", task_id=task_id, task_status=last_status
                    )
                try:
                    output_path.write_bytes(urllib.request.urlopen(output[0], timeout=60).read())
                except Exception as exc:
                    raise self._operation_error(
                        "download", exc, task_id, last_status
                    ) from exc
                return {"task_id": task_id, "model": model}
            if last_status == "FAILED":
                raise RunwayOperationError(
                    "task_status", "RunwayTaskFailed", task_id=task_id, task_status=last_status
                )
            time.sleep(self._task_poll_interval_seconds)
        raise RunwayTaskTimeoutError(task_id, last_status)

    @staticmethod
    def _safe_task_status(value: object) -> str:
        if isinstance(value, str) and value in {
            "PENDING",
            "RUNNING",
            "THROTTLED",
            "SUCCEEDED",
            "FAILED",
            "CANCELLED",
        }:
            return value
        return "UNKNOWN"

    @staticmethod
    def _operation_error(
        phase: str,
        exc: Exception,
        task_id: str | None = None,
        task_status: str | None = None,
    ) -> RunwayOperationError:
        return RunwayOperationError(
            phase,
            type(exc).__name__,
            exc.code if isinstance(exc, HTTPError) else None,
            task_id,
            task_status,
        )

    def _request(self, path: str, payload: dict[str, object] | None = None) -> dict[str, object]:
        request = urllib.request.Request("https://api.dev.runwayml.com" + path, data=json.dumps(payload).encode() if payload is not None else None, headers={"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json", "X-Runway-Version": "2024-11-06"})
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.loads(response.read())


class ElevenLabsHTTPClient:
    def __init__(self, api_key: str, voice_id: str, model: str) -> None:
        self._api_key, self._voice_id, self._model = api_key, voice_id, model

    def synthesize(self, narration: str, output_path: Path) -> tuple[list[dict[str, object]], dict[str, object]]:
        url = f"https://api.elevenlabs.io/v1/text-to-speech/{self._voice_id}/with-timestamps?output_format=mp3_44100_128"
        request = urllib.request.Request(url, data=json.dumps({"text": narration, "model_id": self._model}).encode(), headers={"xi-api-key": self._api_key, "Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=60) as response:
            payload = json.loads(response.read())
        output_path.write_bytes(base64.b64decode(payload["audio_base64"]))
        alignment = payload.get("alignment") or {}
        starts, ends = alignment.get("character_start_times_seconds", []), alignment.get("character_end_times_seconds", [])
        return ([{"start": starts[0], "end": ends[-1]}] if starts and ends else []), {"voice_id": self._voice_id, "model": self._model}


class RunwayVideoProvider(VideoProvider):
    """Keeps Seedance vendor model/API details behind the video adapter."""

    _MODEL = "seedance2_5"

    def __init__(self, client: RunwayClient) -> None:
        self._client = client

    def generate_scene(
        self, prompt: str, duration_seconds: float, output_path: Path, reference_image: Path | None
    ) -> GeneratedSceneVideo:
        return GeneratedSceneVideo(
            output_path,
            self._client.generate(prompt, self._MODEL, duration_seconds, reference_image, output_path),
        )


class ElevenLabsTTSProvider(TTSProvider):
    def __init__(self, client: ElevenLabsClient) -> None:
        self._client = client

    def synthesize(self, narration: str, output_path: Path) -> GeneratedSpeech:
        timestamps, metadata = self._client.synthesize(narration, output_path)
        return GeneratedSpeech(output_path, timestamps, metadata)


class FFmpegVideoComposer(VideoComposer):
    def __init__(self, subtitle_font_path: Path | None = None) -> None:
        self._subtitle_font_path = subtitle_font_path

    def compose(
        self, scenes: Sequence[CompositionScene], bgm_path: Path, output_path: Path
    ) -> None:
        clip_paths = [self._render_scene(scene, output_path.parent) for scene in scenes]
        concat_file = output_path.with_suffix(".txt")
        concat_file.write_text("".join(f"file '{path.resolve().as_posix()}'\n" for path in clip_paths))
        subprocess.run(
            [
                "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(concat_file),
                "-stream_loop", "-1", "-i", str(bgm_path), "-filter_complex",
                "[1:a]volume=0.15[bgm];[0:a][bgm]amix=inputs=2:duration=first[a]",
                "-map", "0:v", "-map", "[a]", "-shortest", "-c:v", "libx264", "-c:a", "aac", str(output_path),
            ],
            check=True,
        )

    def _render_scene(self, scene: CompositionScene, output_dir: Path) -> Path:
        clip_path = output_dir / f"{scene.scene_id}.composed.mp4"
        subtitle_filter = ""
        if scene.subtitle is not None:
            end = scene.timestamps[-1].get("end", scene.duration_seconds) if scene.timestamps else scene.duration_seconds
            text = self._escape_drawtext_value(scene.subtitle)
            font = (
                f":fontfile='{self._escape_drawtext_value(self._subtitle_font_path)}'"
                if self._subtitle_font_path is not None
                else ""
            )
            subtitle_filter = f",drawtext=text='{text}'{font}:x=(w-text_w)/2:y=h*0.82:enable='between(t,0,{end})'"
        command = ["ffmpeg", "-y", "-i", str(scene.video_path)]
        if scene.audio_path is not None:
            command += ["-i", str(scene.audio_path)]
        else:
            command += ["-f", "lavfi", "-t", str(scene.duration_seconds), "-i", "anullsrc=r=48000:cl=stereo"]
        command += [
            "-filter_complex", f"[0:v]scale=1080:1920{subtitle_filter}[v]",
            "-map", "[v]", "-map", "1:a", "-t", str(scene.duration_seconds),
            "-c:v", "libx264", "-c:a", "aac", str(clip_path),
        ]
        subprocess.run(command, check=True)
        return clip_path

    @staticmethod
    def _escape_drawtext_value(value: str | Path) -> str:
        """Escape filter separators; Windows drive colons require escaping for FFmpeg."""
        return str(value).replace("\\", "/").replace("'", r"\'").replace(":", r"\:")
