# 2026-09-09 Workflow Engine

## 목표

VORA의 코드 기반 Workflow 정의와 최소 Workflow Engine을 구현한다.

Workflow가 Node를 순서대로 실행하고,
승인이 필요한 Node에서는 `WAITING_APPROVAL` 상태로 멈추며,
사용자 승인 후 다음 Node부터 다시 실행할 수 있어야 한다.

실제 LLM / 영상 생성 기능은 이번 Task 범위가 아니다.

### 근거

9/8에 Workflow 실행 상태와 결과를 저장할 DB 구조를 만들었으므로,
9/9에는 그 DB를 기반으로 실제 실행 순서와 상태를 관리하는 Engine의 뼈대를 구현한다.


---

## 1. Core Workflow Node

VORA MVP의 실제 작업 Node는 4개다.

- 입력 분석 → `input_analysis`
- 콘텐츠 기획 → `content_planning`
- 대본 생성 → `script_generation`
- 영상 생성 → `video_generation`

Workflow 순서:

`input_analysis`
→ `content_planning`
→ `script_generation`
→ `video_generation`

승인 필요 여부:

- `input_analysis` → 승인 없음
- `content_planning` → 승인 필요
- `script_generation` → 승인 필요
- `video_generation` → 승인 필요

### 근거

승인은 별도의 작업을 수행하는 기능이 아니라
생성된 결과에 대한 사용자 판단이므로 별도 Node로 만들지 않는다.


---

## 2. 승인 처리

승인이 필요한 Node가 결과 생성을 완료하면:

`NodeExecution.status = WAITING_APPROVAL`

로 저장하고 Workflow 실행을 중단한다.

사용자가 승인하면:

`WAITING_APPROVAL → SUCCESS`

로 변경하고 다음 Node부터 실행을 재개한다.

Workflow 전체도 승인 대기 중에는:

`WorkflowExecution.status = WAITING_APPROVAL`

로 저장한다.

승인 후:

`WorkflowExecution.status = RUNNING`

으로 변경한다.

### 근거

현재 DB가 `NodeExecution → UserApproval` 구조이므로,
승인 정보를 해당 결과를 생성한 NodeExecution에 연결하는 것이 자연스럽다.


---

## 3. Workflow 코드 정의

Workflow 구조 자체는 DB Table로 만들지 않고 Python 코드로 정의한다.

구성:

### `WorkflowDefinition`

전체 Workflow 설계도.

포함 정보:

- Workflow Key
- Workflow Version
- 시작 Node
- Node 목록
- Transition 목록

### `NodeDefinition`

각 작업 단계의 정의.

최소 정보:

- Node Key
- 승인 필요 여부

### `Transition`

현재 Node 다음에 어떤 Node를 실행할지 정의한다.

예:

`content_planning → script_generation`

### `WorkflowRegistry`

`workflow_key + workflow_version`으로
사용할 `WorkflowDefinition`을 찾아준다.

현재는 하나만 등록한다.

`vora_content_creation / version 1`

### 근거

VORA는 사용자가 Workflow를 자유롭게 조립하는 n8n 형태가 아니라
서비스에서 정한 고정 Workflow다.

따라서 Workflow 구조는 코드로 관리하고,
DB에는 실제 실행 상태와 결과만 저장한다.

Registry는 승인 후 재개하거나 서버가 재시작되어도
DB의 `workflow_key + workflow_version`으로 동일한 설계도를 다시 찾기 위해 사용한다.


---

## 4. 상태 흐름

### 승인 없는 Node

`PENDING`
→ `RUNNING`
→ `SUCCESS`

### 승인 필요한 Node

`PENDING`
→ `RUNNING`
→ `WAITING_APPROVAL`
→ `SUCCESS`

### 실패

Node 실행 중 오류 발생:

`NodeExecution = FAILED`

`WorkflowExecution = FAILED`

### Workflow 완료

마지막 `video_generation` 승인 완료:

`WorkflowExecution = SUCCESS`

### 근거

현재 MVP에서는 실행 중 / 승인 대기 / 성공 / 실패만으로
필요한 실행 상태를 충분히 표현할 수 있다.

사용자 수정 요청과 부분 재실행은 후속 Task에서 처리한다.


---

## 5. Workflow Engine 역할

Workflow Engine은 Workflow 실행의 진행 관리자다.

책임:

- WorkflowDefinition 조회
- 시작 Node 결정
- 다음 Node 결정
- NodeExecution 생성
- NodeExecution 상태 변경
- WorkflowExecution 상태 변경
- Node 실행 결과 DB 저장
- 승인 필요 여부 판단
- 승인 대기에서 실행 중단
- 승인 후 실행 재개
- 실패 시 Workflow FAILED 처리

Workflow 상태는 Workflow Engine만 변경한다.

### 근거

여러 Service가 상태를 직접 변경하면 실행 규칙이 분산되고
Workflow 이력이 꼬일 수 있으므로 상태 관리 책임을 Engine 한 곳에 둔다.


---

## 6. Engine 실행 방식

Workflow Engine의 주요 실행 진입점은 두 가지로 둔다.

### Workflow 시작

`start_execution()`

새 Workflow를 시작하고,
다음 중 하나가 발생할 때까지 계속 Node를 실행한다.

- `WAITING_APPROVAL`
- `SUCCESS`
- `FAILED`

예:

Workflow 시작
→ 입력 분석
→ 콘텐츠 기획
→ `WAITING_APPROVAL`
→ 실행 종료


### 승인 후 재개

`resume_after_approval()`

사용자 승인 후 DB 상태와 WorkflowDefinition을 다시 확인하고
다음 Node부터 실행을 재개한다.

예:

콘텐츠 기획 승인
→ 콘텐츠 기획 `SUCCESS`
→ 대본 생성
→ `WAITING_APPROVAL`

### 근거

승인은 몇 분 또는 몇 시간 뒤에 발생할 수 있고 서버가 재시작될 수도 있으므로,
Engine이 계속 대기하는 방식보다 DB에 상태를 저장하고
요청이 들어올 때 다시 실행하는 방식이 안전하다.


---

## 7. NodeExecutor 역할

NodeExecutor는 해당 Node의 실제 작업만 수행한다.

흐름:

Workflow Engine
→ Node 실행에 필요한 입력 전달

NodeExecutor
→ 실제 작업 수행
→ 결과 또는 오류 반환

Workflow Engine
→ 결과 저장 및 상태 변경

NodeExecutor가 직접 해서는 안 되는 것:

- WorkflowExecution 상태 변경
- NodeExecution 상태 변경
- 다음 Node 결정

### 근거

향후 OpenAI, Gemini, Video Service, MCP 등
실제 실행 기술이 바뀌더라도 Workflow 상태 관리 로직은 Engine에 유지하기 위함이다.

즉:

- Workflow Engine = 진행 관리자
- NodeExecutor = 실제 작업 수행자


---

## 8. NodeExecutionAttempt

Node가 실제로 실행될 때
`NodeExecutionAttempt`를 기록한다.

첫 실행:

NodeExecution
└─ Attempt 1

이번 Task에서는 실제 Retry 로직을 구현하지 않는다.

향후 Retry가 추가되면:

NodeExecution
├─ Attempt 1 → FAILED
└─ Attempt 2 → SUCCESS

형태로 같은 NodeExecution 아래 Attempt가 증가한다.

### 근거

사용자 수정에 따른 새로운 버전과
시스템 오류에 따른 기술적 재시도를 분리해서 기록하기 위함이다.

- `user_requested_version` = 사용자 수정 결과 버전
- `attempt_no` = 같은 버전의 시스템 실행 시도 횟수


---

## 9. 구현 방식

이번 Task에서는 실제 AI Provider를 연결하지 않는다.

