# VORA — YouTube OAuth 안전 토큰 갱신 Task Spec

> **일자:** 2026-09-17
> **상태:** Architect 검토용 초안 · 실제 Google API 호출 제외
> **범위:** YouTube OAuth / Publication 토큰 처리 및 Mock 테스트

## 1. 목표

YouTube 게시 전에 유효한 Access Token을 확보한다. 만료가 임박하면 기존 Refresh Token으로 갱신하고, 성공한 토큰을 PostgreSQL에 저장한 다음 **한 번만** 업로드한다.

**근거:** 만료 토큰으로 인한 업로드 실패를 막으면서 기존 `delivery_unknown` 중복 게시 방지 정책을 유지한다.

## 2. 확정 아키텍처 유지

- `SocialAccountConnection`의 기존 `access_token`, `refresh_token`, `token_expires_at`, `status` 사용. 새 테이블·Migration 금지.
- Workflow Engine 변경 금지. PostgreSQL은 영구 상태의 Source of Truth.
- 사용자 Publish Action과 YouTube `private` 게시 정책 유지.
- YouTube 실제 게시 E2E 성공 기능 및 Instagram 코드를 변경하지 않는다.

**근거:** 필요한 필드와 `CONNECTED`/`EXPIRED`/`REVOKED` 상태가 이미 존재한다.

## 3. 제안 정책 — 구현 전 Architect 확인 대상

| 상황 | 제안 동작 | 근거 |
| --- | --- | --- |
| 만료까지 5분 초과 | 저장된 Access Token 사용 | 불필요한 갱신 방지 |
| 만료까지 5분 이하 | 업로드 전에 Refresh 1회 시도 | 업로드 중 만료 위험 감소 |
| 만료 시각 `NULL` | Refresh Token이 있으면 선제 갱신 | 유효기간을 모르는 토큰의 무작정 사용 방지 |
| Refresh Token 없음 / `invalid_grant` | 업로드하지 않고 재연결 필요 오류; 확인 가능한 인증 불가 상태는 기존 `EXPIRED`로 표시 | 복구 불가능한 인증 오류 분리 |
| Refresh 네트워크 오류 / 429 / 5xx | 이번 업로드 시작 전 중단; 자동 갱신 재시도 없음 | 최초 구현의 단순성·예측 가능성 확보 |
| 동시 Worker 갱신 | 해당 DB row를 `SELECT … FOR UPDATE`로 잠그고 잠금 후 만료 재확인 | 중복 갱신 및 토큰 덮어쓰기 방지 |

- DB Lock과 외부 Refresh 요청에는 유한한 timeout을 적용하고, 실패 시 transaction을 rollback해 잠금을 해제한다.
- `EXPIRED`는 확인된 인증 불가에만 사용한다. 일시적 네트워크 장애만으로 `EXPIRED`/`REVOKED`로 변경하지 않는다.
- 새 Status/Version 컬럼을 임의로 추가하지 않는다.

## 4. 구현 흐름

```text
사용자 Publish 승인
  → Publication Worker
  → 동일 SocialAccountConnection row lock
  → 최신 만료 시각 재확인
  → 필요 시 Google refresh_token grant
  → 새 Access Token + 만료 시각 commit
  → lock 해제
  → 기존 YouTube private 업로드
```

- Google Refresh 응답에 새 Refresh Token이 있을 때만 교체한다. 없으면 기존 값을 보존한다.
- **같은 외부 계정 재연결**에서 Refresh Token이 생략되었을 때만 기존 토큰을 보존한다. 외부 계정 ID가 변경되면 이전 토큰을 승계하지 말고 새로운 계정의 offline 인증을 요구한다.
- Refresh 단계와 업로드 POST를 분리한다. 업로드가 시작된 뒤 Timeout/429/5xx 등 `delivery_unknown`이 발생하면 **토큰 갱신을 이유로 자동 재업로드하지 않는다**.
- 토큰, Client Secret, Authorization 헤더를 로그·예외·테스트 출력에 남기지 않는다.

**근거:** 계정 혼동을 막고, 이미 업로드되었을 수 있는 영상을 중복 게시하지 않기 위함.

## 5. 구현 범위

- `social_providers.py`: YouTube Refresh HTTP adapter 및 민감정보 없는 오류 분류.
- Publication token service 또는 기존 service의 최소 변경: row lock, 만료 판정, 영속화.
- `publication_runtime.py`: YouTube 업로드 직전에 유효 토큰 확보.
- `main.py`: 동일 계정 재연결 시 Refresh Token 보존; 다른 계정 토큰 승계 금지.
- 관련 pytest 추가. 구조상 새 service가 필요하면 이유와 최소 변경 범위를 보고한다.

## 6. 테스트 및 완료 조건

- [ ] 유효 토큰은 Refresh API 미호출.
- [ ] 만료 임박/만료 시 Refresh 후 새 토큰·만료 시각 저장.
- [ ] 만료 시각 `NULL` 처리 및 Refresh Token 생략 응답에서 기존 값 보존.
- [ ] 다른 계정 재연결 시 기존 Refresh Token 미승계.
- [ ] Refresh Token 없음, `invalid_grant`, 일시적 네트워크 실패 시 업로드 POST 미호출.
- [ ] 동시 갱신에서 중복 Refresh 방지. 실제 PostgreSQL 동시성 검증이 불가능하면 **미검증**으로 보고하고 Mock만으로 보장한다고 주장하지 않는다.
- [ ] Refresh 성공 뒤 YouTube private 업로드 및 기존 멱등성 유지.
- [ ] 업로드 `delivery_unknown` 시 자동 재업로드 금지.
- [ ] 인증정보 로그·에러 노출 방지 및 Instagram 회귀 없음.
- [ ] `python -m pytest`, `python -m ruff check .`, `python -m mypy app`, `git diff --check` 실행 결과 보고.

## 7. 제외 및 완료 보고

**제외:** 실제 Google OAuth/API 쓰기 요청, 실제 YouTube 재업로드, Instagram 토큰 갱신, 새로운 DB 스키마, Cloud 배포, Notion 변경.

변경 파일·동작·테스트 PASS/FAIL·동시성 검증 여부·핵심 Diff·미해결 사항을 보고한다. **구현 및 검증까지만 수행하며 Commit/Push/PR/Merge 금지.** 사용자 검토 후 별도 진행한다.
