# Quality Gates

## 준비와 실행

Windows PowerShell 5.1에서 실행한다. Python 환경을 먼저 활성화하고
backend/requirements.txt의 의존성을 설치한다. Harness는 PATH의 python과 git을 사용한다.
Frontend는 Node.js와 npm.cmd, frontend/package.json의 설치된 의존성이 필요하다.
Harness는 의존성을 자동 설치하지 않는다.

backend/tests/conftest.py는 VORA_TEST_POSTGRES_URL을 필수로 요구한다.
접속 가능한 전용 PostgreSQL DB를 준비하고 환경변수로 지정한다.
DB 이름은 vora_test 또는 vora_test_로 시작하는 영문/숫자/밑줄 이름이어야 한다.
테스트가 해당 DB에 테이블을 생성하므로 개발/운영 DB를 사용하지 않는다.
인증 정보는 문서나 실행 보고서에 기록하지 않는다.

저장소 루트에서:

```powershell
powershell -NoProfile -File scripts/harness.ps1 -Scope Backend
powershell -NoProfile -File scripts/harness.ps1 -Scope Frontend
powershell -NoProfile -File scripts/harness.ps1 -Scope All
```

기본 Scope는 Backend다. 다른 디렉터리에서는 스크립트의 절대 경로로 호출한다.
스크립트 위치로 repo root를 찾으며 호출자의 현재 디렉터리에 의존하지 않는다.

| 범위 | 실행 디렉터리 | 명령 |
| --- | --- | --- |
| Backend | backend | python -m pytest |
| Backend | backend | python -m ruff check . |
| Backend | backend | python -m mypy app |
| Frontend 변경 시 | frontend | npm run lint |
| Frontend 변경 시 | frontend | npm test |
| Frontend 변경 시 | frontend | npm run build |
| 모든 범위 | repo root | git diff --check 및 git diff --cached --check |

Windows에서는 npm.ps1 실행 정책 충돌을 피하도록 npm.cmd로 호출한다.
각 단계는 실제 프로세스 종료 코드를 검사한다. 명령 누락/실행 예외도 FAIL이며,
실패 후 나머지 단계를 실행하고 요약한다. 전체 성공은 exit 0, 하나라도 실패하면 exit 1이다.
잘못된 Scope 등 매개변수 오류 역시 PowerShell이 non-zero로 종료한다.

## DB Schema 변경 시

이번 Bootstrap은 DB 변경이 없어 Alembic을 실행하지 않는다.
향후 DB 변경 Task에서는 별도의 폐기 가능한 migration 검증 DB를 대상으로,
backend 디렉터리에서 아래를 실행하고 결과를 별도 보고한다.

```powershell
python -m alembic upgrade head
python -m alembic current
```

backend/alembic.ini와 migrations/env.py는 app.database 설정을 사용한다.
pytest 전용 VORA_TEST_POSTGRES_URL은 Alembic에 자동 적용되지 않는다.
app.config의 DB_HOST / DB_PORT / DB_NAME / DB_USER / DB_PASSWORD를 검증 DB로 설정하고
대상을 확인한 뒤 실행한다. 빈 DB upgrade와 필요한 기존 revision에서의 upgrade를 확인한다.
Harness의 일반 PASS에는 이 수동 Gate가 포함되지 않는다.

## 결과와 Diff Review

Task 때문에 발생한 실패는 수정하고 재검증한다. 기존 실패와 실행 환경 문제는
별도로 보고한다. 실행하지 못한 Gate와 skip된 테스트의 이유를 남기며,
모든 Gate나 외부 연동이 검증되었다고 보고하지 않는다.
기본 pytest에서는 Celery/Redis E2E가 opt-in으로 skip될 수 있다.
이를 실행할 때는 기존 conftest.py의 VORA_RUN_CELERY_REDIS_E2E,
VORA_TEST_REDIS_URL, VORA_TEST_CELERY_QUEUE, VORA_E2E_FAKE_PROVIDERS 조건을 따른다.

git diff --check는 untracked 파일을 포함하지 않는다.
git status --short --untracked-files=all로 생성 파일도 리뷰하고, 새 파일은
git diff --no-index --check -- NUL <파일>로 별도 확인한다.
git diff --stat도 untracked 파일을 집계하지 않으므로 완료 보고에 생성 파일 목록을 함께 적는다.

Harness 자체의 회귀 검증은 아래 명령으로 실행한다.

```powershell
powershell -NoProfile -File tests/harness.tests.ps1
```

이 검증은 임시 fixture와 가짜 명령으로 범위/경로/실패 전파를 확인한다.
실제 Backend/Frontend Gate의 성공을 대신하지 않는다.
