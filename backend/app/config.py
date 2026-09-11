# .env 파일의 환경변수를 읽고 검증하기 위한 BaseSettings
from pydantic_settings import BaseSettings, SettingsConfigDict


# VORA Backend에서 사용할 환경변수 정의
class Settings(BaseSettings):
    # PostgreSQL 접속 정보
    db_host: str
    db_port: int
    db_name: str
    db_user: str
    db_password: str

    # backend/.env 파일에서 값을 읽도록 설정
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
    )


# 애플리케이션 전체에서 사용할 설정 객체
settings = Settings()  # type: ignore[call-arg]  # Values are loaded by BaseSettings.
