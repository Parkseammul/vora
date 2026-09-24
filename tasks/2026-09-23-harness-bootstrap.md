# Task: VORA AI Development Harness Bootstrap

- Date: 2026-09-23
- Branch: `feat/celery-job-recovery`
- Type: Engineering Process / Harness
- Priority: P0

## 1. Goal

VORA의 AI 개발 방식을 다음 흐름으로 표준화한다.

**Architecture Rules → Task Spec → Codex Implementation → Automated Harness → Diff Review → PR**

현재 `AGENTS.md`는 유지하고, 중복 규칙을 새로 만들지 않는다.
이번 Task는 **Harness 체계를 repo에 정리하는 작업만 수행**하며 Celery Job Recovery 기능 구현은 아직 시작하지 않는다.

## 2. Current Repository Rules

작업 전 반드시 현재 repository의 다음 파일/구조를 확인한다.

- `AGENTS.md`
- `backend/requirements.txt`
- `backend/pytest.ini`
- `backend/alembic.ini`
- `frontend/package.json`
- 기존 `tasks/*.md`
- 기존 테스트/스크립트 구조

추측해서 규칙이나 명령을 추가하지 않는다.

## 3. Target Structure

```text
vora/
├─ AGENTS.md
├─ docs/
│  └─ harness/
│     ├─ README.md
│     ├─ architecture-rules.md
│     ├─ quality-gates.md
│     └─ failure-recovery-rules.md
├─ tasks/
│  └─ 2026-09-23-harness-bootstrap.md
├─ scripts/
│  └─ harness.ps1
└─ tests/
```

필요한 디렉터리가 없으면 생성한다.

## 4. Document Responsibilities

### `AGENTS.md`

기존 내용을 보존한다.

필요하면 아래 내용만 최소 수정한다.

- 상세 Architecture Rule은 `docs/harness/architecture-rules.md` 참조
- 검증 명령/완료 Gate는 `docs/harness/quality-gates.md` 참조
- 장애/Retry/복구 정책은 `docs/harness/failure-recovery-rules.md` 참조
- 각 작업은 `tasks/*.md` Task Spec 범위 안에서 수행

**근거:** 전역 AI 규칙과 상세 기술 규칙을 분리해 중복/불일치를 방지한다.

### `docs/harness/README.md`

Harness의 목적과 실행 흐름만 간결하게 기록한다.

필수 흐름:

1. `AGENTS.md` 확인
2. 관련 Harness 문서 확인
3. Task Spec 확인
4. 관련 코드/테스트 분석
5. Task 범위 구현
6. Harness 실행
7. Diff Review
8. 결과 보고
9. PR

사용자 = Architect / Reviewer
Codex = Implementer
Harness = Automated Verification

### `architecture-rules.md`

현재 VORA에서 이미 확정된 불변식만 기록한다.

반드시 포함:

- PostgreSQL = durable Source of Truth
- Redis = Queue / Result / PubSub, business SoT 아님
- Workflow / Node / Transition = Python code-defined
- Workflow Engine만 Workflow 상태 전이를 책임
- Revision != Attempt
- Partial rerun은 최초 영향 Node부터
- 이전 SUCCESS 결과 재사용
- Publication은 generation과 독립
- 최종 승인 후 자동 SNS 게시 금지
- 불명확한 외부 게시 결과 자동 재게시 금지

새로운 설계를 임의로 확정하지 않는다.

### `quality-gates.md`

현재 repo에서 실제 실행 가능한 검증 명령을 기준으로 작성한다.

Backend 기본 Gate:

- `python -m pytest`
- `python -m ruff check .`
- `python -m mypy app`
- DB 변경 시 Alembic 검증
- `git diff --check`

Frontend 변경 시:

- `npm run lint`
- `npm test`
- `npm run build`

규칙:

- Task 때문에 발생한 실패는 수정한다.
- 기존 실패가 있다면 숨기지 않고 별도 보고한다.
- 실행하지 못한 Gate는 이유를 보고한다.
- 모든 Gate를 실행했다고 허위 보고하지 않는다.

### `failure-recovery-rules.md`

현재 확정된 복구 원칙만 기록한다.

- technical retry = 새 Attempt
- user revision = 새 logical revision/version
- 실패 이력 삭제 금지
- retryable error와 user-action-required error 구분
- 외부 결과 불명확 시 automatic duplicate action 금지
- Publication delivery ambiguity는 자동 재게시 금지
- PostgreSQL 기록을 기준으로 복구 판단

Celery Job Recovery의 세부 구현안은 **확정된 사실과 미결정 사항을 구분**한다.

## 5. `scripts/harness.ps1`

Windows PowerShell에서 repo 검증을 한 번에 실행할 수 있게 만든다.

요구사항:

- repo root 기준으로 동작
- 명령 실패 시 non-zero exit
- 어떤 단계에서 실패했는지 명확히 출력
- Backend / Frontend / All 범위를 선택할 수 있도록 단순한 parameter 제공
- Backend:
  - pytest
  - Ruff
  - mypy
  - git diff --check
- Frontend:
  - lint
  - test
  - build
  - git diff --check
- DB schema 변경 검증은 필요 시 실행할 수 있게 별도 option 또는 명확한 안내 제공

과도한 framework나 dependency를 추가하지 않는다.

**근거:** Harness는 문서가 아니라 실제 PASS/FAIL을 만드는 실행 장치여야 한다.

## 6. Scope Guard

이번 Task에서 하지 않는다.

- Celery Job Recovery 기능 구현
- DB Schema 변경
- Workflow Engine 리팩터링
- AWS 작업
- CI/CD 도입
- GitHub Actions 추가
- 테스트 Framework 교체
- 기존 Business Logic 수정
- Task와 무관한 코드 정리

## 7. Verification

문서/스크립트 작성 후 최소 확인:

1. PowerShell script syntax / 실행 경로 확인
2. 가능한 범위에서 backend Harness 실행
3. `git diff --check`
4. 생성/수정 파일 목록 확인

Frontend를 변경하지 않았다면 Frontend Gate 전체 실행은 필수가 아니며, 실행 여부를 보고한다.

## 8. Completion Report

완료 시 아래 형식으로 보고한다.

1. 생성/수정 파일
2. 각 파일의 역할
3. `AGENTS.md` 변경 여부와 이유
4. 실행한 Harness 명령
5. PASS / FAIL
6. 기존 실패가 있다면 구분
7. 남은 미결정 사항
8. 다음 Task로 `Celery Job Recovery`를 시작해도 되는지
9. `git diff --stat`
10. `git diff --check`

## 9. Definition of Done

- Harness 문서 책임이 서로 중복되지 않는다.
- `AGENTS.md`가 최상위 AI 규칙으로 유지된다.
- Architecture / Quality / Recovery 규칙이 분리돼 있다.
- Task Spec과 Harness의 관계가 README에 설명돼 있다.
- `scripts/harness.ps1`로 실제 검증을 실행할 수 있다.
- 기존 Business Logic / DB Schema는 변경되지 않는다.
- 변경 범위가 Harness 구축에 한정된다.
