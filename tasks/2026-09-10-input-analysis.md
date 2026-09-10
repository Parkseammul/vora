# VORA 9/10 Task Spec
## Text/Image 입력 → 구조화 Input Analysis → Workflow 연결

### Goal

사용자가 `request_text`와 선택적인 이미지 0~6장을 한 번에 제출하면,

`입력 검증 → 원본 저장 → PostgreSQL 저장 → DB 기준 입력 재조립 → WorkflowEngine 실행 → input_analysis 구조화 결과 → content_planning 전달 → WAITING_APPROVAL 응답`

까지 연결한다.

**근거:** 오늘은 실제 AI 품질보다, 이후 AI/영상 기능이 붙어도 흔들리지 않는 입력·저장·Workflow 계약을 먼저 완성하는 것이 중요하다.

### API

```text
POST /workflow-executions
Content-Type: multipart/form-data

request_text: 필수
images: 선택, 0~6장
```

규칙:
- `request_text`: NULL/빈 문자열/공백만 있는 값 불가
- 이미지: `image/jpeg`, `image/png`만 허용
- 이미지 1장 최대 10MB
- 이미지 없음 → `InputType.TEXT`
- 이미지 있음 → `InputType.TEXT_IMAGE`

**근거:** Text/Image를 별도 API로 나누지 않아 사용자 요청 흐름을 단순하게 유지한다.

### DB / 원본 저장

#### ExecutionInputSnapshot
- `request_text`를 `NOT NULL`로 변경
- whitespace-only를 막는 DB CHECK 추가
- `input_type` 저장

#### FileAsset
`sort_order: int | None` 추가

입력 이미지에는 업로드 순서대로 `1, 2, 3 ...` 저장.

규칙:
- `sort_order >= 1`
- 같은 `ExecutionInputSnapshot`에서 동일 `sort_order` 중복 불가
- 생성된 AUDIO/VIDEO/THUMBNAIL 등은 `sort_order = NULL` 가능

**근거:** 이미지 순서가 이후 영상 장면의 기본 순서이므로 DB에서 보존해야 한다.

> 기존 migration은 수정하지 말고 새 Alembic migration을 만든다.

### 파일 저장

AWS S3는 아직 사용하지 않는다.

```text
로컬 파일
backend/uploads/inputs/{workflow_execution_id}/{uuid}.{ext}

DB storage_key
inputs/{workflow_execution_id}/{uuid}.{ext}
```

- 저장명은 UUID 사용
- `FileAsset.file_name`에는 원본 파일명 보존
- 절대 Windows 경로는 DB에 저장하지 않음
- `backend/uploads/`는 Git에서 제외

**근거:** 지금은 로컬로 개발하되 나중에 S3로 옮겨도 `storage_key` 계약을 재사용하기 위함이다.

### 개발용 User

로그인/JWT는 이번 Task에서 구현하지 않는다.

`backend/scripts/seed_dev_user.py`를 추가하고 다음 개발용 User를 생성한다.

```text
email = dev@vora.local
name = VORA Dev
```

- 여러 번 실행해도 중복 생성되지 않게 작성
- `user_id = 1` 하드코딩 대신 email로 조회 후 실제 DB id 사용

**근거:** `WorkflowExecution.user_id`는 필수지만 인증까지 이번 범위에 넣으면 Task가 커진다.

### PostgreSQL = Source of Truth

API 값을 바로 Engine으로 넘기지 않는다.

```text
Request
→ 파일 저장
→ WorkflowExecution / ExecutionInputSnapshot / FileAsset 저장
→ DB commit
→ DB에 저장된 값으로 input_data 재조립
→ WorkflowEngine.start_execution()
```

`input_analysis` 최초 입력 예:

```json
{
  "request_text": "이 화장품 사진으로 30초 광고 만들어줘",
  "input_type": "TEXT_IMAGE",
  "source_asset_ids": [11, 12, 13]
}
```

`source_asset_ids`는 클라이언트에서 받지 않고, DB에 저장된 입력 IMAGE FileAsset를 `sort_order ASC`로 조회해 백엔드가 만든다.

**근거:** Workflow가 실제 영구 저장된 데이터와 동일한 값을 사용하게 해 재실행·추적 시 불일치를 막는다.

### InputAnalysisResult

실제 LLM은 아직 붙이지 않고 Rule/Fake Analyzer로 계약만 완성한다.

```python
ContentGoal:
- GENERAL_SHORTFORM
- PRODUCT_AD
- INFORMATIONAL

AgeGroup:
- ALL
- TEENS
- TWENTIES
- THIRTIES
- FORTIES
- FIFTIES_PLUS

Gender:
- ALL
- FEMALE
- MALE
```

구조:

```json
{
  "content_goal": "PRODUCT_AD",
  "target_audience": {
    "age_group": "TWENTIES",
    "gender": "FEMALE",
    "audience_group": "일반인"
  },
  "duration_seconds": 30,
  "tone": "밝고 자연스럽게",
  "source_asset_ids": [11, 12, 13]
}
```

