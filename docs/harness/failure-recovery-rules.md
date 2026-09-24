# Failure / Retry / Recovery Rules

상태 소유권과 데이터 경계는 [Architecture](architecture-rules.md)를 따른다.
여기서는 확정된 장애 대응 원칙만 기록한다.

- Technical retry는 같은 NodeExecution의 새 Attempt로 기록한다.
- User revision은 새 logical revision/version으로 기록한다.
- 성공/실패 Attempt와 이전 결과 이력을 삭제하지 않는다.
- Timeout / Network / 5xx 등 retryable error와 사용자 조치가 필요한 오류를 구분한다.
  오류가 기술적이라는 이유만으로 결과가 불명확한 외부 쓰기를 반복하지 않는다.
- 외부 결과 불명확 시 automatic duplicate action을 금지한다.
  Publication delivery ambiguity는 자동 재게시하지 않고, 확보한 외부 식별자와 조회 결과로 확인한다.
- 복구 판단은 PostgreSQL 기록을 기준으로 한다. Redis/Celery 상태만으로 business success를 판단하지 않는다.

## Celery Job Recovery: 확정 범위와 미결정 사항

[후속 Task Spec](../../tasks/2026-09-23-celery-job-recovery.md)은 중복 메시지,
stale RUNNING, commit 이후 enqueue 유실, 불명확한 외부 결과를 처리할 것을 요구한다.
이 요구사항과 위 원칙은 확정되어 있으나, Bootstrap에서 복구 기능이 구현된 것은 아니다.

구체적인 중복 실행 방지 방식, stale 판정 시간/조건, 복구 트리거,
enqueue 유실 탐지 방식, retry limit 및 terminal state 매핑은 후속 Task에서
현재 구현을 분석한 뒤 결정한다. Schema나 아키텍처 변경이 필요하면 Architect에게 보고한다.
이 문서는 새 Table, lock 방식, scheduler 또는 상태 전이를 확정하지 않는다.
