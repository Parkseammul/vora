# 2026-09-08 DB 모델링 작업 명세서

## 작업 목표

VORA MVP에서 확정한 데이터베이스 설계를 기준으로
SQLAlchemy 모델과 Alembic 마이그레이션을 구현한다.

이번 작업 범위는 다음과 같다.

- SQLAlchemy 모델 구현
- Enum 구현
- PK / FK / UNIQUE / JSONB / 기본값 구현
- Alembic 마이그레이션 생성
- PostgreSQL 실제 스키마 반영
- 핵심 데이터베이스 제약조건 테스트

작업 시작 전에 저장소 루트의 `AGENTS.md`를 읽고 규칙을 따른다.

또한 기존 `backend/app` 구조와 데이터베이스 설정을 먼저 확인하고,
현재 프로젝트 구조를 최대한 유지해서 구현한다.

아키텍처와 도메인 규칙을 임의로 변경하지 않는다.

---

# 구현 대상

총 9개의 데이터베이스 모델을 구현한다.

1. User
2. WorkflowExecution
3. ExecutionInputSnapshot
4. FileAsset
5. NodeExecution
6. NodeExecutionAttempt
7. UserApproval
8. SocialAccountConnection
9. SocialPublication

중요:

`Workflow`, `Node`, `Transition`은 Python 코드로 정의하는 개념이다.
따라서 별도의 데이터베이스 테이블을 만들지 않는다.

- 근거: VORA의 Workflow는 사용자가 직접 조립하는 방식이 아니라 애플리케이션 코드에서 정의하는 고정 실행 흐름이기 때문이다.

---

# 1. User

사용자 계정을 저장한다.

필드:

- `id`: BIGINT PK
- `email`: VARCHAR(255), NOT NULL, UNIQUE
- `name`: VARCHAR(100), NOT NULL
- `created_at`: 생성 시각
- `updated_at`: 수정 시각

규칙:

- 동일한 이메일은 중복 저장할 수 없다.
  - 근거: 하나의 이메일이 여러 사용자 계정으로 생성되는 것을 방지하기 위해서다.

- 사용자 인증 방식은 이번 DB 모델링 작업 범위에서 확정하지 않는다. 따라서 `password_hash` 등 인증 관련 컬럼은 이번 작업에서 추가하지 않는다.
  - 근거: 인증 방식이 확정되지 않은 상태에서 특정 로그인 방식에 종속되는 DB 구조를 미리 만들지 않기 위해서다.

---

# 2. WorkflowExecution

워크플로우 전체 실행 1회를 저장한다.

필드:

- `id`: BIGINT PK
- `user_id`: FK → User.id
- `workflow_key`: VARCHAR(100), NOT NULL
- `workflow_version`: INTEGER, NOT NULL
- `status`: ENUM, 기본값 PENDING
- `started_at`: nullable
- `finished_at`: nullable
- `created_at`
- `updated_at`

상태값:

- PENDING
- RUNNING
- WAITING_APPROVAL
- SUCCESS
- FAILED

---

# 3. ExecutionInputSnapshot

워크플로우 실행이 시작될 당시의 사용자 입력을 저장한다.

필드:

- `id`: BIGINT PK
- `workflow_execution_id`: FK → WorkflowExecution.id, NOT NULL, UNIQUE
- `input_type`: ENUM
- `request_text`: TEXT, nullable
- `input_data`: JSONB, NOT NULL, 기본값 빈 객체
- `created_at`

입력 유형:

- TEXT
- IMAGE
- TEXT_IMAGE

규칙:

- `workflow_execution_id`에 UNIQUE를 적용하여 WorkflowExecution 1개당 ExecutionInputSnapshot 1개만 존재하도록 한다.
  - 근거: 하나의 Workflow 실행이 시작될 당시의 입력 상태를 하나의 기준값으로 보존하기 위해서다.

- 이미지만 입력하는 경우가 있으므로 `request_text`는 NULL을 허용한다.
  - 근거: VORA는 Text뿐 아니라 Image 단독 입력도 지원하기 때문이다.

---

# 4. FileAsset

이미지, 음성, 영상, 썸네일 등의 파일 정보를 저장한다.

필드:

- `id`: BIGINT PK
- `workflow_execution_id`: FK → WorkflowExecution.id
- `execution_input_snapshot_id`: FK → ExecutionInputSnapshot.id, nullable
- `node_execution_id`: FK → NodeExecution.id, nullable
- `asset_type`: ENUM
- `storage_key`: VARCHAR(500), NOT NULL, UNIQUE
- `file_name`: VARCHAR(255), NOT NULL
- `mime_type`: VARCHAR(100), NOT NULL
- `file_size`: BIGINT, NOT NULL
- `created_at`
- `updated_at`

파일 유형:

- IMAGE
- AUDIO
- VIDEO
- THUMBNAIL

