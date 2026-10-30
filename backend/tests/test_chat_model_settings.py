import pytest

from app import main


def test_chat_model_settings_default_preserves_existing_behavior(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_MAX_RETRIES", raising=False)

    assert main._chat_model_settings() == {}


def test_chat_model_settings_supports_zero_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_MAX_RETRIES", "0")

    assert main._chat_model_settings() == {"max_retries": 0}


@pytest.mark.parametrize("value", ["-1", "not-an-integer"])
def test_chat_model_settings_rejects_invalid_values(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("OPENAI_MAX_RETRIES", value)

    with pytest.raises(ValueError, match="OPENAI_MAX_RETRIES"):
        main._chat_model_settings()
