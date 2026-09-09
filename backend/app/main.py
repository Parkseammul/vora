# FastAPI 애플리케이션을 만들기 위해 FastAPI 클래스를 가져옴
from fastapi import FastAPI

# SQL 문자열을 안전하게 실행하기 위한 SQLAlchemy 함수
from sqlalchemy import text

# database.py에서 만든 PostgreSQL 연결 Engine 가져오기
from app.database import engine


# VORA Backend의 FastAPI 애플리케이션 객체 생성
app = FastAPI()


# 서버 자체가 정상 실행 중인지 확인하는 API
@app.get("/health")
def health_check():
    return {"status": "ok"}


# FastAPI가 PostgreSQL에 실제로 연결되는지 확인하는 API
@app.get("/health/db")
def database_health_check():
    # PostgreSQL 연결을 하나 가져옴
    with engine.connect() as connection:
        # DB에 아주 간단한 쿼리를 보내 실제 통신 여부 확인
        connection.execute(text("SELECT 1"))

    # 여기까지 오류 없이 실행되면 DB 연결 성공
    return {
        "status": "ok",
        "database": "connected",
    }