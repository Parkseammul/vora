# Task: VORA Agent Skill (`SKILL.md`)

## 목표

VORA의 마케팅 콘텐츠 생성·수정 흐름을 AI Agent가 일관되게 수행하도록
repo에 재사용 가능한 Agent Skill을 추가한다.

> 근거: Skill은 반복 가능한 작업 절차, 입력, 출력, 최종 검증 기준을 `SKILL.md`로 묶어 재사용하는 방식이다.

이번 Task는 **Skill 문서/리소스 작성까지만** 한다.
Workflow Engine, MCP Server, ContentPlanningService, Frontend에는 연결하지 않는다.

---

## 위치

```text
skills/
  vora-marketing-workflow/
    SKILL.md
```

필요한 경우에만 작은 참고 문서를 같은 폴더 아래 추가할 수 있다.

> 근거: Skill을 독립된 폴더 bundle로 유지하면 repo에서 버전 관리하고 이후 설치/업로드하기 쉽다.

---

## SKILL.md 필수 구성

YAML frontmatter:

```yaml
---
name: vora-marketing-workflow
description: Use VORA to create or revise short-form marketing content while preserving approvals, partial reruns, and explicit publishing.
---
```

본문에는 아래 내용만 포함한다.

### 1. 목적

사용자의 자연어 요청을 기준으로 VORA의 숏폼 제작 Workflow를 진행한다.

```text
요청
→ 입력 분석
→ 콘텐츠 기획
→ 사용자 승인
→ 대본
→ 사용자 승인
→ 영상
→ 최종 승인
→ 별도 게시
```

### 2. 입력

- 사용자 원문 요청
- 선택 이미지
- 선택적으로 타깃 / 길이 / 톤 / 플랫폼 요구사항

사용자의 원문 의도를 임의로 축약하거나 바꾸지 않는다.

### 3. MCP Tool 사용 규칙

사용 가능한 Tool:

```text
get_brand_guide
search_marketing_docs
save_content_plan
```

규칙:

- 브랜드 기준이 필요한 기획에서는 `get_brand_guide`를 우선 사용
- 참고 마케팅 문서가 실제로 필요한 경우에만 `search_marketing_docs` 사용
- `save_content_plan`은 승인된 기획을 저장하거나 사용자가 명시적으로 저장을 요청한 경우에만 사용
- Tool 실패를 숨기지 않고 결과에 명시

### 4. Workflow 규칙

반드시 유지:

- Workflow 상태는 Workflow Engine만 변경
- 사용자 승인 없이 다음 승인 단계로 임의 진행 금지
- Revision과 Retry 구분
- 수정 요청 시 영향을 받는 가장 이른 Node부터 부분 재실행
- 영향 없는 이전 SUCCESS 결과는 재사용
- 최종 영상 승인 후에도 자동 SNS 게시 금지
- 게시에는 별도 사용자 Action 필요

### 5. Revision 판단 예

Planning 수준:

```text
전체 콘셉트 변경
핵심 메시지 변경
타깃 변경
영상 방향 변경
```

→ `content_planning`부터 재실행

Script 수준:

```text
첫 문장 변경
나레이션 수정
자막 수정
```

→ `script_generation`부터 재실행

Video 수준:

```text
영상 장면 표현 수정
영상 결과만 다시 생성
```

→ `video_generation`부터 재실행

### 6. 출력 원칙

Agent는 현재 상태와 다음 사용자 Action을 명확히 안내한다.

예:

```text
기획 생성 완료
현재 상태: 사용자 승인 대기
다음 Action: 승인 또는 수정 요청
```

내부 Workflow 상태와 사용자에게 보여주는 안내를 혼동하지 않는다.

### 7. 완료 체크

완료 전 확인:

```text
사용자 원문 의도가 유지됐는가
필요한 MCP Tool만 호출했는가
승인 단계를 건너뛰지 않았는가
Revision / Retry를 혼동하지 않았는가
불필요한 upstream Node를 재실행하지 않았는가
최종 승인 후 자동 게시하지 않았는가
```

---

## 범위 제외

이번 Task에서는 하지 않는다.

```text
Workflow Engine 코드 변경
MCP Client 연결
ContentPlanningService Tool 호출
Frontend 변경
DB Schema / Alembic
새로운 Provider
Skill 자동 설치
OpenAI Skills API 업로드
```

---

## 검증

- `SKILL.md` frontmatter에 `name`, `description` 존재
- Skill 내용이 현재 VORA 아키텍처 규칙과 충돌하지 않는지 확인
- `AGENTS.md`와 모순 없는지 확인
- 불필요한 구현 코드 변경 없음
- `git diff --check`

Backend 코드 변경이 없다면 전체 pytest 재실행은 필수 아님.

---

## 완료 보고

1. 변경 파일
2. Skill 이름 / description
3. 정의한 Workflow
4. MCP Tool 사용 규칙
5. Revision 판단 규칙
6. AGENTS.md 충돌 여부
7. git diff-check 결과
8. 범위 밖 코드 변경 여부
