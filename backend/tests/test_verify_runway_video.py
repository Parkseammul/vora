import importlib.util
import json
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Self
from unittest.mock import Mock
from urllib.error import HTTPError

import pytest

from app.media_providers import (
    RunwayHTTPClient,
    RunwayOperationError,
    RunwayTaskTimeoutError,
)
from app.video_generation import GeneratedSceneVideo


class _Response:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        return None

    def read(self) -> bytes:
        return self._payload


@pytest.fixture
def verification_script() -> ModuleType:
    path = Path(__file__).parents[1] / "scripts" / "verify_runway_video.py"
    spec = importlib.util.spec_from_file_location("verify_runway_video", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_runway_client_uses_seedance_text_to_video_contract(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    responses = iter(
        [
            _Response(b'{"id":"task-1"}'),
            _Response(b'{"status":"SUCCEEDED","output":["https://download.example/video.mp4"]}'),
            _Response(b"mp4"),
        ]
    )
    urlopen = Mock(side_effect=lambda *args, **kwargs: next(responses))
    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    output_path = tmp_path / "video.mp4"

    metadata = RunwayHTTPClient("test-key", task_poll_max_attempts=1).generate(
        "prompt", "seedance2_5", 4, None, output_path
    )

    request = urlopen.call_args_list[0].args[0]
    assert request.full_url == "https://api.dev.runwayml.com/v1/text_to_video"
    assert request.get_header("X-runway-version") == "2024-11-06"
    assert json.loads(request.data) == {
        "model": "seedance2_5",
        "promptText": "prompt",
        "ratio": "720:1280",
        "duration": 4,
    }
    assert urlopen.call_count == 3
    assert output_path.read_bytes() == b"mp4"
    assert metadata == {"task_id": "task-1", "model": "seedance2_5"}


def test_runway_timeout_does_not_create_a_second_generation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    responses = iter([_Response(b'{"id":"task-1"}'), _Response(b'{"status":"RUNNING"}')])
    urlopen = Mock(side_effect=lambda *args, **kwargs: next(responses))
    monkeypatch.setattr("urllib.request.urlopen", urlopen)

    with pytest.raises(RunwayTaskTimeoutError) as error:
        RunwayHTTPClient("test-key", task_poll_max_attempts=1).generate(
            "prompt", "seedance2_5", 4, None, tmp_path / "video.mp4"
        )

    assert (error.value.task_id, error.value.status) == ("task-1", "RUNNING")
    assert urlopen.call_count == 2


def test_runway_http_error_diagnostics_preserve_only_safe_context(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    post_error = HTTPError("https://sensitive.example", 401, "", None, None)
    urlopen = Mock(side_effect=post_error)
    monkeypatch.setattr("urllib.request.urlopen", urlopen)

    with pytest.raises(RunwayOperationError) as error:
        RunwayHTTPClient("test-key").generate("prompt", "seedance2_5", 4, None, tmp_path / "video.mp4")

    assert (error.value.phase, error.value.exception_type, error.value.http_status) == (
        "generation_request",
        "HTTPError",
        401,
    )
    assert error.value.task_id is None


def test_runway_status_and_download_errors_keep_existing_task_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    status_error = HTTPError("https://sensitive.example", 503, "", None, None)
    responses = iter([_Response(b'{"id":"task-1"}'), status_error])
    monkeypatch.setattr("urllib.request.urlopen", Mock(side_effect=responses))

    with pytest.raises(RunwayOperationError) as status_failure:
        RunwayHTTPClient("test-key").generate("prompt", "seedance2_5", 4, None, tmp_path / "video.mp4")

    assert (status_failure.value.phase, status_failure.value.http_status, status_failure.value.task_id) == (
        "task_status",
        503,
        "task-1",
    )

    download_error = HTTPError("https://sensitive.example", 502, "", None, None)
    responses = iter(
        [
            _Response(b'{"id":"task-2"}'),
            _Response(b'{"status":"SUCCEEDED","output":["https://sensitive.example/video.mp4"]}'),
            download_error,
        ]
    )
    monkeypatch.setattr("urllib.request.urlopen", Mock(side_effect=responses))

    with pytest.raises(RunwayOperationError) as download_failure:
        RunwayHTTPClient("test-key").generate("prompt", "seedance2_5", 4, None, tmp_path / "video.mp4")

    assert (
        download_failure.value.phase,
        download_failure.value.http_status,
        download_failure.value.task_id,
        download_failure.value.task_status,
    ) == ("download", 502, "task-2", "SUCCEEDED")


def test_missing_configuration_stops_before_video_request(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], verification_script: ModuleType
) -> None:
    monkeypatch.setattr(verification_script, "settings", SimpleNamespace(runway_api_key=None))

    assert verification_script.main() == 1
    assert capsys.readouterr().out == "not_run reason=runway_configuration_missing\n"


def test_preflight_failure_reports_only_its_safe_exception_type(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], verification_script: ModuleType
) -> None:
    monkeypatch.setattr(verification_script, "settings", SimpleNamespace(runway_api_key="key"))
    monkeypatch.setattr(verification_script, "_RATIO", "invalid")

    assert verification_script.main() == 1
    output = capsys.readouterr().out
    assert output == "runway_video_failed phase=preflight exception_type=ValueError\n"
    assert "key" not in output


def test_runway_timeout_preserves_task_for_reconciliation(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], verification_script: ModuleType
) -> None:
    class TimeoutProvider:
        _MODEL = "seedance2_5"

        def __init__(self, client: object) -> None:
            pass

        def generate_scene(
            self, prompt: str, duration_seconds: float, output_path: Path, reference_image: Path | None
        ) -> GeneratedSceneVideo:
            raise RunwayTaskTimeoutError("task-1", "RUNNING")

    monkeypatch.setattr(verification_script, "settings", SimpleNamespace(runway_api_key="key"))
    monkeypatch.setattr(verification_script, "RunwayHTTPClient", Mock())
    monkeypatch.setattr(verification_script, "RunwayVideoProvider", TimeoutProvider)

    assert verification_script.main() == 1
    assert capsys.readouterr().out == "runway_video_timed_out task_id=task-1 task_status=RUNNING\n"