규칙:

사용자가 입력한 파일은:

- `execution_input_snapshot_id` 값 존재
- `node_execution_id` NULL

노드 실행 결과로 생성된 파일은:

- `node_execution_id` 값 존재
- `execution_input_snapshot_id` NULL

- 근거: 파일이 사용자 입력에서 온 것인지 Node 실행 결과로 생성된 것인지 출처를 구분하기 위해서다.

실제 파일 자체는 데이터베이스에 저장하지 않는다.

추후 S3에 파일을 저장하고,
데이터베이스에는 파일 메타데이터와 `storage_key`를 저장한다.

- 근거: 이미지·영상 같은 대용량 파일을 DB에 직접 저장하지 않고 객체 스토리지와 메타데이터 저장소의 역할을 분리하기 위해서다.

`execution_input_snapshot_id`와 `node_execution_id` 중
정확히 하나만 값이 존재하도록 XOR CHECK Constraint를 적용한다.

따라서 다음 규칙을 만족해야 한다.

- 사용자 입력 파일 → `execution_input_snapshot_id`만 존재 ✅
- 노드 생성 파일 → `node_execution_id`만 존재 ✅
- 두 값이 모두 NULL → 허용하지 않음 ❌
- 두 값이 모두 존재 → 허용하지 않음 ❌

- 근거: 모든 FileAsset의 생성 출처가 반드시 하나로 명확하게 결정되어야 하기 때문이다.

---

# 5. NodeExecution

특정 Node의 논리적인 실행 결과 1개를 저장한다.

필드:

- `id`: BIGINT PK
- `workflow_execution_id`: FK → WorkflowExecution.id
- `node_key`: VARCHAR(100), NOT NULL
- `user_requested_version`: INTEGER, NOT NULL, 기본값 1
- `status`: ENUM, 기본값 PENDING
- `input_data`: JSONB, NOT NULL, 기본값 빈 객체
- `output_data`: JSONB, nullable
- `started_at`: nullable
- `finished_at`: nullable
- `created_at`
- `updated_at`

상태값:

- PENDING
- RUNNING
- WAITING_APPROVAL
- SUCCESS
- FAILED
- RETRYING

필수 UNIQUE:

`(workflow_execution_id, node_key, user_requested_version)`

규칙:

- `(workflow_execution_id, node_key, user_requested_version)`에 UNIQUE를 적용한다.
  - 근거: 동일한 Workflow에서 같은 Node의 같은 사용자 요청 버전이 중복 생성되는 것을 방지하기 위해서다.

- 사용자가 결과 수정을 요청하면 새로운 NodeExecution을 만들고 `user_requested_version`을 증가시킨다.
  - 근거: 이전 결과를 덮어쓰지 않고 사용자 수정 이력을 버전별로 보존하기 위해서다.

- 기술적인 재시도에서는 `user_requested_version`을 증가시키지 않는다.
  - 근거: 사용자 수정과 시스템 오류로 인한 재시도는 서로 다른 개념이기 때문이다.

예:

`user_requested_version = 1`

사용자 수정 요청

`user_requested_version = 2`

---

# 6. NodeExecutionAttempt

같은 NodeExecution에서 발생한 기술적인 실행 시도를 저장한다.

필드:

- `id`: BIGINT PK
- `node_execution_id`: FK → NodeExecution.id
- `attempt_no`: INTEGER, NOT NULL
- `status`: ENUM
- `llm_provider`: VARCHAR(50), nullable
- `llm_model`: VARCHAR(100), nullable
- `latency_ms`: INTEGER, nullable
- `input_tokens`: INTEGER, nullable
- `output_tokens`: INTEGER, nullable
- `cost`: NUMERIC(12,6), nullable
- `error_code`: VARCHAR(100), nullable
- `error_message`: TEXT, nullable
- `metadata`: JSONB, NOT NULL, 기본값 빈 객체
- `started_at`: nullable
- `finished_at`: nullable
- `created_at`

상태값:

- RUNNING
- SUCCESS
- FAILED

필수 UNIQUE:

`(node_execution_id, attempt_no)`

규칙:

- `(node_execution_id, attempt_no)`에 UNIQUE를 적용한다.
  - 근거: 같은 NodeExecution에서 동일한 재시도 번호가 중복 생성되는 것을 방지하기 위해서다.

- Timeout, Network 오류, 5xx 등의 기술적인 문제는 새로운 NodeExecution을 만들지 않고 같은 NodeExecution 안에서 Attempt만 증가시킨다.
  - 근거: 작업 내용이 바뀐 것이 아니라 동일한 작업을 시스템이 다시 시도하는 것이기 때문이다.

예:

NodeExecution version = 1

- Attempt 1 → FAILED
- Attempt 2 → SUCCESS

이 경우 `user_requested_version`은 계속 1이다.

