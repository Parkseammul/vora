# SQLAlchemy에서 PostgreSQL 연결을 만들기 위한 함수
from sqlalchemy import create_engine

# .env에서 읽어온 DB 설정값
from app.config import settings

# PostgreSQL 연결 주소 생성
# 형태:
# postgresql+psycopg://사용자:비밀번호@주소:포트/DB이름
DATABASE_URL = (
    f"postgresql+psycopg://"
    f"{settings.db_user}:"
    f"{settings.db_password}@"
    f"{settings.db_host}:"
    f"{settings.db_port}/"
    f"{settings.db_name}"
)

# SQLAlchemy가 PostgreSQL과 통신할 때 사용할 Engine 생성
#
# Engine = Python 애플리케이션과 DB 사이의
# 연결을 관리하는 핵심 객체
engine = create_engine(DATABASE_URL)
