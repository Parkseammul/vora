# VORA Backend가 사용할 Python 3.12 기반 이미지
# slim = 불필요한 패키지를 줄인 가벼운 이미지
FROM python:3.12-slim


# Container 내부에서 Python이 .pyc / __pycache__ 파일을 만들지 않도록 설정
ENV PYTHONDONTWRITEBYTECODE=1

# Python 로그를 버퍼에 쌓지 않고 바로 출력
# Docker 로그를 실시간으로 확인하기 편하게 함
ENV PYTHONUNBUFFERED=1


# Container 내부의 작업 폴더를 /app으로 지정
WORKDIR /app

# 라이브러리 목록을 먼저 Container 안으로 복사
COPY requirements.txt .


# requirements.txt에 정의된 Python 라이브러리 설치
RUN pip install --no-cache-dir -r requirements.txt



# backend의 전체 소스코드를 Container의 /app으로 복사
COPY . .

# Container가 시작될 때 FastAPI 실행
#
# app.main:app
# = app/main.py 안의 app = FastAPI() 객체
#
# --host 0.0.0.0
# = Container 외부에서도 FastAPI에 접근할 수 있게 함
#
# --port 8000
# = FastAPI 실행 포트
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]