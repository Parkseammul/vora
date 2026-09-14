# VORA Frontend Workflow UI Task Spec

## 목표
사용자가 숏폼 생성 요청을 입력하고, Workflow 진행 상태를 확인하며, 기획·대본·최종 영상을 승인/수정할 수 있는 UI를 구현한다.

범위는 **최종 영상 승인까지**이며, SNS Publication은 제외한다.

---

## 1. 화면 구조

```text
/request
/workflows/:workflowExecutionId/plan
/workflows/:workflowExecutionId/script
/workflows/:workflowExecutionId/video
```

- `workflowExecutionId`는 실제 실행 인스턴스인 `WorkflowExecution.id`를 의미한다.
- **근거:** Workflow 정의와 실행 인스턴스를 이름으로 명확히 구분하기 위해서다.

공통 구조:

```text
WorkflowLayout
├─ WorkflowStepper
└─ Outlet
```

- **근거:** 모든 단계에서 동일한 Workflow 진행 상태를 보여주기 위해서다.

---

## 2. Request Page

입력:
- 자연어 Text
- Image 0~6장
- Drag & Drop 이미지 순서 변경
- `[숏폼 만들기]`

영상 길이/톤/타깃은 별도 폼으로 받지 않고 자연어에서 `input_analysis`가 해석한다.

- **근거:** VORA의 핵심 UX인 “말하면, 만들어보라”를 유지하고 중복 입력/충돌 규칙을 만들지 않기 위해서다.

요청 생성 후 바로:

```text
/workflows/:workflowExecutionId/plan
```

으로 이동한다.

- **근거:** 별도 Progress 페이지 없이 사용자가 최종적으로 확인할 기획 화면에서 바로 기다리게 하는 편이 단순하다.

---

## 3. 상태 조회

### Plan / Script
REST Polling을 사용한다.

- `PENDING`, `RUNNING` → 2초마다 재조회
- `WAITING_APPROVAL`, `SUCCESS`, `FAILED` → Polling 중지

- **근거:** 짧게 끝나는 작업은 SSE까지 확장할 필요가 없고, 상태값만 확인하면 충분하다.

### Video
- REST로 최초 상태 조회
- `video_generation` 진행 중에는 기존 SSE 사용
- SSE 완료 이벤트 수신 후 REST 재조회

- **근거:** Redis/SSE는 실시간 알림용이고 PostgreSQL의 durable state가 최종 진실이기 때문이다.

---

## 4. Workflow Stepper

사용자용 단계:

```text
입력 분석
기획
대본
영상
```

사용자용 상태:

```text
대기
진행 중
승인 대기
수정 요청됨
재실행 중
완료
실패
```

백엔드 enum을 그대로 노출하지 않는다.

- **근거:** Revision/부분 재실행 흐름은 보여주되 기술적인 상태명은 숨기기 위해서다.

---

## 5. Plan Page

표시:
- Concept
- Hook
- Key Message
- CTA
- Visual Style
- BGM Direction
- 총 영상 길이
- Scene 개수

Scene 상세 데이터는 노출하지 않는다.

- **근거:** 사용자가 승인에 필요한 핵심 기획만 빠르게 판단할 수 있게 하기 위해서다.

버튼:
- `[승인]`
- `[수정 요청]`

---

## 6. Script Page

대본은 **타임라인형**으로 표시한다.

예:

```text
0~5초
Narration: ...
Subtitle: ...

5~10초
Narration: ...
Subtitle: ...
```

총 영상 길이도 표시한다.

- **근거:** 실제 Scene 흐름과 자연스럽게 연결되고, “첫 5초 수정” 같은 Revision 요청도 명확해진다.

---

## 7. 승인 / 수정 요청

각 승인 화면에서:

```text
[승인] [수정 요청]
```

`수정 요청` 클릭 시 Modal을 띄우고 자연어 수정 요청을 입력한다.

- **근거:** Revision 생성을 명확한 사용자 액션으로 분리하고 기존 화면을 복잡하게 만들지 않기 위해서다.

### 앞 단계부터 재실행되는 경우

예: Script 화면에서 요청했지만 `content_planning`부터 재실행되는 경우

현재 화면은 유지하고 팝업을 띄운다.

