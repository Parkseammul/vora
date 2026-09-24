# VORA Development Harness

[AGENTS.md](../../AGENTS.md)가 최상위 AI 규칙이며, 각 작업의 범위와 완료 조건은
[tasks/](../../tasks/)의 해당 Task Spec이 정한다. Harness는 그 작업의 검증 결과를 제공한다.

사용자 = Architect / Reviewer, Codex = Implementer, Harness = Automated Verification.

1. AGENTS.md 확인
2. 관련 Harness 문서 확인: [Architecture](architecture-rules.md), [Quality Gates](quality-gates.md), [Recovery](failure-recovery-rules.md)
3. Task Spec 확인
4. 관련 코드/테스트 분석
5. Task 범위 구현
6. Harness 실행
7. Diff Review
8. 해당 Task Spec의 Completion Report 형식으로 결과 보고
9. PR

실행 예: 저장소 루트에서 `powershell -NoProfile -File scripts/harness.ps1 -Scope Backend`.
준비 조건, 범위 선택, DB 검증과 결과 해석은 Quality Gates를 따른다.
이번 Bootstrap은 문서와 검증 스크립트만 구축한다. Celery Job Recovery 구현은 별도 Task다.
