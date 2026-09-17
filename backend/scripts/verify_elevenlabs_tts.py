"""Make one approved ElevenLabs TTS request without a Workflow or database access."""

from datetime import datetime, timezone
from pathlib import Path

from app.config import settings
from app.media_providers import ElevenLabsHTTPClient

_TEXT = "안녕하세요. 만들어보라에서 생성한 음성입니다."
_OUTPUT_ROOT = Path("uploads") / "local-e2e"


def main() -> int:
    if not settings.elevenlabs_api_key or not settings.elevenlabs_voice_id:
        print("not_run reason=elevenlabs_configuration_missing")
        return 1
    if not settings.elevenlabs_model:
        print("not_run reason=elevenlabs_model_missing")
        return 1

    output_path = _OUTPUT_ROOT / (
        "elevenlabs-tts-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + ".mp3"
    )
    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        timestamps, _ = ElevenLabsHTTPClient(
            settings.elevenlabs_api_key,
            settings.elevenlabs_voice_id,
            settings.elevenlabs_model,
        ).synthesize(_TEXT, output_path)
    except Exception:  # noqa: BLE001 - provider failures must not leak local credentials or URLs.
        # Do not expose provider payloads or credentials from this local verification command.
        print("elevenlabs_tts_failed")
        return 1

    if not output_path.is_file() or output_path.stat().st_size == 0:
        print("elevenlabs_tts_failed")
        return 1
    print(
        "elevenlabs_tts_succeeded "
        f"mp3_path={output_path} byte_size={output_path.stat().st_size} "
        f"timestamps_present={bool(timestamps)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
