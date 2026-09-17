import importlib.util
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest


@pytest.fixture
def verification_script() -> ModuleType:
    path = Path(__file__).parents[1] / "scripts" / "verify_elevenlabs_tts.py"
    spec = importlib.util.spec_from_file_location("verify_elevenlabs_tts", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_missing_configuration_stops_before_tts_request(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], verification_script: ModuleType
) -> None:
    monkeypatch.setattr(
        verification_script,
        "settings",
        SimpleNamespace(elevenlabs_api_key=None, elevenlabs_voice_id=None, elevenlabs_model="model"),
    )

    assert verification_script.main() == 1
    assert capsys.readouterr().out == "not_run reason=elevenlabs_configuration_missing\n"


def test_tts_verification_makes_one_request_and_reports_only_file_metadata(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    verification_script: ModuleType,
) -> None:
    calls: list[tuple[str, Path]] = []

    class FakeClient:
        def __init__(self, api_key: str, voice_id: str, model: str) -> None:
            assert (api_key, voice_id, model) == ("key", "voice", "model")

        def synthesize(self, narration: str, output_path: Path) -> tuple[list[dict[str, object]], dict[str, object]]:
            calls.append((narration, output_path))
            output_path.write_bytes(b"mp3")
            return [{"start": 0, "end": 1}], {}

    monkeypatch.setattr(
        verification_script,
        "settings",
        SimpleNamespace(elevenlabs_api_key="key", elevenlabs_voice_id="voice", elevenlabs_model="model"),
    )
    monkeypatch.setattr(verification_script, "ElevenLabsHTTPClient", FakeClient)
    monkeypatch.setattr(verification_script, "_OUTPUT_ROOT", tmp_path)

    assert verification_script.main() == 0
    assert calls == [("안녕하세요. 만들어보라에서 생성한 음성입니다.", next(tmp_path.glob("*.mp3")))]
    output = capsys.readouterr().out
    assert "elevenlabs_tts_succeeded" in output
    assert "byte_size=3" in output
    assert "timestamps_present=True" in output
    assert "key" not in output and "voice" not in output


def test_tts_request_error_does_not_retry_or_expose_details(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path, verification_script: ModuleType
) -> None:
    calls = 0

    class FailingClient:
        def __init__(self, api_key: str, voice_id: str, model: str) -> None:
            pass

        def synthesize(self, narration: str, output_path: Path) -> tuple[list[dict[str, object]], dict[str, object]]:
            nonlocal calls
            calls += 1
            raise OSError("provider response must remain private")

    monkeypatch.setattr(
        verification_script,
        "settings",
        SimpleNamespace(elevenlabs_api_key="key", elevenlabs_voice_id="voice", elevenlabs_model="model"),
    )
    monkeypatch.setattr(verification_script, "ElevenLabsHTTPClient", FailingClient)
    monkeypatch.setattr(verification_script, "_OUTPUT_ROOT", tmp_path)

    assert verification_script.main() == 1
    assert calls == 1
    assert capsys.readouterr().out == "elevenlabs_tts_failed\n"
