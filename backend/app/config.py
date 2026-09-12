from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

from app.llm_provider import LLMProviderType


# VORA Backend에서 사용할 환경변수 정의
class Settings(BaseSettings):
    # PostgreSQL 접속 정보
    db_host: str
    db_port: int
    db_name: str
    db_user: str
    db_password: str
    llm_provider: LLMProviderType = LLMProviderType.OPENAI
    content_planning_model: str = "gpt-4o-mini"
    script_generation_model: str = "gpt-4o-mini"
    revision_impact_model: str = "gpt-4o-mini"
    fixed_bgm_asset_id: int | None = None
    uploads_root: Path = Path("uploads")
    runway_api_key: str | None = None
    elevenlabs_api_key: str | None = None
    elevenlabs_voice_id: str | None = None
    elevenlabs_model: str = "eleven_multilingual_v2"

    # backend/.env 파일에서 값을 읽도록 설정
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
    )


# 애플리케이션 전체에서 사용할 설정 객체
settings = Settings()  # type: ignore[call-arg]  # Values are loaded by BaseSettings.
