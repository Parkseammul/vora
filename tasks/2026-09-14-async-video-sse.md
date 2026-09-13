# 2026-09-14 Redis/Celery 비동기 영상 Job + SSE

## Goal

`video_generation`만 Redis + Celery로 비동기 실행하고, 진행 상황은 SSE로 Frontend에 전달한다.

**근거:** 영상 생성은 오래 걸릴 수 있으므로 HTTP 요청과 실제 생성 작업을 분리해야 한다.

## Target Flow

```text
Script 승인
→ Workflow Engine
→ video_generation enqueue
→ Redis Queue
→ Celery Worker
→ VideoGenerationService
   ├─ Scene 영상 생성
   ├─ TTS 생성
   └─ FFmpeg 합성
→ 결과 반환
→ Workflow Engine 완료 처리
→ video_generation = WAITING_APPROVAL

진행 이벤트
Celery Worker → Redis → SSE → Frontend
```

## Scope

### 1. 비동기 대상

- `video_generation`만 Celery 비동기 처리
- 다른 Node의 공통 Async Framework는 이번 범위에서 제외

**근거:** 현재 실제 장시간 작업은 영상 생성이므로 필요한 범위만 구현한다.

### 2. Job / 실행 이력

- 별도 `AsyncJob` / `VideoGenerationJob` 테이블 생성하지 않음
- 기존 `NodeExecution` + `NodeExecutionAttempt` 사용
- Celery 추적 정보는 `NodeExecutionAttempt.metadata_`에 저장
  - `celery_task_id`
  - `queue`
  - `queue_state`
  - 필요 시 `worker`

**근거:** Celery는 새로운 비즈니스 Domain이 아니라 기술적 실행 수단이므로 기존 Attempt 구조를 재사용한다.

### 3. 상태

기존 `NodeExecutionStatus`를 유지한다.

- `PENDING`
- `RUNNING`
- `RETRYING`
- `WAITING_APPROVAL`
- `SUCCESS`
- `FAILED`

`QUEUED` 같은 Celery 내부 상태는 Node Status에 추가하지 않고 metadata로만 추적한다.

**근거:** VORA Domain 상태와 Celery 인프라 상태를 분리한다.

### 4. 책임 분리

- Celery Worker: `VideoGenerationService` 실행 + 성공/실패 결과 반환
- Workflow Engine: Attempt / NodeExecution / WorkflowExecution 상태 전이
- Worker와 Service는 Workflow 상태를 직접 변경하지 않음

**근거:** 기존 원칙인 `Workflow Engine = 상태 관리`, `Service/Worker = 작업 수행`을 유지한다.

### 5. Retry / Attempt

- 기술적 실행 1회 = `NodeExecutionAttempt` 1개
- 최대 시도 횟수: **3회**
- Retry 대상:
  - timeout
  - network error
  - 5xx
  - 일시적 외부 Provider 오류
- Retry 비대상:
  - validation error
  - 잘못된 입력
  - Provider 설정 누락
  - 잘못된 BGM Asset 등 영구 오류

**근거:** 기술적 재시도 이력을 DB에 남기되 무제한 Retry는 방지한다.

### 6. SSE 진행 이벤트

퍼센트 대신 실제 작업 단계를 전달한다.

```text
QUEUED
→ SCENE_GENERATION
→ TTS_GENERATION
→ COMPOSING
→ COMPLETED
```

실패 시:

```text
FAILED
```

예시 payload:

```json
{
  "workflow_execution_id": 101,
  "node": "video_generation",
  "stage": "SCENE_GENERATION",
  "message": "Scene 2 영상을 생성하고 있어요."
}
```

**근거:** AI 영상 생성은 단계별 소요 시간이 달라 정확한 퍼센트보다 실제 진행 단계가 더 신뢰할 수 있다.

### 7. PostgreSQL / Redis / SSE 역할

- **PostgreSQL:** Workflow 영구 상태의 Source of Truth
- **Redis:** Celery Queue + 실시간 진행 이벤트
- **SSE:** Redis 진행 이벤트를 Backend → Frontend로 전달

**근거:** 영구 상태와 실시간 이벤트의 책임을 분리한다.

## Architecture Rules

- PostgreSQL = Source of Truth 유지
- Workflow Engine만 Workflow 상태 변경
- Celery 내부 상태와 VORA Domain 상태 분리
- `Revision != Attempt` 원칙 유지
- 기존 `VideoGenerationService / VideoProvider / TTSProvider / FFmpegVideoComposer` 구조 유지
- fake success 금지

## Non-goals

- 다른 Node의 공통 Async Framework
- 별도 Async Job DB Table
- Scene-level Revision
- 정확한 퍼센트 진행률
- AI BGM
- SNS Publication
- AWS 배포
- 실제 Runway / ElevenLabs API Key 발급 및 실호출

## Definition of Done

- Script 승인 후 API 요청이 영상 생성 완료까지 blocking되지 않음
- `video_generation`이 Redis/Celery Queue에 enqueue됨
- Celery Worker가 `VideoGenerationService` 실행
- Retry 가능한 오류는 최대 3회까지 재시도되고 각 시도는 별도 Attempt로 기록
- Retry 불가능 오류는 즉시 실패 처리
- Worker 결과를 받은 Workflow Engine이 상태 처리
- 성공 시 `video_generation = WAITING_APPROVAL`
- SSE로 `QUEUED → SCENE_GENERATION → TTS_GENERATION → COMPOSING → COMPLETED/FAILED` 전달
- 기존 Workflow / Revision / Video Generation 회귀 테스트 유지
- `python -m pytest` PASS
- `python -m ruff check .` PASS
- `python -m mypy app` PASS
- DB Schema 변경 시에만 Alembic Migration 추가

## Codex Completion Report

완료 후 아래만 보고할 것.

1. 변경 파일
2. 구현 내용
3. Redis/Celery/SSE 실행 흐름
4. Retry / Attempt 처리
5. 테스트 결과
6. DB Schema / Migration 변경 여부
7. 남은 문제
8. Task Spec 이탈 또는 Architecture 변경 여부
9. 핵심 Diff 요약