```text
기획 단계부터 다시 생성 중입니다.
기획 화면으로 이동하시겠습니까?

[현재 화면에 있기] [기획 화면으로 이동]
```

- **근거:** 사용자를 강제로 이동시키지 않으면서 재실행 범위는 명확히 알려주기 위해서다.

---

## 8. Video Page

생성 중에는 SSE stage를 기반으로 단계형 Progress UI를 표시한다.

```text
✓ 작업 준비
✓ Scene 생성
● 음성 생성
○ 영상 합성
○ 완료
```

현재 작업 설명도 함께 표시한다.

사용 stage:

```text
QUEUED
SCENE_GENERATION
TTS_GENERATION
COMPOSING
COMPLETED
FAILED
```

퍼센트 진행률은 표시하지 않는다.

- **근거:** 실제 퍼센트를 계산하지 않는 구조에서 가짜 진행률을 보여주지 않기 위해서다.

### 영상 완료 후

표시:
- 영상 Player
- 총 길이
- Scene 수
- `[영상 다운로드]`
- `[최종 승인]`
- `[수정 요청]`

승인 전에도 다운로드 가능하다.

- **근거:** 다운로드는 승인과 별개의 사용자 편의 기능이며, 현재 MVP에는 SNS 게시가 아직 없기 때문이다.

---

## 9. 조회 API 구조

혼합형으로 구성한다.

### 공통 상태

```http
GET /workflow-executions/{workflowExecutionId}
```

용도:
- WorkflowLayout
- Stepper
- 전체 Node 상태

### 단계별 상세

```http
GET /workflow-executions/{id}/plan
GET /workflow-executions/{id}/script
GET /workflow-executions/{id}/video
```

- **근거:** 공통 Workflow 상태와 단계별 `output_data`를 분리하면 프론트 타입과 책임이 명확해진다.

---

## 10. Error / Retry UI

사용자에게 기술 오류 원문은 노출하지 않는다.

재시도 중:

```text
영상 생성 중 일시적인 문제가 발생했습니다.
자동으로 다시 시도하고 있어요. (2/3)
```

최종 실패:

```text
영상 생성에 실패했습니다.
잠시 후 다시 시도해주세요.

[다시 시도]
```

- **근거:** Provider/HTTP 오류 등은 Attempt metadata와 로그에 남기고 사용자에게는 필요한 행동만 안내하기 위해서다.

---

## 11. Frontend State

React Context를 사용한다.

`WorkflowContext`:
- workflowExecutionId
- Workflow 상태
- Node 상태
- 현재 단계
- `refetch()`

각 페이지의 상세 결과는 페이지 local state로 관리한다.

- **근거:** Stepper와 각 페이지가 공유하는 상태만 Context에 두고, MVP 단계에서 Zustand 같은 별도 전역 라이브러리는 추가하지 않기 위해서다.

PostgreSQL이 Source of Truth이며 Context는 UI 렌더링용 임시 상태다.

---

## 12. 테스트 범위

핵심 UI 동작을 자동 테스트한다.

포함:
- Request 입력 / 이미지 업로드 / 순서 변경
- Plan / Script REST Polling
- `WAITING_APPROVAL`에서 Polling 중지
- 승인
- 수정 요청 Modal
- 앞 단계 Revision 발생 시 이동 팝업
- Script 타임라인
- Video SSE stage 반영
- RETRYING / FAILED UI
- 최종 영상 / 다운로드 / 승인 UI
- Build / Lint
- Backend 기존 테스트 회귀 확인

브라우저 전체 E2E 자동화는 이번 Task 범위에서 제외한다.

- **근거:** 핵심 Workflow UI 동작은 자동으로 검증하되, 현재 MVP에서 Playwright 환경까지 확장하는 비용은 피하기 위해서다.

---

## Non-goals

이번 Task에 포함하지 않는다.

- SNS Publication
- YouTube / Instagram 게시
- 새로운 Workflow Engine 설계
- PostgreSQL Source of Truth 변경
- 전체 Workflow SSE 전환
- 퍼센트 진행률
- Zustand 등 추가 상태관리 라이브러리
- 브라우저 전체 E2E 자동화
- 불필요한 Backend 리팩터링
