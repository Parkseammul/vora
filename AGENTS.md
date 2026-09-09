# AGENTS.md

## 프로젝트
VORA는 AI Workflow를 안정적으로 실행·승인·재실행·게시하는 Backend Platform이다.

## 역할
- 사용자는 Architect로서 구조와 핵심 의사결정을 담당한다.
- Codex는 Implementer로서 확정된 설계와 Task Spec에 따라 구현한다.
- 아키텍처 변경이 필요하면 Codex가 임의로 변경하지 않고 사용자에게 보고한다.

## 핵심 아키텍처 규칙
- Workflow / Node / Transition은 Python 코드 정의이며 DB Table이 아니다.
- Workflow Engine만 Workflow 상태 변경을 책임진다.
- Node Executor와 외부 Service는 작업 결과만 반환한다.
- PostgreSQL은 Workflow 영구 상태의 Source of Truth다.
- Redis는 비동기 Queue 용도이며 Source of Truth로 사용하지 않는다.
- Workflow 성공과 SNS 게시 성공은 독립적으로 관리한다.

## 도메인 규칙
- 사용자 수정 Version과 시스템 Retry Attempt를 반드시 분리한다.
- 사용자 수정 → 새로운 NodeExecution + `user_requested_version` 증가.
- Timeout / Network / 5xx Retry → 같은 NodeExecution + 새로운 NodeExecutionAttempt.
- 부분 재실행 시 영향받지 않은 이전 SUCCESS 결과는 재사용한다.
- 최종 영상 승인 후 SNS 게시가 자동 실행되지 않는다.
- SNS 게시는 별도의 사용자 Publish Action으로 실행한다.

## 개발 규칙
- 수정 전 관련 기존 코드를 먼저 확인한다.
- 현재 Task Spec 범위만 구현한다.
- Task와 관계없는 파일은 수정하지 않는다.
- 불필요한 리팩터링이나 추상화를 추가하지 않는다.
- 새로운 Framework / DB / Domain Table을 임의로 추가하지 않는다.
- 기존 사용자의 변경사항을 삭제하거나 덮어쓰지 않는다.
- 이해하기 어려운 비즈니스 규칙에는 간결한 주석을 작성한다.

## 검증
변경 후 가능한 범위에서 아래 검증을 실행한다.

- `python -m pytest`
- `python -m ruff check .`
- `python -m mypy app`
- DB 변경 시 `alembic upgrade head`

검증 실패를 숨기지 않는다.

현재 Task 때문에 발생한 실패는 수정한 뒤 다시 검증한다.

## 작업 완료 보고
작업 완료 시 다음을 보고한다.

1. 변경한 파일
2. 구현한 내용
3. 실행한 검증 명령
4. PASS / FAIL 결과
5. 남은 문제
6. 아키텍처 판단이 필요한 항목
7. 핵심 Diff 요약