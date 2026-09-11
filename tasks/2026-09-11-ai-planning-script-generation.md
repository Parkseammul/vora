# VORA 9/11 Task Spec
## AI 기획 + 대본 생성 Node

### Goal

`input_analysis` 결과를 받아 실제 LLM 기반으로

`content_planning → 사용자 승인 → script_generation → 사용자 승인`

까지 동작하게 한다.

오늘 범위는 **최초 생성 흐름**만 구현한다.

**근거:** 9/11 WBS의 Definition of Done은 기획/대본 생성 Node 동작, Structured Output 검증, LLM Provider Adapter 경계, 실패/형식 오류 테스트까지다.

---

### Content Planning

#### ContentPlanningResult

```text
concept
hook
key_message
cta
visual_style
bgm_direction
scenes[]
```

#### ScenePlan

```text
scene_id
purpose
main_objects
description
duration_seconds
source_asset_id
visual_direction
transition_to_next
```

규칙:

- `scene_id`는 Backend가 생성한다.
- Scene 배열 순서가 실제 재생 순서다.
- `duration_seconds > 0`
- 소수점 최대 1자리
- `source_asset_id = FileAsset.id | null`
- Transition은 `CUT / FADE / ZOOM`
- 마지막 Scene만 `transition_to_next = null`

**근거:** Scene 순서와 식별자를 분리해 이후 재정렬/부분 수정 시 동일 Scene을 안정적으로 추적하기 위함이다.

---

### Content Planning Business Rules

최초 AI 생성 시:

- 입력 이미지 Asset은 전부 사용한다.
- `source_asset_ids` 순서를 유지한다.
- 동일 `FileAsset.id`를 여러 Scene에 임의 재사용하지 않는다.
- Scene 수는 입력 이미지 수 이상이어야 한다.
- 추가 Scene은 `source_asset_id = null` 가능하다.
- Text-only이면 모든 Scene에서 `source_asset_id = null` 가능하다.
- Scene duration 합은 `InputAnalysisResult.duration_seconds`와 같아야 한다.

Asset 검증:

1. FileAsset 존재
2. 현재 WorkflowExecution 소유
3. 현재 입력의 `source_asset_ids`에 포함

**근거:** 사용자가 업로드한 이미지와 순서는 명시적 요구사항이며, AI가 임의로 누락하거나 재배치하면 안 된다.

---

### Script Generation

승인된 `ContentPlanningResult`를 입력으로 사용한다.

#### ScriptGenerationResult

```text
scenes[]
```

#### ScriptScene

```text
planning_scene_id
narration
subtitle
speaking_style
emphasis_keywords
```

규칙:

- Planning Scene ↔ ScriptScene은 1:1
- 모든 `planning_scene_id`를 정확히 한 번 참조
- 누락/중복/존재하지 않는 ID 금지
- Planning Scene 순서를 유지
- `narration = null` 가능
- `subtitle = null` 가능
- 둘 다 null인 순수 영상 Scene 허용
- narration이 null이면 speaking_style도 null
- `emphasis_keywords = []` 허용

**근거:** Script가 Planning 구조를 바꾸지 않고 승인된 기획을 그대로 실행하도록 하기 위함이다.

---

### Duration Validation

Narration은 대응하는 Planning Scene 시간 안에 들어와야 한다.

```text
estimated_tts_duration <= scene.duration_seconds
```

Subtitle도 Scene 시간 안에 읽을 수 있는 길이인지 검증한다.

실제 TTS 생성은 오늘 구현하지 않고 Fake/Interface 기반으로 검증한다.

**근거:** Script 단계가 승인된 Planning 시간을 임의 변경하지 않게 하기 위함이다.

---

### LLM Provider Adapter

Business Service가 Vendor SDK를 직접 호출하지 않는다.

```text
ContentPlanningService
ScriptGenerationService
        ↓
LLMProvider
        ↓
OpenAI / Gemini / Claude
```

#### LLMProviderType

```text
OPENAI
GEMINI
CLAUDE
```

`provider`는 Enum, `model`은 문자열로 둔다.

공통 호출 계약:

```text
generate_structured(
    prompt,
    response_model,
    provider,
    model,
    images=[]
)
```

반환:

```text
data
metadata
```

metadata:

```text
provider
model
input_tokens
output_tokens
cost
```

Prompt는 각 Service가 만든다.