def test_runway_script_outputs_safe_generation_diagnostic(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], verification_script: ModuleType
) -> None:
    class FailingProvider:
        _MODEL = "seedance2_5"

        def __init__(self, client: object) -> None:
            pass

        def generate_scene(
            self, prompt: str, duration_seconds: float, output_path: Path, reference_image: Path | None
        ) -> GeneratedSceneVideo:
            raise RunwayOperationError("generation_request", "HTTPError", http_status=401)

    monkeypatch.setattr(verification_script, "settings", SimpleNamespace(runway_api_key="key"))
    monkeypatch.setattr(verification_script, "RunwayHTTPClient", Mock())
    monkeypatch.setattr(verification_script, "RunwayVideoProvider", FailingProvider)

    assert verification_script.main() == 1
    output = capsys.readouterr().out
    assert output == "runway_video_failed phase=generation_request exception_type=HTTPError http_status=401\n"
    assert "key" not in output


def test_video_verification_makes_one_generation_request_and_reports_file_metadata(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    verification_script: ModuleType,
) -> None:
    client = Mock()
    providers: list[object] = []

    class FakeProvider:
        _MODEL = "seedance2_5"

        def __init__(self, received_client: object) -> None:
            assert received_client is client
            providers.append(received_client)

        def generate_scene(
            self, prompt: str, duration_seconds: float, output_path: Path, reference_image: Path | None
        ) -> GeneratedSceneVideo:
            assert prompt == verification_script._PROMPT
            assert duration_seconds == 4
            assert reference_image is None
            output_path.write_bytes(b"mp4")
            return GeneratedSceneVideo(output_path, {"task_id": "private"})

    monkeypatch.setattr(verification_script, "settings", SimpleNamespace(runway_api_key="key"))
    monkeypatch.setattr(verification_script, "RunwayHTTPClient", lambda *args, **kwargs: client)
    monkeypatch.setattr(verification_script, "RunwayVideoProvider", FakeProvider)
    monkeypatch.setattr(verification_script, "_OUTPUT_ROOT", tmp_path)

    assert verification_script.main() == 0
    assert providers == [client]
    output = capsys.readouterr().out
    assert "runway_video_succeeded" in output
    assert "byte_size=3" in output
    assert "task_status=SUCCEEDED" in output
    assert "key" not in output and "private" not in output
