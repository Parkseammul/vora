# VORA Task Spec — Instagram Reels 안전 게시

> **일자:** 2026-09-16  
> **브랜치:** `feat/instagram-reels-integration`  
> **상태:** 구현 요청용 · 실제 Meta 게시 제외  
> **한 줄 목표:** Instagram Reels를 **처리 완료 후 게시**하고, 장애 시 **중복 게시 없이 이력을 조회·복구**할 수 있게 한다.

---

## 1. 배경과 범위

YouTube 비공개 게시 E2E는 성공했다. Instagram은 OAuth·Publication·Celery·S3의 기본 코드만 있으며 **실제 OAuth/게시 E2E는 미검증**이다.

현재 Instagram 코드에는 다음 문제가 있다.

| 현재 문제 | 이번 작업에서 해결할 내용 |
|---|---|
| Container 생성 직후 `media_publish` 실행 | `FINISHED` 확인 후 게시 |
| Timeout 등에서 전체 게시 흐름 자동 재시도 | 외부 쓰기 결과 불명확 시 자동 재시도 중지 |
| Container ID 미저장 | ID와 진행 상태를 PostgreSQL에 저장하고 조회 기반 복구 |

**범위:** 기존 Instagram Provider, Publication/Attempt, Worker, 필요한 최소 Storage 점검 및 Mock 테스트.  
**근거:** 기존 아키텍처를 재사용하면서 실제 게시 전의 P0 위험만 먼저 해결한다.

## 2. 확정 설계와 근거

| 결정 | 간략 근거 |
|---|---|
| **Instagram API with Instagram Login 유지** | 기존 OAuth 코드 활용. Facebook Login의 Page Token/권한 모델과 혼용하지 않음. |
| **Workflow와 Publication 분리 유지** | 영상 생성 성공과 SNS 게시 성공은 별개. Workflow Engine 수정 불필요. |
| **PostgreSQL = Source of Truth** | Worker 재시작 후에도 Container·게시 진행 상태 확인 가능. |
| 기존 `SocialPublicationAttempt.metadata_` JSONB 우선 활용 | 불필요한 신규 테이블·마이그레이션 방지. 단, 안전성에 부족하면 먼저 보고. |
| **사용자의 별도 Publish Action 필수** | 영상 최종 승인만으로 자동 게시하지 않음. 기존 승인 범위 안에서 Container 완료 후 게시까지 진행. |
| 전송 불명확 시 자동 재시도 금지 | POST 응답 유실은 성공/실패 미확정이므로 반복 전송 시 중복 위험. |
| 실패 Publication/Attempt 이력 보존 | 원인 분석과 복구 경로 추적. 기존 YouTube 기능은 변경하지 않음. |

**API 계약 확인:** 구현 전 Meta **Instagram Login 공식 문서**로 API 호스트·버전·토큰 종류·권한·Endpoint·Container 상태값을 확인한다. 확인 불가 항목은 추정 구현하지 말고 블로커로 보고한다.

## 3. 구현 요구사항

### A. 게시 단계 분리

```text
사용자 Publish → Publication PENDING → Celery
  → S3 영상/접근 URL 확인
  → Container 생성 → Container ID DB commit
  → 상태 조회(유한 polling) → FINISHED
  → 게시 요청 의도 기록 → media_publish
  → Media ID 저장 → 가능한 범위에서 Media 재조회
  → 성공 확인 후 Publication SUCCESS
```

- `IN_PROGRESS`는 제한된 간격·횟수로 조회하고, `FINISHED`에서만 `media_publish`한다.
- `ERROR`/`EXPIRED` 및 Polling 시간 초과는 기록하고 **새 Container를 자동 생성하지 않는다**.
- Meta 공식 문서에서 지원하는 기게시 상태가 확인되면 재게시 대신 기존 게시 결과를 조회한다.
- `media_publish` 응답 ID와 **실제 게시 재조회 결과**를 구분한다. 확인된 permalink만 `external_post_url`에 저장한다.

**근거:** Meta의 영상 처리는 비동기이며, 반환 ID만으로 최종 조회 검증까지 완료했다고 단정할 수 없다.

### B. 영속 상태와 재개

기존 Attempt의 `metadata_` 등에 필요한 최소 정보만 저장한다.

- `container_id`, 마지막 확인 상태
- 게시 요청 의도/시각, 확보한 `media_id`
- 불명확 결과 및 외부 상태 대조 필요 여부

