# Task: VORA MCP Server + Notion Tools

## 목표

VORA가 Notion의 브랜드/마케팅 자료를 **MCP Tool 형태로 조회·저장**할 수 있도록 MCP Server를 구현한다.

> 근거: 외부 서비스 연동을 Workflow 코드에 직접 묶지 않고 MCP 인터페이스로 분리하기 위함.

이번 Task는 **MCP Server + Notion Tool 구현까지만** 한다.  
Workflow Engine이나 ContentPlanningService 연결은 하지 않는다.

## 구조

```text
VORA Backend
    ↓ 추후 MCP Client
VORA MCP Server
    ↓
Notion API
```

MCP Server는 기존 FastAPI와 별도 실행한다.

```text
FastAPI     : 8000
MCP Server  : 8001
```

> 근거: 기존 Backend 실행과 MCP Tool Server의 책임을 분리하기 위함.

## 구현 Tool

### 1. `get_brand_guide`

Notion의 지정된 브랜드 가이드 페이지를 읽어 plain text로 반환한다.

```text
NOTION_BRAND_GUIDE_PAGE_ID
```

필수 처리:
- block children 조회
- nested block 처리
- pagination 처리

> 근거: 이후 콘텐츠 기획 단계에서 브랜드 톤과 기준을 참고하기 위한 Tool.

### 2. `search_marketing_docs`

```text
query: str
limit: int = 5
```

Notion에서 마케팅 관련 페이지를 검색한다.

반환 예:

```json
[
  {
    "page_id": "...",
    "title": "...",
    "url": "...",
    "excerpt": "..."
  }
]
```

> 근거: 기획 과정에서 필요한 기존 마케팅 자료를 Tool로 탐색하기 위함.

Notion Search를 semantic/vector search처럼 구현하지 않는다.

### 3. `save_content_plan`

```text
title: str
content: str
```

지정된 Notion 부모 페이지 아래에 콘텐츠 기획 페이지를 생성한다.

```text
NOTION_CONTENT_PLAN_PARENT_PAGE_ID
```

반환:

```text
page_id
url
```

> 근거: VORA가 만든 결과를 외부 협업 도구에 저장하는 MCP write Tool 사례를 보여주기 위함.

## 환경변수

```env
NOTION_API_KEY=
NOTION_BRAND_GUIDE_PAGE_ID=
NOTION_CONTENT_PLAN_PARENT_PAGE_ID=
```

API Token은 로그나 에러 메시지에 노출하지 않는다.

## 파일 구조

```text
backend/
  app/
    mcp_server.py
    notion_client.py
  tests/
    test_mcp_server.py
    test_notion_client.py
```

역할:

```text
mcp_server.py
→ MCP Tool 등록

notion_client.py
→ Notion HTTP API 통신 / pagination / block parsing
```

> 근거: MCP protocol 책임과 외부 API 통신 책임을 분리하여 테스트하기 쉽게 유지.

기존 `httpx`를 사용하고 불필요한 Notion SDK는 추가하지 않는다.

## 예외 처리

최소 다음을 처리한다.

```text
환경변수 누락
401 / 403
404
429
5xx
timeout
잘못된 Notion 응답
```

Tool 사용자가 원인을 알 수 있는 명확한 에러를 반환한다.

## 테스트

실제 Notion API는 호출하지 않고 Mock 기반으로 테스트한다.

필수:

```text
get_brand_guide 정상 조회
nested block / pagination
search_marketing_docs 결과 변환
save_content_plan 생성
401 / 404 / 429 처리
환경변수 누락
MCP Tool 등록 확인
```

기존 Backend 전체 테스트도 통과해야 한다.

## 범위 제외

이번 Task에서는 하지 않는다.

```text
Workflow Engine → MCP 연결
ContentPlanningService → MCP 호출
Agent Tool 선택
SKILL.md
OAuth
Vector Search
Frontend
DB Schema / Alembic
```

> 근거: MCP 인프라 구현과 실제 Workflow 적용을 분리해서 변경 범위를 작게 유지하기 위함.

## 완료 조건

```powershell
python -m pytest --basetemp=.test-tmp
python -m ruff check app tests scripts
python -m mypy app
git diff --check
```

MCP Server 로컬 실행까지 확인한다.

완료 보고:
1. 변경 파일
2. 구현 Tool 3개
3. MCP / Notion 구조
4. 테스트 결과
5. DB Schema / Alembic 변경 여부
6. Workflow Engine 변경 여부