Fake / Stub NodeExecutor를 사용해 Workflow Engine 자체를 검증한다.

예:

`input_analysis`
→ fake input analysis result

`content_planning`
→ fake content planning result

### 근거

이번 Task의 목적은 AI 결과 품질이 아니라 다음을 검증하는 것이다.

- Node 실행 순서
- 상태 변경
- DB 실행 이력
- 승인 대기
- 승인 후 재개
- 실패 처리


---

## 10. 반드시 검증할 시나리오

### Workflow 시작

Workflow 시작

→ 입력 분석 `SUCCESS`

→ 콘텐츠 기획 `WAITING_APPROVAL`

→ Workflow `WAITING_APPROVAL`

→ 여기서 실행 중단


### 승인 후 재개

콘텐츠 기획 승인

→ 콘텐츠 기획 `SUCCESS`

→ Workflow `RUNNING`

→ 대본 생성

→ 대본 생성 `WAITING_APPROVAL`


### 마지막 승인

영상 생성 승인

→ 영상 생성 `SUCCESS`

→ Workflow `SUCCESS`


### 실패

NodeExecutor 실행 오류

→ 해당 NodeExecution `FAILED`

→ WorkflowExecution `FAILED`


### Attempt

Node 실행 시:

→ `NodeExecutionAttempt attempt_no = 1` 생성


### Registry

`workflow_key + workflow_version`으로
올바른 WorkflowDefinition 조회


---

## 11. 이번 Task에서 하지 않는 것

- 실제 OpenAI / Gemini / Anthropic 연결
- 실제 영상 생성
- Redis / Celery
- SSE
- SNS 게시
- OAuth2
- 사용자 수정 요청 처리
- `user_requested_version` 증가
- 부분 재실행
- 실제 Retry / Backoff
- 범용 DAG Engine
- Workflow Builder
- Workflow / Node / Transition DB Table 생성


---

## 12. DB 변경 원칙

기존 9/8 DB Schema를 사용한다.

새 Table 또는 Column이 필요하다고 판단되면
Codex가 임의로 Migration을 생성하지 않고 Architect에게 먼저 보고한다.

### 근거

9/8에 핵심 실행 이력 Schema를 이미 확정했으므로,
Workflow Engine 구현 편의를 위해 Domain 구조를 임의 변경하지 않는다.


---

## 13. Harness

구현 후 다음 방식으로 검증한다.

- Alembic / PostgreSQL
  - 기존 Migration과 실제 DB 상태가 정상인지 확인
- pytest
  - Workflow 실행 / 승인 대기 / 재개 / 실패 / Attempt 기록 검증
- Ruff
  - 코드 오류 가능성과 품질 문제 검사
- mypy
  - 타입 오류 검사

DB 연결 실패를 테스트 `skip`으로 숨기지 않는다.

### 검증 흐름

Codex 구현
→ DB 상태 확인
→ pytest
→ Ruff
→ mypy
→ Diff Review

### 근거

AI Agent가 코드를 작성하더라도
자동 검증을 통과해야 작업 완료로 판단하기 위함이다.


---

## Definition of Done

- `WorkflowDefinition` 구현
- `NodeDefinition` 구현
- `Transition` 구현
- `WorkflowRegistry` 구현
- VORA Workflow v1 등록
- `start_execution()` 구현
- `resume_after_approval()` 구현
- Workflow Engine 최소 실행 구현
- Fake / Stub NodeExecutor 구현
- Node 순차 실행 확인
- 승인 대기 시 실행 중단 확인
- 승인 후 실행 재개 확인
- 마지막 승인 후 Workflow `SUCCESS` 확인
- Executor 실패 시 Workflow `FAILED` 확인
- `NodeExecutionAttempt` 첫 실행 기록 확인
- DB에 NodeExecution 결과 및 상태 저장 확인
- pytest PASS
- 관련 Ruff PASS
- 관련 mypy PASS
- Diff Review 가능 상태