---

# 7. UserApproval

사용자의 승인 또는 수정 요청을 저장한다.

필드:

- `id`: BIGINT PK
- `node_execution_id`: FK → NodeExecution.id, UNIQUE
- `user_id`: FK → User.id
- `decision`: ENUM
- `revision_request`: TEXT, nullable
- `created_at`

결정값:

- APPROVED
- REVISION_REQUESTED

규칙:

- NodeExecution 하나당 UserApproval은 최대 1개다.
  - 근거: 특정 버전의 Node 결과에 대한 사용자의 최종 판단을 하나로 유지하기 위해서다.

- `decision = APPROVED`인 경우 `revision_request`는 NULL이어도 된다.
  - 근거: 승인한 경우에는 별도의 수정 요청 내용이 필요하지 않기 때문이다.

- `decision = REVISION_REQUESTED`인 경우 `revision_request`는 반드시 존재해야 한다.
  - 근거: 다음 NodeExecution Version을 생성할 때 무엇을 수정해야 하는지 알아야 하기 때문이다.

- 위 규칙은 DB CHECK Constraint로도 검증한다.
  - 근거: 잘못된 수정 요청 데이터가 DB에 직접 저장되는 것도 방지하기 위해서다.

- 수정 요청 내용은 이후 새로운 NodeExecution Version을 생성할 때 사용한다.

---

# 8. SocialAccountConnection

사용자가 연결한 YouTube / Instagram 계정의 OAuth 연결 정보를 저장한다.

필드:

- `id`: BIGINT PK
- `user_id`: FK → User.id
- `platform`: ENUM
- `external_account_id`: VARCHAR(255), NOT NULL
- `account_name`: VARCHAR(255), nullable
- `status`: ENUM, 기본값 CONNECTED
- `access_token`: TEXT, NOT NULL
- `refresh_token`: TEXT, nullable
- `token_expires_at`: nullable
- `created_at`
- `updated_at`

플랫폼:

- YOUTUBE
- INSTAGRAM

상태값:

- CONNECTED
- EXPIRED
- REVOKED

필수 UNIQUE:

`(user_id, platform, external_account_id)`

규칙:

- `(user_id, platform, external_account_id)`에 UNIQUE를 적용한다.
  - 근거: 동일 사용자가 같은 SNS 계정을 중복 연결하는 것을 방지하기 위해서다.

- YouTube / Instagram OAuth2 실제 연동은 이후 SNS 연동 단계에서 구현한다. 이번 DB 모델링 작업에서는 OAuth 연결 정보를 저장할 데이터 구조만 구현한다.
  - 근거: 이번 Task의 범위는 DB 모델링이며 실제 외부 API 연동은 별도의 기능 구현 단계이기 때문이다.

- Access Token과 Refresh Token은 로그에 출력하지 않는다.
  - 근거: 외부 SNS 계정 접근 권한을 가진 민감한 인증 정보이기 때문이다.

---

# 9. SocialPublication

SNS 게시 실행 정보를 저장한다.

필드:

- `id`: BIGINT PK
- `social_account_connection_id`: FK → SocialAccountConnection.id
- `file_asset_id`: FK → FileAsset.id
- `status`: ENUM, 기본값 PENDING
- `idempotency_key`: VARCHAR(255), NOT NULL, UNIQUE
- `external_post_id`: VARCHAR(255), nullable
- `external_post_url`: TEXT, nullable
- `error_message`: TEXT, nullable
- `published_at`: nullable
- `created_at`
- `updated_at`

상태값:

- PENDING
- SUCCESS
- FAILED

규칙:

- Workflow 성공 여부와 SNS 게시 성공 여부는 독립적으로 관리한다.
  - 근거: 영상 생성이 성공해도 SNS API 오류 등으로 게시만 실패할 수 있기 때문이다.

- `idempotency_key`에 UNIQUE를 적용한다.
  - 근거: 동일한 게시 요청이 재전송되더라도 같은 콘텐츠가 의도치 않게 여러 번 게시되는 것을 방지하기 위해서다.

- `(social_account_connection_id, file_asset_id)`에는 전역 UNIQUE를 추가하지 않는다.
  - 근거: 사용자가 같은 영상을 의도적으로 다시 게시할 수도 있기 때문이다.

---

# 반드시 구분해야 하는 핵심 개념

## 사용자 수정

사용자가 콘텐츠 결과 수정을 요청하는 경우:

새로운 NodeExecution을 만든다.

예:

`SCRIPT_GENERATION version 1`

↓ 사용자 수정 요청

`SCRIPT_GENERATION version 2`

즉:

`user_requested_version + 1`

---

## 시스템 재시도

Timeout, Network 오류, 5xx와 같은 기술적인 실패의 경우:

같은 NodeExecution을 유지한다.

대신 새로운 NodeExecutionAttempt를 만든다.

