# 2026-09-12 수정 요청 기반 부분 재실행

## 목표

사용자의 자연어 수정 요청을 받아 AI가 재실행 시작 Node를 판단하고,
해당 Node부터 새 `user_requested_version`으로 부분 재실행한다.

핵심 원칙:
- 앞쪽 SUCCESS 결과는 재사용
- 기존 결과는 삭제하지 않고 이력으로 보존
- 사용자 수정과 기술 Retry를 분리

---

## 1. Revision API

입력:

```json
{
  "current_node": "script_generation",
  "revision_request": "첫 문장을 더 강하게 바꿔줘"
}
```

- `current_node`: 사용자가 현재 보고 있던 결과에 대한 힌트
- `revision_request`: 자연어 수정 요청
- blank / whitespace-only 요청은 거부

권장 API:

```text
POST /workflow-executions/{workflow_execution_id}/revisions
```

**근거:** 사용자가 내부 Workflow 구조를 몰라도 자연어로 수정할 수 있게 한다.

---

## 2. AI가 재실행 시작 Node 판단

AI는 아래 4개 Core Node 중 하나만 반환한다.

```text
input_analysis
content_planning
script_generation
video_generation
```

예:

```text
"타깃을 20대로 바꿔줘"
→ input_analysis

"기획 콘셉트를 바꿔줘"
→ content_planning

"첫 문장을 더 강하게"
→ script_generation

"영상 분위기를 어둡게"
→ video_generation
```

- `current_node`는 판단 힌트일 뿐 target을 강제하지 않음
- 애매한 요청도 가장 가능성 높은 Node 하나를 선택
- Enum / Pydantic으로 4개 외 값은 차단

**근거:** UX는 자연어 중심으로 유지하되, Workflow 실행 범위는 고정된 Node 안에서만 통제한다.

---

## 3. Revision과 Attempt 분리

사용자 수정:

```text
script_generation v1
→ 수정 요청
→ script_generation v2
```

기술적 Retry:

```text
script_generation v2
→ Attempt 1 실패
→ Attempt 2 재시도
```

- 사용자 수정 → 새 `user_requested_version`
- Timeout / Network / 5xx → 같은 Version의 새 `NodeExecutionAttempt`

**근거:** 사용자 의도 변경과 시스템 장애 재시도를 같은 이력으로 섞지 않는다.

---

## 4. 부분 재실행 규칙

Workflow:

```text
input_analysis
→ content_planning
→ script_generation
→ video_generation
```

재실행 시작점:

```text
입력 조건 변경 → input_analysis
기획 변경      → content_planning
대본 변경      → script_generation
영상 변경      → video_generation
```

### 앞쪽 Node

target 이전 최신 SUCCESS 결과는 재실행하지 않고 재사용한다.

예:

```text
대본 수정

input_analysis      → 기존 SUCCESS 재사용
content_planning    → 기존 SUCCESS 재사용
script_generation   → 새 Version 생성
video_generation    → 이후 새 결과 필요
```

**근거:** 승인된 결과를 불필요하게 다시 생성하지 않아 비용과 결과 변동을 줄인다.

### 뒤쪽 Node

기존 downstream 결과는 삭제하지 않는다.

예:

```text
script_generation v1
→ video_generation v1

script_generation v2
→ video_generation v2
```

기존 v1은 이력으로 남기되 현재 유효 결과로 사용하지 않는다.

**근거:** 수정 이력을 보존하면서 변경된 upstream과 예전 downstream 결과가 섞이는 것을 막는다.

---

## 5. Target Node 실행

Target Node는 기존 upstream 결과와 함께 수정 요청을 전달받아야 한다.

개념적으로:

```text
previous_output
+ upstream_input
+ revision_request
→ 새 Version 결과
```

예:

```text
기존 Script
+ 승인된 Planning
+ "첫 문장을 더 강하게"
→ Script v2
```

**근거:** 단순 재생성이 아니라 사용자가 요청한 수정 내용이 실제 결과에 반영되어야 한다.

---

## 6. Workflow Engine 책임

Revision 실행 제어는 Workflow Engine이 담당한다.

필요한 책임:

1. WorkflowExecution 확인
2. AI가 판단한 target_node 검증
3. target 이전 SUCCESS 결과 재사용
4. target부터 새 `user_requested_version` 생성
5. downstream 기존 이력 보존
6. 기존 Approval 규칙에 따라 실행

Service / API Router가 Workflow 상태를 직접 변경하지 않는다.

**근거:** 상태 전이 책임을 Workflow Engine 하나에 유지한다.

---

## 7. LLM Provider

9/11에 만든 `LLMProvider` 구조를 그대로 사용한다.

```text
RevisionImpactService
→ LLMProvider.generate_structured(...)
```

- Prompt: `RevisionImpactService`
- Vendor 호출 / Structured Output 파싱: `LLMProvider`
- Retry: Workflow 실행 계층 책임

새 Workflow Node는 추가하지 않는다.

**근거:** 비즈니스 판단과 Vendor 호출 책임을 분리하고 기존 Adapter 구조를 재사용한다.

---

## 8. 테스트

최소 검증:

- 자연어 요청이 4개 Core Node 중 하나로 분류됨
- 잘못된 target 값은 validation 실패
- blank revision_request 거부
- Script 수정 시 input_analysis / content_planning 재실행 안 함
- 수정 대상 Node의 `user_requested_version` 증가
- 기존 NodeExecution 이력 유지
- 기존 downstream 결과가 현재 결과로 노출되지 않음
- 사용자 Revision과 Attempt 분리 유지
- 기존 Approval 흐름 회귀 없음
- AI Service 미주입 시 fake 성공 없음

전체 검증:

```bash
python -m pytest
python -m ruff check .
python -m mypy app
```

---

## Non-goals

이번 Task에서 제외:

- 실제 Video 생성 구현
- Celery / Redis Retry
- SNS 게시 / OAuth2
- Frontend 구현
- 복잡한 field-level dependency graph
- 새로운 Workflow Node
- 불필요한 DB Schema 변경

---

## Definition of Done

- Revision API 동작
- `current_node + revision_request` 입력
- AI가 재실행 시작 Node 판단
- target 이전 SUCCESS 재사용
- target부터 새 `user_requested_version`
- 기존 결과 overwrite / delete 없음
- downstream 구버전이 현재 결과로 사용되지 않음
- Revision / Attempt 분리 유지
- 기존 Approval 규칙 유지
- pytest / Ruff / mypy PASS
- DB Schema 임의 변경 없음
