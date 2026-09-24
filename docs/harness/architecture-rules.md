# Architecture Rules

[AGENTS.md](../../AGENTS.md)의 상세 불변식이다. 새 설계를 확정하는 문서가 아니다.
장애 처리 판단은 [Recovery](failure-recovery-rules.md), 검증은 [Quality Gates](quality-gates.md)를 따른다.

| 경계 | 확정된 불변식 |
| --- | --- |
| 영속 상태 | PostgreSQL이 durable Source of Truth다. |
| Redis | Queue / Result / PubSub 용도이며 business Source of Truth가 아니다. |
| Workflow 정의 | Workflow / Node / Transition은 Python code-defined이며 DB Table이 아니다. |
| 상태 전이 | Workflow Engine만 Workflow 상태 전이를 책임진다. Node Executor와 외부 Service는 작업 결과만 반환한다. |
| Revision과 Attempt | 사용자 수정은 새 NodeExecution과 증가한 user_requested_version이다. 기술 Retry는 같은 NodeExecution의 새 NodeExecutionAttempt다. |
| 부분 재실행 | 최초 영향 Node부터 재실행하며 영향받지 않은 이전 SUCCESS 결과를 재사용한다. |
| Publication | generation 성공과 SNS 게시 성공은 독립적으로 관리한다. |
| 게시 시작 | 최종 승인 후 자동 SNS 게시를 하지 않는다. 별도의 사용자 Publish Action이 필요하다. |
| 게시 결과 불명확 | 자동 재게시를 금지한다. 복구 판단은 Recovery 문서를 따른다. |

확인 근거: backend/app/workflow_definitions.py, workflow_engine.py, celery_app.py,
publication.py 및 기존 Revision / Instagram 안전 게시 Task Spec.
