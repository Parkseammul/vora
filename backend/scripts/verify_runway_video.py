"""Make one approved Runway text-to-video request without a Workflow or database access."""

from datetime import datetime, timezone
from pathlib import Path

from app.config import settings
from app.media_providers import (
    RunwayHTTPClient,
    RunwayOperationError,
    RunwayTaskTimeoutError,
    RunwayVideoProvider,
)

_MODEL = "seedance2_5"
_RATIO = "720:1280"
_DURATION_SECONDS = 4.0
_POLL_INTERVAL_SECONDS = 5.0
_POLL_MAX_ATTEMPTS = 60
_OUTPUT_ROOT = Path("uploads") / "local-e2e"
_PROMPT = "A calm vertical product video of a reusable tumbler on a sunlit desk, gentle camera movement."


def _validate_request(output_path: Path) -> None:
    if RunwayVideoProvider._MODEL != _MODEL:
        raise ValueError("Unexpected Runway model")
    if _RATIO != "720:1280":
        raise ValueError("Unexpected Runway output ratio")
    if not 4 <= _DURATION_SECONDS <= 30 or not _DURATION_SECONDS.is_integer():
        raise ValueError("Runway duration must be a whole number from 4 to 30 seconds")
    if output_path.suffix.lower() != ".mp4" or not output_path.is_relative_to(_OUTPUT_ROOT):
        raise ValueError("Runway output path must be an MP4 under the local E2E directory")


def _diagnostic_line(
    phase: str,
    exception_type: str,
    http_status: int | None = None,
    task_id: str | None = None,
    task_status: str | None = None,
) -> str:
    parts = [f"runway_video_failed phase={phase}", f"exception_type={exception_type}"]
    if http_status is not None:
        parts.append(f"http_status={http_status}")
    if task_id is not None:
        parts.append(f"task_id={task_id}")
    if task_status is not None:
        parts.append(f"task_status={task_status}")
    return " ".join(parts)


def main() -> int:
    if not settings.runway_api_key:
        print("not_run reason=runway_configuration_missing")
        return 1
    output_path = _OUTPUT_ROOT / (
        "runway-video-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + ".mp4"
    )
    phase = "preflight"
    try:
        _validate_request(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        phase = "generation_request"
        generated = RunwayVideoProvider(
            RunwayHTTPClient(
                settings.runway_api_key,
                task_poll_interval_seconds=_POLL_INTERVAL_SECONDS,
                task_poll_max_attempts=_POLL_MAX_ATTEMPTS,
            )
        ).generate_scene(_PROMPT, _DURATION_SECONDS, output_path, None)
    except RunwayTaskTimeoutError as exc:
        # The task already exists. Do not create another generation from this timeout path.
        print(f"runway_video_timed_out task_id={exc.task_id} task_status={exc.status}")
        return 1
    except RunwayOperationError as exc:
        print(
            _diagnostic_line(
                exc.phase, exc.exception_type, exc.http_status, exc.task_id, exc.task_status
            )
        )
        return 1
    except Exception as exc:  # noqa: BLE001 - provider failures must not leak local credentials, URLs, or bodies.
        print(_diagnostic_line(phase, type(exc).__name__))
        return 1

    if not generated.path.is_file() or generated.path.stat().st_size == 0:
        task_id = generated.metadata.get("task_id")
        print(
            _diagnostic_line(
                "download", "OutputFileMissing", task_id=str(task_id) if task_id else None,
                task_status="SUCCEEDED",
            )
        )
        return 1
    print(
        "runway_video_succeeded "
        f"mp4_path={generated.path} byte_size={generated.path.stat().st_size} task_status=SUCCEEDED"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