**필수 규칙:** ID를 확보하면 다음 단계 진행 **전에 commit**한다. 재개 시 기존 Container/Media ID부터 확인하고 새로 만들거나 게시하지 않는다. 현재 `PUBLISHING` 상태가 Worker 재개를 막는 경로도 점검한다.

**한계:** 외부 생성은 성공했지만 ID 응답이 유실되거나 DB 저장 전에 종료되면 ID를 모를 수 있다. 이 상황에서 복구 성공을 주장하거나 자동 재생성하지 말고 수동 확인 대상으로 남긴다.

**근거:** 외부 API 호출과 DB commit은 하나의 트랜잭션이 아니므로, 중간 장애를 구분해야 한다.

### C. 실패·재시도 정책

| 발생 위치/상황 | 처리 |
|---|---|
| Container 생성·`media_publish`의 Timeout/Network/429/5xx | `delivery_unknown` 기록, **외부 POST 자동 재시도 금지** |
| Container ID 확보 후 Worker 중단 | DB의 ID로 상태 조회 후 안전한 단계에서 재개 |
| 게시 요청 이후 응답 불명확 | Container/Media 상태 대조. 게시 여부 확정 전 POST 반복 금지 |
| Media ID 확보 후 조회 실패 | ID 보존, 조회만 재시도 가능. `media_publish` 반복 금지 |
| 명확한 권한·요청 오류 | 실패 이력 보존, 사용자 조치 필요 표시 |

기존 DB 상태(`FAILED` 등)만으로 불명확성을 표현하기 어렵다면 Attempt의 `error_code`와 안전한 설명으로 명시한다. 새 상태·DB 구조·재게시 정책이 필요하면 **임의 확정하지 말고 선택지와 근거를 보고**한다.

실패 후 **새 Publication을 생성하는 재게시**는 사용자 명시적 승인 시에만 허용한다. 조회만으로 확정할 수 없는 상황에서는 미게시로 단정하지 않는다.

### D. S3·보안 사전 점검

- Private S3 객체 존재, MIME(`video/mp4`), HTTPS, Presigned GET 접근·유효기간을 확인한다.
- Worker가 로컬 파일에 의존하는 경로와 URL 만료 위험을 점검한다.
- **Access/Refresh Token, 비밀키, Presigned URL 원문을 로그·DB·보고서에 남기지 않는다.**
- TTL·Cloud 저장 계약 변경이 필요하면 최소 수정안과 이유를 보고하고 이번 범위를 임의 확장하지 않는다.

**근거:** Meta가 영상을 가져오지 못하면 Container 처리에 실패하며 URL 유출은 비공개 자산 접근 위험이다.

## 4. 검증 및 완료 기준 (DoD)

**Meta HTTP Mock 테스트:**

- [ ] `IN_PROGRESS → FINISHED → media_publish`, 오류·만료·Polling 종료
- [ ] Container ID / Media ID 영속화 및 기존 ID를 이용한 재개
- [ ] Container 생성·게시 단계별 Timeout/429/5xx → `delivery_unknown`, POST 1회만 호출
- [ ] 게시 응답 유실 또는 최종 Media 조회 실패 시 중복 게시하지 않음
- [ ] 사용자 Publish 승인 필수, 실패 이력 보존, 비밀정보 비노출
- [ ] 기존 YouTube 관련 테스트 회귀 없음

**Harness (`backend/`):**

```powershell
python -m pytest
python -m ruff check .
python -m mypy app
git diff --check
```

DB 구조 변경이 승인되어 실제 발생한 경우에만 Alembic Migration 검증을 추가한다. **Mock PASS를 실제 Instagram E2E 성공으로 표기하지 않는다.**

## 5. 제외 범위 · 작업 종료 규칙

**이번에 하지 않음:** 실제 Meta OAuth/게시, OAuth 토큰 자동 갱신, 전체 Cloud 배포, 전면적 OAuth/Frontend 개편, Workflow Engine 수정, Notion/WBS 수정.

**Codex 진행 순서:** `AGENTS.md·기존 Task Spec 확인 → 공식 API 계약 확인 → 최소 구현 → Mock 테스트/Harness → Diff 보고`.

보고 내용은 **변경 파일 / 핵심 Diff / 테스트 PASS·FAIL / 미검증 사항 / 설계 결정 필요 항목**으로 간략히 정리한다. 중요 정책이나 DB 변경이 필요하면 구현을 멈추고 사용자에게 보고한다. **Commit·Push·PR·Merge 및 실제 SNS 게시 금지.**
