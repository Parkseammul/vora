# 2026-09-15 VORA Publication + SNS + Cloud Task Spec

## 1. 목표

최종 승인된 VORA 영상을 사용자가 명시적으로 게시하고, YouTube/Instagram 게시 상태를 플랫폼별로 독립 추적한다.

최종 완료 기준:

```text
Browser Request
→ Content Planning
→ 승인
→ Script
→ 승인
→ Video
→ 최종 승인
→ Publish
→ YouTube + Instagram 실제 게시
→ 플랫폼별 SUCCESS/FAILED 확인
→ 실제 게시물 확인
```

---

## 2. 핵심 설계 결정

### SNS 계정
- 사용자당 `YouTube 1개 + Instagram 1개`
- 동일 플랫폼 재연결 시 기존 `SocialAccountConnection` 갱신

**근거:** MVP에서 멀티계정 관리보다 OAuth/게시 흐름 검증이 우선이다.

### 게시 실행
- 최종 승인 후에도 자동 게시하지 않는다.
- 사용자가 별도 `Publish` 버튼을 눌러야 Publication을 시작한다.
- YouTube / Instagram은 각각 독립된 Publication으로 처리한다.

**근거:** Workflow 성공과 외부 SNS 게시 성공/실패를 분리한다.

### 게시 문구
- AI가 초안을 생성한다.
- 사용자가 게시 전에 수정할 수 있다.
- YouTube: `title + description`
- Instagram: `caption`

**근거:** AI 자동화와 사용자 최종 통제를 모두 유지한다.

### 중복 게시
- 일반 Publish는 idempotency로 중복 게시를 막는다.
- 사용자가 명시적으로 `다시 게시`를 선택한 경우 새 Publication을 생성한다.

**근거:** 실수로 인한 중복 게시를 막되 의도적인 재게시까지 제한하지 않는다.

### YouTube
- MVP 업로드는 항상 `private`
- 실제 테스트 계정에 업로드한다.

**근거:** 실제 API 게시를 검증하면서 외부 공개 위험을 줄인다.

### Instagram
- 테스트 전용 Instagram Professional 계정에 실제 Reels 게시
- 최종 Video는 S3 private 저장
- Instagram 전달 시 Presigned URL 사용

**근거:** Instagram이 접근 가능한 URL은 필요하지만 원본 영상을 public으로 둘 필요는 없다.

---

## 3. Publication 상태 / Retry

### DB 상태

```text
PENDING
PUBLISHING
SUCCESS
FAILED
```

- `PUBLISHING`은 DB에 저장
- `RETRYING`은 Publication status에 넣지 않고 Attempt/SSE에서 표현

**근거:** DB에는 복원해야 하는 비즈니스 상태를 남기고, 기술적 재시도는 실행 이력으로 분리한다.

### SocialPublicationAttempt

Publication마다 기술적 시도 이력을 별도로 저장한다.

```text
SocialPublication #21
status = SUCCESS

├─ Attempt #1 FAILED (timeout)
├─ Attempt #2 FAILED (HTTP 503)
└─ Attempt #3 SUCCESS
```

**근거:** 외부 API 게시 실패와 Retry 이력을 영구적으로 추적할 수 있어야 한다.

### Retry 정책

자동 Retry 대상:
- timeout
- network error
- HTTP 5xx
- 일시적 Provider 오류

수동 Retry 대상:
- OAuth 만료
- 권한 문제
- HTTP 4xx
- 잘못된 요청/계정 설정

Retry 횟수:
- 최초 포함 총 3회
- Retry 간격 3초 고정

**근거:** MVP에서는 복잡한 Backoff보다 명확한 실패/재시도 정책 검증이 우선이다.

---

## 4. 비동기 처리 / SSE

Publication은 Celery 비동기로 실행한다.

```text
Publish 요청
→ SocialPublication PENDING
→ Celery enqueue
→ Worker PUBLISHING
→ 외부 API 호출
→ SUCCESS / FAILED
```

Frontend에는 SSE로 플랫폼별 상태를 실시간 전달한다.

```text
YouTube    SUCCESS
Instagram  RETRYING

↓

YouTube    SUCCESS
Instagram  FAILED
```

