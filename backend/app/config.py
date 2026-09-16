from pathlib import Path

from pydantic import Field
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
    publication_copy_model: str = "gpt-4o-mini"
    fixed_bgm_asset_id: int | None = None
    uploads_root: Path = Path("uploads")
    runway_api_key: str | None = None
    elevenlabs_api_key: str | None = None
    elevenlabs_voice_id: str | None = None
    elevenlabs_model: str = "eleven_multilingual_v2"
    redis_url: str = "redis://localhost:6379/0"
    video_generation_queue: str = "video_generation"
    publication_queue: str = "publication"
    storage_provider: str = "local"
    s3_bucket: str | None = None
    s3_region: str | None = None
    youtube_client_id: str | None = None
    youtube_client_secret: str | None = None
    youtube_redirect_uri: str | None = None
    instagram_client_id: str | None = None
    instagram_client_secret: str | None = None
    instagram_redirect_uri: str | None = None
    # Keep the Instagram Login Graph API contract pinned rather than relying on
    # Meta's unversioned endpoint. Operators must update this deliberately.
    instagram_graph_api_version: str = "v24.0"
    instagram_container_poll_interval_seconds: float = 5.0
    instagram_container_poll_max_attempts: int = 24
    instagram_presigned_url_ttl_seconds: int = 3600
    frontend_url: str = "http://localhost:5173"
    e2e_fake_providers: bool = Field(False, validation_alias="VORA_E2E_FAKE_PROVIDERS")
    ffmpeg_subtitle_font_path: Path | None = Field(
        None, validation_alias="FFMPEG_SUBTITLE_FONT_PATH"
    )

    # backend/.env 파일에서 값을 읽도록 설정
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
    )


# 애플리케이션 전체에서 사용할 설정 객체
settings = Settings()  # type: ignore[call-arg]  # Values are loaded by BaseSettings.