기본값/검증:
- `content_goal`: `GENERAL_SHORTFORM`
- `age_group`: `ALL`
- `gender`: `ALL`
- `audience_group`: `"일반인"`, 공백 불가
- `duration_seconds`: 기본 30, 5~60
- `tone`: `"밝고 자연스럽게"`, 공백 불가
- `source_asset_ids`: 최대 6개, 중복 불가, 순서 유지

처리 계약:

```text
raw input
→ Pydantic InputAnalysisResult 검증
→ model_dump()
→ input_analysis NodeExecution.output_data
→ content_planning NodeExecution.input_data
```

**근거:** 실제 LLM을 나중에 붙여도 Node 간 데이터 계약은 그대로 유지할 수 있다.

### Workflow 실행

`POST /workflow-executions` 요청 한 번으로 생성과 실행을 같이 처리한다.

```text
WorkflowExecution 생성
→ input_analysis
→ content_planning
→ WAITING_APPROVAL
```

별도 `/start` API는 만들지 않는다.

기존 흐름은 유지한다.

```text
input_analysis
→ content_planning
→ script_generation
→ video_generation
```

**근거:** 사용자 입장에서는 요청 한 번이면 되고, 실행 상태 제어는 WorkflowEngine이 담당하는 것이 자연스럽다.

### API Response

`content_planning`이 승인 대기 상태가 되면:

```json
{
  "workflow_execution_id": 101,
  "status": "WAITING_APPROVAL",
  "current_node": "content_planning",
  "current_node_execution_id": 205,
  "result": {
    "...": "content planning result"
  }
}
```

**근거:** 프론트가 별도 조회 없이 바로 현재 승인 대상과 결과를 표시할 수 있다.

### Failure Handling

DB 저장 실패 시:

```text
transaction rollback
→ 이번 요청에서 생성한 로컬 파일 삭제
→ WorkflowEngine 실행 금지
```

cleanup 실패는 원래 DB 오류를 덮지 말고 로그만 남긴다.

**근거:** DB에 연결되지 않은 orphan 파일이 쌓이는 것을 막기 위함이다.

### Harness

성공 케이스:
- Text-only → `TEXT`, FileAsset 없음, `source_asset_ids=[]`
- Text + 이미지 3장 → `TEXT_IMAGE`, `sort_order=1,2,3`
- 실제 DB FileAsset ID가 `source_asset_ids`에 순서대로 들어감
- `InputAnalysisResult` 검증 성공
- `input_analysis.output_data == content_planning.input_data`
- Workflow / content_planning 상태가 `WAITING_APPROVAL`
- API 응답에 execution id, current node, node execution id, result 포함
- Engine 입력이 DB 저장 후 재조회한 값임을 검증

실패 케이스:
- request_text 누락 / 빈 값 / 공백만
- 이미지 7장
- PDF / WebP / GIF
- 이미지 10MB 초과
- DB request_text NULL / blank
- sort_order 0 / 중복
- DB 저장 실패 시 로컬 파일 cleanup
- DB 저장 실패 후 Engine 미실행

DB 연결 실패 시 테스트를 skip하지 말고 실패시킨다.

**근거:** VORA 포트폴리오의 핵심은 정상 동작뿐 아니라 실패 시 상태와 데이터가 깨지지 않는 백엔드임을 보여주는 것이다.

### Non-goals

이번 Task에서 하지 않는다:
- 실제 OpenAI / Gemini / Anthropic 호출
- 실제 Content Planning AI
- Script / Video 생성
- Redis / Celery / SSE
- S3
- OAuth2 / SNS 게시
- Revision / Partial Rerun
- 범용 Workflow Context 시스템

**근거:** 오늘은 입력과 Workflow 계약을 완성하는 날이며, 범위를 넓히면 검증 포인트가 흐려진다.

### Definition of Done

```text
Text/Image 입력
→ Validation
→ Local File 저장
→ PostgreSQL 저장
→ DB 기준 input_data 재조립
→ WorkflowEngine
→ input_analysis structured output
→ content_planning 전달
→ WAITING_APPROVAL
→ API Response
```

위 흐름이 실제 PostgreSQL 기반 테스트로 증명되어야 한다.

기존 Workflow Engine 테스트는 유지한다.

특히:

```text
Executor 성공
→ NodeExecutionAttempt SUCCESS

승인 필요 Node
→ NodeExecution WAITING_APPROVAL
```

의 기존 의미가 깨지면 안 된다.

작업 완료 후 `pytest`, 관련 파일 `ruff`, 관련 파일 `mypy`, `alembic upgrade head/current/check` 결과를 보고한다.

**구현 후 바로 commit하지 말고, 변경 파일 목록 / migration 내용 / 테스트 결과를 먼저 보고한 뒤 Diff Review를 기다린다.**