**근거:** 영상 업로드와 외부 API 처리는 오래 걸릴 수 있으므로 HTTP 요청과 실행을 분리한다.

---

## 5. Storage

Storage Provider를 추상화한다.

```text
StorageProvider
├─ LocalStorageProvider
└─ S3StorageProvider
```

- Local 개발/E2E: Local
- Cloud: S3
- 이번 범위에서 S3 업로드가 필수인 대상은 최종 VIDEO

**근거:** 기존 로컬 E2E를 깨지 않고 Cloud 환경에서 S3를 사용할 수 있어야 한다.

---

## 6. Frontend

### SNS 연결
두 진입점을 제공한다.

- `/settings/social`
- `/video` 화면의 미연결 계정 연결 버튼

연결 로직/API는 공통으로 사용한다.

### 게시 UX

```text
최종 승인 영상
→ YouTube / Instagram 선택
→ AI 게시 문구 초안
→ 사용자 수정
→ 게시 버튼
→ 확인 Modal
→ 최종 게시
```

게시 상태는 플랫폼별로 표시한다.

```text
YouTube    ✅ 성공
Instagram  ❌ 실패  [다시 시도]
```

**근거:** 플랫폼별 Publication이 독립적이므로 UI도 성공/실패를 독립적으로 보여줘야 한다.

---

## 7. Cloud 범위

이번 Cloud 목표는 전체 VORA E2E가 외부 환경에서 실행되는 것이다.

포함:
- React Frontend AWS 배포
- FastAPI → ECS Fargate
- Celery Worker → ECS Fargate
- PostgreSQL → RDS
- Redis → ElastiCache
- File/최종 Video → S3
- 주요 로그 → CloudWatch

이번 범위 제외:
- MCP Server Cloud 배포
- OpenTelemetry
- Prometheus
- Loki
- Tempo

**근거:** 포트폴리오 핵심 Workflow/비동기/Publication Cloud E2E에 필요한 인프라만 우선한다.

---

## 8. 구현 순서

1. `SocialPublication` 상태 확장 (`PUBLISHING`)
2. `SocialPublicationAttempt` 추가 + Alembic
3. Publication Service / Retry / Idempotency
4. Celery Publication Task
5. Publication SSE
6. OAuth2 + SocialAccountConnection 갱신
7. YouTube private 업로드
8. StorageProvider + S3 final video upload
9. Instagram Reels + Presigned URL
10. Frontend SNS 연결 / 게시 Modal / 플랫폼별 상태
11. AWS 배포
12. Cloud Full E2E

---

## 9. Definition of Done

- [ ] YouTube/Instagram OAuth2 계정 연결 가능
- [ ] 플랫폼당 계정 1개 유지
- [ ] 최종 승인 전에는 게시 불가
- [ ] Publish는 Workflow와 독립 실행
- [ ] YouTube private 실제 업로드 성공
- [ ] Instagram 테스트 계정 Reels 실제 게시 성공
- [ ] 플랫폼별 Publication 상태 독립 저장
- [ ] `SocialPublicationAttempt`에 Retry 이력 저장
- [ ] 기술 오류 총 3회 시도 / 3초 간격
- [ ] OAuth/4xx 오류는 사용자 조치 후 수동 Retry
- [ ] 중복 Publish 기본 차단
- [ ] 명시적 재게시 허용
- [ ] Publication Celery 비동기 실행
- [ ] Publication SSE 실시간 상태 표시
- [ ] Local/S3 Storage Provider 분리
- [ ] 최종 Video S3 private 저장 + Presigned URL
- [ ] AWS에서 Frontend + Backend + Worker + RDS + Redis + S3 실행
- [ ] Browser Request부터 YouTube/Instagram 실제 게시까지 Cloud Full E2E 통과

---

## 10. 변경 금지 원칙

- Workflow Engine이 기존 Workflow 상태의 Source of Truth 역할을 유지한다.
- Publication은 Workflow 성공 여부와 별도 Aggregate로 유지한다.
- 최종 영상 승인만으로 자동 게시하지 않는다.
- Retry와 사용자 Revision 개념을 섞지 않는다.
- OAuth access/refresh token을 로그에 출력하지 않는다.
- 기존 성공한 Workflow/Revision/Video E2E 동작을 깨지 않는다.
