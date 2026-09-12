# Task Spec — 2026-09-13 Video Generation

## Goal

승인된 Planning + Script를 입력으로 Scene별 영상을 생성하고, TTS·자막·BGM을 합성해 최종 9:16 MP4를 만든다.

최종 결과:
- `1080x1920` MP4
- Video `FileAsset` 저장
- `video_generation` → `WAITING_APPROVAL`

## Scope

### 1. Scene별 영상 생성

각 `ScenePlan`을 독립적으로 처리한다.

- `source_asset_id != null`
  - 해당 FileAsset 이미지를 reference image로 전달
  - Scene description / visual_direction 등을 prompt에 반영
- `source_asset_id == null`
  - text prompt only로 영상 생성

영상 Provider 구조:

```text
VideoGenerationService
→ VideoProvider
→ RunwayVideoProvider
→ model = Seedance 2.5
```

Seedance 모델 이름/외부 API 세부값은 Adapter 내부에 둔다.

**근거:** Scene 단위 생성은 기존 Planning/Script Scene 구조와 맞고, 이후 Scene 단위 재생성·실패 격리에 유리하다.

### 2. Scene별 TTS

각 `ScriptScene.narration`을 Scene별로 ElevenLabs에 전달한다.

```text
TTSProvider
→ ElevenLabs
→ scene narration audio + timestamp
```

- `narration == null`이면 TTS 생성 안 함
- 같은 voice / model / 설정을 유지
- 승인된 Script narration이 최종 음성의 Source of Truth

**근거:** 승인된 대본과 실제 음성을 일치시키고 Scene duration 검증 및 부분 재생성을 쉽게 한다.

### 3. Subtitle

- `ScriptScene.subtitle != null`
  - 해당 subtitle만 화면 자막으로 사용
  - ElevenLabs timestamp를 기준으로 타이밍 배치
- `subtitle == null`
  - narration이 있어도 자막 생성하지 않음

별도 STT/자막 AI 모델은 사용하지 않는다.

**근거:** 이미 승인된 Script가 있으므로 다시 STT할 필요가 없고, 화면 자막 여부도 Script 계약을 따른다.

### 4. BGM

9/13 MVP에서는 AI 음악 생성 미사용.

- 고정 BGM FileAsset 사용
- FFmpeg 합성 시 narration보다 낮은 볼륨으로 믹싱

**근거:** 영상 생성 E2E 완성이 우선이며 AI BGM은 별도 고도화 범위로 분리한다.

### 5. FFmpeg Composition

Scene별 결과를 최종 영상으로 합성한다.

```text
scene_1.mp4 + scene_1_tts
scene_2.mp4 + scene_2_tts
...
subtitle
bgm
→ final_video.mp4
```

FFmpeg 책임:
- Scene 연결
- TTS 오디오 합성
- Subtitle 렌더링
- BGM 믹싱
- 최종 9:16 MP4 생성

## Failure Rule

Scene 하나라도 생성 실패 시 해당 `video_generation` 실행은 `FAILED`.

```text
Scene 1 SUCCESS
Scene 2 SUCCESS
Scene 3 FAILED
→ video_generation FAILED
```

- 이미 성공한 Scene 파일은 삭제하지 않는다.
- 성공 Scene 재사용 / 실패 Scene만 재생성하는 최적화는 후속 확장 가능.
- timeout / 5xx / network error는 사용자 Revision이 아니라 기술 실패이며 기존 `NodeExecutionAttempt` 체계를 따른다.

**근거:** 사용자 수정 Version과 기술 Retry를 기존 설계대로 분리한다.

## Architecture Rules

- Workflow 상태 변경은 Workflow Engine만 수행한다.
- `VideoGenerationService`는 생성 과정을 조율하고 결과만 반환한다.
- `VideoProvider`와 `TTSProvider`를 분리한다.
- Vendor SDK/API 세부사항은 Provider Adapter 안에 둔다.
- Seedance 하나에 영상/TTS/자막/BGM 책임을 몰아넣지 않는다.
- Production Executor에서 Provider 미설정 시 fake 성공 금지.

## Non-goals

이번 Task에서 하지 않는다.

- Redis / Celery 실제 비동기 처리
- AI BGM 생성
- 여러 Video 모델 품질 비교 / 자동 선택
- SNS 게시
- Scene 단위 Revision 기능 확장
- 과도한 영상 효과 / 편집 UI
- 새로운 Core Node 추가

## Definition of Done

- [ ] Planning Scene ↔ Script Scene 기준으로 Scene별 영상 생성
- [ ] reference image 있음/없음 두 경로 지원
- [ ] Scene별 ElevenLabs TTS + timestamp 처리
- [ ] subtitle nullable 규칙 적용
- [ ] 고정 BGM 합성
- [ ] FFmpeg로 `1080x1920 final_video.mp4` 생성
- [ ] 최종 Video FileAsset 저장
- [ ] `video_generation`이 `WAITING_APPROVAL`까지 도달
- [ ] Scene 실패 시 Node 실패 + 성공 결과 보존
- [ ] 기존 Revision/Attempt 규칙 회귀 없음
- [ ] 테스트 추가
- [ ] `python -m pytest` PASS
- [ ] `python -m ruff check .` PASS
- [ ] `python -m mypy app` PASS

## Codex Completion Report

완료 후 아래만 보고한다.

1. 변경 파일
2. 구현 내용
3. 실행한 검증 명령
4. PASS / FAIL
5. 남은 문제
6. Architecture 결정 변경 여부