예:

`NodeExecution version 1`

- Attempt 1 → FAILED
- Attempt 2 → SUCCESS

즉:

`attempt_no + 1`

이때 `user_requested_version`은 증가하지 않는다.

사용자 수정 Version과 시스템 Retry Attempt를 절대 같은 개념으로 구현하지 않는다.

- 근거: 사용자 요구사항 변경 이력과 시스템 장애 재시도 이력을 독립적으로 추적해야 하기 때문이다.

---

# Alembic 마이그레이션

SQLAlchemy 모델 구현 후 Alembic Migration을 생성한다.

Migration에는 다음 내용이 정확하게 반영되어야 한다.

- 9개 Table
- Primary Key
- Foreign Key
- UNIQUE Constraint
- CHECK Constraint
- ENUM
- JSONB
- nullable
- 기본값

Migration 생성 후 실제 PostgreSQL에 적용한다.

```bash
alembic upgrade head
```

파괴적인 DB 초기화 작업은 하지 않는다.

다음과 같은 작업을 임의로 수행하지 않는다.

- 데이터베이스 삭제
- 테이블 전체 삭제
- 데이터 전체 삭제
- TRUNCATE
- 기존 Migration History 초기화
- 기존 Migration 파일 삭제
- 기존 데이터를 손상시킬 수 있는 명령

필요한 경우 먼저 Architect에게 보고한다.

- 근거: Agent가 구현 과정에서 기존 데이터나 Migration 이력을 임의로 손상시키는 것을 방지하기 위해서다.

---

# 자동 테스트

최소한 다음 내용을 자동 테스트한다.

1. 동일한 User email 중복 저장 → 실패
2. 동일한 WorkflowExecution에 Snapshot 2개 생성 → 실패
3. 동일한 `(workflow_execution_id, node_key, user_requested_version)` → 실패
4. 동일한 `(node_execution_id, attempt_no)` → 실패
5. 동일 NodeExecution에 UserApproval 2개 생성 → 실패
6. 동일한 `(user_id, platform, external_account_id)` → 실패
7. 동일한 `idempotency_key` → 실패
8. 서로 다른 `user_requested_version` → 정상 저장
9. 같은 NodeExecution에서 서로 다른 `attempt_no` → 정상 저장
10. JSONB 저장 및 조회 → 정상 동작
11. FileAsset에서 `execution_input_snapshot_id`와 `node_execution_id`가 모두 NULL → 실패
12. FileAsset에서 `execution_input_snapshot_id`와 `node_execution_id`가 모두 존재 → 실패
13. `decision = REVISION_REQUESTED`인데 `revision_request`가 NULL → 실패

---

# 검증

구현 완료 후 가능한 범위에서 다음 검증을 실행한다.

```bash
python -m pytest
python -m ruff check .
python -m mypy app
alembic upgrade head
```

현재 작업으로 인해 발생한 오류는 수정한 뒤 다시 검증한다.

검증 실패를 숨기지 않는다.

도구가 설치되어 있지 않거나 기존 프로젝트 문제로 실행할 수 없는 경우에는
임의로 우회하지 말고 원인을 완료 보고에 남긴다.

---

# 이번 작업에서 하지 않을 것

이번 작업에서는 다음 기능을 구현하지 않는다.

- Workflow Engine
- Node Executor
- AI API 호출
- Redis
- Celery
- SSE
- S3 실제 업로드
- OAuth 실제 연동
- YouTube 실제 게시
- Instagram 실제 게시
- Frontend
- MCP Server
- Agent Skill
- 새로운 Domain Table
- 불필요한 리팩터링
- 미래 기능을 위한 과도한 추상화

---

# 완료 조건

다음 조건을 만족해야 이번 작업을 완료한 것으로 본다.

1. 9개의 SQLAlchemy Model 구현
2. 필요한 Enum 구현
3. PK / FK / UNIQUE / CHECK / JSONB / 기본값 반영
4. 사용자 수정 Version과 시스템 Attempt 분리
5. Alembic Migration 생성
6. `alembic upgrade head` 성공
7. PostgreSQL 실제 스키마 확인
8. 핵심 데이터베이스 제약 테스트 통과
9. pytest 검증 결과 확인
10. Ruff 검증 결과 확인
11. mypy 검증 결과 확인
12. Task 범위 밖의 아키텍처 변경 없음
13. 불필요한 파일 수정 없음

---

# 작업 완료 보고

작업이 끝나면 다음 내용을 보고한다.

1. 변경한 파일
2. 구현한 모델
3. 생성한 Alembic Migration
4. 적용한 핵심 데이터베이스 제약조건
5. 실행한 검증 명령
6. PASS / FAIL 결과
7. 남아 있는 문제
8. Architect의 판단이 필요한 사항
9. 핵심 Diff 요약