```text
Service = 무엇을 생성할지 결정
Provider = 어느 LLM에 어떻게 호출할지 처리
```

**근거:** VORA 비즈니스 규칙과 외부 LLM Vendor 의존성을 분리하고, 이후 Multi-LLM 교체/비교를 쉽게 하기 위함이다.

---

### Failure Handling

Service가 Retry를 직접 수행하지 않는다.

```text
Service
→ LLMProvider
→ 오류를 공통 LLM 오류로 변환
→ Workflow 실행 계층에서 실패 기록
```

Retry / Attempt 책임은 Workflow Engine / Node 실행 계층에 둔다.

오늘 Celery Retry 전체 구현은 하지 않는다.

**근거:** 기존 VORA 규칙인 `Attempt = 시스템 재시도`를 유지하고 Business Service와 실행 제어 책임을 섞지 않기 위함이다.

---

### Node Executor 연결

기존 fake 결과를 실제 Service 호출로 교체한다.

```text
content_planning
→ ContentPlanningService
→ LLMProvider
→ ContentPlanningResult
→ WAITING_APPROVAL
```

기획 승인 후:

```text
script_generation
→ ScriptGenerationService
→ LLMProvider
→ ScriptGenerationResult
→ WAITING_APPROVAL
```

`input_analysis` 기존 동작과 Workflow 정의는 유지한다.

**근거:** 기존 Workflow Engine 계약을 유지하면서 AI Node 구현만 교체하기 위함이다.

---

### 예상 파일

새 파일:

```text
backend/app/content_planning.py
backend/app/script_generation.py
backend/app/llm_provider.py

backend/tests/test_content_planning.py
backend/tests/test_script_generation.py
backend/tests/test_llm_provider.py
```

수정 가능:

```text
backend/app/node_executors.py
backend/app/config.py
backend/requirements.txt
backend/tests/test_workflow_engine.py
```

DB Schema 변경이 없다면 Migration은 만들지 않는다.

**근거:** 오늘 Task에 필요한 최소 변경만 수행하고 불필요한 구조 확장을 막는다.

---

### Harness

성공 케이스:

- Text-only Content Planning 생성
- 이미지 1~6장 모두 사용 및 순서 유지
- Scene duration 합 검증
- Transition 규칙 검증
- Backend scene_id 생성
- Planning → WAITING_APPROVAL
- 승인 후 Script Generation 실행
- Planning Scene ↔ ScriptScene 1:1
- narration/subtitle nullable 규칙
- TTS/Subtitle 시간 검증
- Script → WAITING_APPROVAL
- LLM 결과 + metadata 반환

실패 케이스:

- 존재하지 않는/다른 Workflow 소유 Asset
- 입력 Asset 누락/순서 위반/동일 ID 중복 사용
- duration 합 불일치
- 잘못된 Transition
- Structured Output 형식 오류
- Planning Scene 누락/중복/nonexistent 참조
- narration null인데 speaking_style 존재
- narration/subtitle 시간 초과
- Provider 호출 오류

**근거:** 정상 경로뿐 아니라 잘못된 AI 출력과 외부 Provider 실패에서도 Workflow 상태와 데이터 계약이 깨지지 않는지 검증하기 위함이다.

---

### Non-goals

9/11에는 구현하지 않는다.

```text
사용자 수정 요청 API
Revision 수정 흐름
부분 재생성
영향 범위 분석
Asset 활성/비활성/재정렬
NEED_USER_INPUT
수정 요청 → 재실행 시작 Node 판단
부분 Workflow 재실행
video_generation 실제 구현
실제 TTS Provider 연동
```

위 항목은 9/12 이후 Task로 분리한다.

**근거:** 오늘 WBS 범위를 넘겨 구현 범위를 키우지 않는다.

---

### Definition of Done

```text
input_analysis
→ 실제 content_planning 생성
→ Structured Output 검증
→ WAITING_APPROVAL

기획 승인
→ 실제 script_generation 생성
→ Structured Output 검증
→ WAITING_APPROVAL
```

추가 완료 조건:

- LLM Provider Adapter 경계 유지
- OPENAI / GEMINI / CLAUDE 선택 구조
- provider + model 선택 가능
- data + metadata 반환
- 실패/형식 오류 테스트 통과
- 기존 Input Analysis / Workflow Engine regression 없음

최종 검증:

```bash
python -m pytest
python -m ruff check .
python -m mypy app
```
