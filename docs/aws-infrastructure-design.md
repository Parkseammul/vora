# VORA AWS 인프라 설계 및 강사 검토서

- 문서 버전: **1.3 최종안** / 수정일: 2026-09-23
- 개발 기간: **2026-09-21~2026-10-11, 총 3주**
- 코드 참조: v1.1의 feat/aws-deployment, e125735 조사 기록 승계. 이번 수정에서 코드 재검증 없음.
- 상태: **사용자 지정 최소 구조 확정 / 강사 검토 대기 / 구현·AWS·관측성 실행 미검증**
- 표기: 확정=사용자 지시·프로젝트 원칙, 후보/가정=상세 선택·측정 시작점, 미검증=실행 증거 없음.
- 이번 작업은 문서만 수정하며 코드·DB·Dockerfile·AWS 작업·유료 API·SNS 게시·commit/push/PR을 수행하지 않는다.

## 1. 검토 결론과 완료 범위

**최종 선택은 EC2 API 1대(Nginx·React·FastAPI·Redis) + EC2 Worker 1대(Celery·Video/Publication Worker·FFmpeg) + RDS PostgreSQL + S3다.** 애플리케이션 관측성은 Prometheus·Grafana·Loki·Alloy, AWS 인프라 관측성은 CloudWatch로 구분한다.

이는 Architect인 사용자가 확정한 **목표 구조**이며 구축·검증 완료를 뜻하지 않는다. EC2 두 대를 동시에 구축할 의무는 없고, ECS는 최종 운영안에서 제외하여 향후 확장 후보로만 남긴다.

| 범위 | 완료 기준 / 현재 상태 |
|---|---|
| 전체 설계 | 최종 구조·근거·P0·비용·종료 계획 제출 / 강사 검토 대기 |
| 로컬 검증 | fake provider·자원 측정·애플리케이션 관측성 검증 결과 또는 차단 원인 / 이번 수정에서 미실행 |
| 개별 AWS 검증 | 강사 전체 설계 검토/승인(G0) 후 필요한 리소스만 사양·예산·정리 범위 승인(G1)을 받아 시험 / 전 항목 미실행 |
| 전체 배포·Cloud Full E2E | 필수 완료 조건 아님. AWS 관측성 stack 전체 배포·유료 호출·실제 게시도 필수 아님 |

승인 지연 시 **설계·로컬 결과·미결정/미실행 목록으로 2026-10-11 마감**할 수 있다. 로컬 성공이나 미실행 항목을 AWS PASS로 표시하지 않는다. 개인 계정 상태는 기존 사용자 설명 외 미확인이고 교육 계정 자원 재사용·삭제는 범위 밖이다.

## 2. 기존 코드 조사 사실과 검증 한계

아래는 **v1.1 코드 조사 기록**이며, 현재 실행 성공을 뜻하지 않는다.

| 확인 기록 | 근거 | 설계 영향 |
|---|---|---|
| Workflow/Node/Transition은 Python 정의, Engine이 영구 상태·Attempt 관리 | [workflow_definitions.py](../backend/app/workflow_definitions.py), [workflow_engine.py](../backend/app/workflow_engine.py) | DB는 상태 저장소, 정의 테이블 신설 금지 |
| video_generation/publication 큐와 Redis result backend·Pub/Sub 사용 | [celery_app.py](../backend/app/celery_app.py), [video_async.py](../backend/app/video_async.py) | Redis 이벤트·결과를 영구 상태로 간주하지 않음 |
| 입력·BGM·최종본은 로컬 경로, 조회는 FileResponse | [workflow_execution_service.py](../backend/app/workflow_execution_service.py), [video_generation.py](../backend/app/video_generation.py), [main.py](../backend/app/main.py) | 분리 호스트 배포 전 파일 전달 구현 필요 |
| S3 adapter는 있으나 생성 완료 시 자동 영속화는 확인되지 않음 | [storage.py](../backend/app/storage.py), [publication_runtime.py](../backend/app/publication_runtime.py) | S3 단독 시험과 앱 연동 완료를 구별 |
| Runway·ElevenLabs 호출 후 CPU FFmpeg 합성, 전체 read/base64 경로 존재 | [media_providers.py](../backend/app/media_providers.py) | GPU 필요 근거 없음, RAM·CPU·scratch 실측 필요 |
| dev 고정 사용자, 소유권 검사 부재; 사용자 token은 DB Text 경로 | [main.py](../backend/app/main.py), [models.py](../backend/app/models.py) | 공개 배포 전 B01/B04 해소 필요 |
| Dockerfile에 FFmpeg·한글 폰트 설치 없음, health는 정적 응답 | [Dockerfile](../backend/Dockerfile), [main.py](../backend/app/main.py) | 이미지 시험·readiness 개선 필요 |
| DB commit과 enqueue 사이 비원자 구간, 최종 승인과 게시는 별도 | [workflow_engine.py](../backend/app/workflow_engine.py), [publication.py](../backend/app/publication.py) | 복구 시험 필요, 승인만으로 게시 금지 |
| Frontend는 Vite 정적 빌드, 개발 proxy 사용 | [vite.config.ts](../frontend/vite.config.ts) | 배포 후보별 production routing 필요 |

## 3. 부하·보관 가정과 S3 산정

| 항목 | 기존 가정·기록 | v1.3 적용 |
|---|---|---|
| 부하 | 동시 1~3명/월 100개, 확장 시 동시 10명/월 1,000개 | 실측 전 시험 시나리오이며 확정 수요 아님 |
| 입력·영상 | 기본 30초·최대 60초·이미지 최대 6개는 기존 스키마 기록 | 10 MiB/장·합계 60 MiB는 미결정 제안 |
| Worker | concurrency=1부터 검증 | 운영 상한·FFmpeg thread/CPU 제한은 측정 후 결정 |
| 저장·보관 | 평균 최종본 50 MB, 보관 30/90일 제안 | 실제 크기·입력·수정 버전·보관 정책 미결정 |
| 전송 | 월 20/100 GB 기존 예산 가정 | 저장량과 별도로 재생·다운로드·게시 트래픽 산정 |

기존 운영 100 GB는 월 1,000개·90일 보관 가정과 맞지 않는다. **최종본만 1,000 × 0.05 GB × 3개월 = 150 GB**이며 입력·BGM·추가 사용자 버전·비현재 객체·미완료 업로드·게시 media를 더해야 한다. 이는 정상상태 예시이며 초기 월평균 GB-month와 구별한다.

기존 $0.025/GB-month 가정으로 최종본만 $3.75/월이다. 총량·요청·전송·백업·로그 보관은 별도이고, 150 GB나 90일을 최종 운영 사양으로 확정하지 않는다.

## 4. 최종 선택 아키텍처

~~~mermaid
flowchart TB
    U["User"] -->|"HTTPS"| N["Nginx / React 정적 파일"]
    subgraph API["EC2 API 1대"]
        N --> A["FastAPI"]
        R["Redis / Queue · result backend · PubSub"]
        A <--> R
    end
    subgraph Worker["EC2 Worker 1대"]
        C["Celery / Video Worker · Publication Worker / FFmpeg"]
    end
    C <--> R
    A --> DB["RDS PostgreSQL / 영구 상태"]
    C -->|"Engine을 통한 상태 반영"| DB
    A <--> S["Private S3 / 입력 · BGM · 최종 영상 · 게시 media"]
    C <--> S
    C --> G["Runway / ElevenLabs"]
    C --> P["YouTube / Instagram"]
~~~

DB·S3 연결 주체는 FastAPI와 Worker이며 Redis가 영구 상태를 기록하는 구조가 아니다. Worker의 Node Executor/외부 Service는 결과만 반환하고 Workflow 상태 변경은 Engine 경로에서 수행한다. RDS의 WorkflowExecution·NodeExecution·Attempt·Revision·Approval·Publication 등은 영구 상태의 개념 범위이며 새 테이블 생성을 지시하지 않는다.

~~~mermaid
flowchart LR
    P["Prometheus"] -->|"scrape /metrics (pull)"| A["FastAPI / Celery / Video·Publication Worker /metrics expose"]
    G["Grafana / 운영 Dashboard"] -->|"query"| P
    A -->|"Logs"| AL["Grafana Alloy"]
    AL --> L["Loki"]
    L --> G
    AWS["EC2 / RDS / AWS 로그"] --> CW["CloudWatch / 인프라 상태·Alarm"]
~~~

관측성은 우선 로컬 또는 최소 환경에서 검증한다. Prometheus/Grafana/Loki의 AWS 배치 위치·자원·보관 기간은 미결정이며 전용 EC2나 운영급 HA를 기본 자원으로 추가하지 않는다. API/Worker 동거 배치 시에도 자원 경쟁을 먼저 측정한다.

## 5. 최소 네트워크·접근 경계

| 대상 | 설계 방향 | 이유·미결정 |
|---|---|---|
| EC2 API/Worker | VPC public subnet·IGW 경로, 필요한 public IPv4, SG 제한 | NAT 없이 외부 HTTPS에 접근한다. public subnet이 모든 포트 공개를 뜻하지 않는다. |
| API | Nginx에서 HTTPS 종료, FastAPI는 host 내부 접근 | ALB 없이 단일 API를 제공한다. 도메인·인증서 발급/갱신 방식은 미결정이다. |
| Redis | API host의 private 경로, Worker SG만 6379 접근, ACL/TLS 검토 | 인터넷에 Queue를 공개하지 않는다. API host 장애 시 큐도 중단되는 한계를 수용한다. |
| RDS | AWS 요구사항에 따라 DB subnet group은 최소 2개 Availability Zone의 private subnet을 포함, 공개 접근 없음, 5432는 API/Worker SG만 | EC2와 영구 데이터를 분리한다. subnet group의 최소 2 AZ 구성과 별개로 RDS Single-AZ vs Multi-AZ 운영 방식은 미결정이다. |
| S3 | private bucket·최소 IAM 권한·인가 후 한시적 URL | host 간 파일 전달용이다. Gateway Endpoint는 필요성 확인 후 선택하며 필수 신규 서비스로 잡지 않는다. |
| 운영·관측 접근 | SSH/metric/Loki/Grafana 포트 인터넷 공개 금지, 관리 접근 제한 | 구체 관리 경로·SG·계정 정책은 실행 카드에서 확정한다. |

CIDR·AZ·quota·리전은 미확인이고 서울은 기존 제안이다. 로그인 구현 전 API 검증은 허용된 관리자 IP로 제한하며 IP 제한이 앱 인증을 대신하지 않는다.

## 6. 데이터 흐름과 P0 6개

**확정 원칙:** Workflow/Node/Transition은 Python 정의, Workflow 상태 변경은 Engine만 수행, PostgreSQL은 영구 상태의 Source of Truth다. 사용자 수정은 새 NodeExecution과 user_requested_version 증가, 기술 Retry는 같은 NodeExecution에 새 Attempt이며 영향받지 않은 SUCCESS는 재사용한다.

흐름: 요청 → Engine/RDS → 승인 → Redis/Celery → 영상 결과·S3 → Engine 상태 반영 → 최종 승인 → **별도 Publish Action** → 게시 결과. Workflow 성공과 게시 성공은 독립이며 최종 승인으로 자동 게시하지 않는다.

| P0 | 확정·유지할 원칙/기존 사실 | 미결정 사항 | 후속 구현·검증 |
|---|---|---|---|
| B01 인증·ownership | dev 고정 사용자·소유권 부재 조사 기록, 사용자별 접근 통제 필수 | Cognito 없이 사용할 인증 방식, User 매핑/schema, session/JWT·SSE 인증 | 익명/타 사용자 조회·수정·승인·게시·SSE 차단 |
| B02 S3·파일 참조 | S3 영속화 선택 확정, 기존 로컬 의존 미해소. 불변 key·참조 정책과 업로드 확인 후 DB SUCCESS 원칙 유지 | key 규칙·DB 참조·업로드/검증/commit 절차, orphan/missing object 복구·보관 만료 정책 | API↔Worker 전달, 버전 재사용, 업로드 실패 시 SUCCESS 방지, orphan·유실 객체 시험 |
| B03 Worker 이미지 | FFmpeg·한글 폰트 누락 조사 기록 | 패키지/폰트·버전·라이선스, FFmpeg CPU/RAM/thread 제한 | fake 합성·한글 자막·non-root·secret 제외·scratch 정리 |
| B04 token·secret | SNS token DB Text 경로 기록, 보호·노출 방지 필요 | 암호화·키 관리/회전·secret 주입·TLS 방식 | 암복호화·회전·로그 마스킹·DB/Redis TLS 시험 |
| B05 작업 복구 | Engine/Attempt 규칙 유지, DB commit→enqueue 비원자 구간 기록 | 기존 모델 복구 또는 outbox/lease 등 별도 승인 필요, timeout·중복 판정·외부 결과 확인 정책 | Celery duplicate execution, enqueue 유실, stale RUNNING, worker 종료, 외부 생성/게시 결과 불명확 처리 |
| B06 자원·비용 제한 | 크기/형식·concurrency·비용/보관 정책 필요 | 입력 byte·rate·video concurrency 운영 상한·재시도/유료 상한·보관 기간 | 상한 차단·오류 응답·자원 초과·보관 만료 후 참조 검증 |

외부 생성/게시 결과가 불명확하면 무조건 재호출하지 않는다. 복구 수단·새 schema·인증 방식은 임의 확정하지 않으며 별도 Task Spec에서 결정한다. P0 전체 해소는 공개 운영 전 필요하지만 격리된 단독 리소스 시험에는 해당 시험의 선행 조건만 적용한다.

## 7. 리소스·비용·개별 AWS 검증

### 7.1 사양과 비용 근거

서비스 선택은 확정이지만 **아래 사양·시간·금액은 기존 가정에 기반한 측정 시작점**이다. 최신 서울 확정 견적이 아니며 가격 재조회는 하지 않았다. 기존 EC2+로컬 DB 또는 ECS 총액은 RDS와 새 관측성 구성을 반영하지 못하므로 v1.3 총예산으로 재사용하지 않는다.

| 자원 | CPU/RAM/storage 후보 | 이유·산정 상태 |
|---|---|---|
| EC2 API | t3.medium 1대, 2 vCPU/4 GiB/gp3 30 GiB | 기존 후보를 측정 시작점으로 유지한다. PostgreSQL은 RDS로 빠지므로 과거 DB 동거 RAM 합계는 폐기하고 API·Redis·OS·수집기 사용량으로 재측정한다. |
| EC2 Worker | t3.large 1대, 2 vCPU/8 GiB/gp3 50 GiB | FFmpeg 자식·다운로드 버퍼·scratch 여유를 확인하기 위한 후보이며 concurrency=1로 시작한다. T3 credit·지속 CPU 성능은 미검증이다. |
| Redis | API CPU/RAM 공유, 별도 관리형 node 없음 | Queue/result/PubSub용이며 영구 상태가 아니다. maxmemory·TTL·eviction·재기동 복구 정책은 미결정이다. |
| RDS PostgreSQL | db.t4g.small, 2 vCPU/2 GiB/gp3 20 GiB 후보 | 영구 상태용 작은 측정 후보이며 pool·쿼리·복구 요구로 조정한다. RDS Single-AZ vs Multi-AZ 운영 방식·backup·RPO/RTO는 미결정이다. |
| S3 | CPU/RAM N/A, 용량은 3절 산식 | 입력/BGM/최종본/게시 media를 영속화한다. React 정적 파일은 API EC2에 두며 별도 frontend bucket은 기본 제외다. |
| Prometheus/Grafana/Loki/Alloy | 로컬부터 검증, CPU/RAM/disk·보관량 미측정 | metrics 수·로그량·수집 주기와 retention을 측정해 산정한다. 별도 AWS host 비용을 임의로 넣지 않는다. |
| CloudWatch | 관리형 CPU/RAM N/A, metric/log/alarm 개수·보관량 미정 | AWS 상태와 알람에 한정해 사용량을 견적한다. 앱 로그 전체를 Loki와 중복 저장하는 것을 기본으로 하지 않는다. |

**월 비용 산식:** EC2 시간 + EBS + IPv4 + RDS compute/storage/backup + S3 저장/요청/전송 + CloudWatch + 관측성 실행/보관 자원 + 기타 승인된 비용. 기존 단가로 EC2 두 대를 730h 사용하면 compute만 (0.052+0.104)×730 = **$113.88**, EBS 80×$0.0912 = **$7.30**, IPv4 두 개는 2×730×$0.005 = **$7.30**이다. 이 약 $128.48은 **부분합**이며 RDS·관측성 등을 포함한 총액은 미정이다. 세금·환율·credit·유료 provider도 별도다.

### 7.2 개별 검증 카드 — 전 항목 미실행

시간·용량은 v1.2의 시험 가정을 유지하며 G1에서 확정한다. 필요한 항목만 순차 실행하고 의존 리소스·보관 시간 비용을 함께 계산한다.

| ID / 목적·가설 | CPU/RAM/storage·시간 | 예상 비용 | 합격 기준 제안 | 종료·잔여 과금 |
|---|---|---|---|---|
| T01 API: 단일 host에서 API·Redis 부하를 처리 | 2 vCPU/4 GiB/gp3 30 GiB, 4h | 4×$0.052 + 30×$0.0912×4/730 + 4×$0.005 ≈ **$0.24**; DB·로그·전송 별도 | CPU/RAM·RPS·p95·동시 요청·error 측정, Redis/PostgreSQL 연결·fake 상태 처리 정상, OOM 0 | EC2·EBS·snapshot·IP·로그 잔여 확인 |
| T02 Worker: 제한 자원으로 FFmpeg 합성 가능 | 2 vCPU/8 GiB/gp3 50 GiB, 4h, concurrency=1 | 4×$0.104 + 50×$0.0912×4/730 + 4×$0.005 ≈ **$0.46**; credit·로그·전송 별도 | 한글 자막·5/30/60초 fake 합성 정상, 생성 시간·RSS·scratch·CPU 기록, OOM 0 | 실행 작업 종료·scratch 정리, EC2·volume·snapshot·IP 확인 |
| T03 S3: API/Worker가 로컬 경로 없이 파일 전달 | CPU/RAM N/A, 합성 객체 최대 1 GB·1일, PUT/GET 각 100회 이내 | 저장 1×$0.025/30 ≈ **$0.00083** + 요청·전송·client 비용; 총액 미정 | upload/download checksum 일치, private 접근 통제, API 업로드→Worker 다운로드 확인 | 객체/비현재 버전·delete marker·multipart·로그 잔여 확인 |
| T05 RDS: 영구 상태 연결·복구 검증(필요 시만) | Single-AZ 시험 후보 2 vCPU/2 GiB/gp3 20 GiB, 4 instance-hours 가정 | **4×시간단가 + 20×저장단가×4/730 + backup/client**; 단가·총액 미정, 견적 전 실행 불가 | TLS·트랜잭션·합성 데이터 restore 일치, 복구 시간 기록 | 원본/restore DB·snapshot·retained backup·로그 확인 |

T01의 PostgreSQL 연결 대상은 승인된 RDS 또는 별도로 명시한 로컬 시험 DB다. 로컬 연결을 RDS 성공으로 기록하지 않는다. T03은 두 EC2를 동시에 만들지 않고 순차 client로 전달을 확인할 수 있으며, T05 restore는 실제 instance-hours와 중복 저장 시간을 반영한다. 이전 T04/T06/T07의 ECR·ElastiCache·Fargate 단독 시험은 기본 계획에서 제외한다.

공통 의존 자원(VPC/subnet/SG/IGW/IAM·필요 IP)은 CPU/RAM/storage N/A, 해당 시험 시간만 사용한다. 목적은 허용 경로 성공·비허용 경로 차단이며 구성 자체 $0이라는 기존 가정 외 IP·전송 비용은 합산한다. CloudWatch 검증을 추가하면 metric/log/alarm 사용량·합격 기준·종료 대상을 카드에 먼저 기입하며, 로그·IP·EBS 등 남는 비용은 18절에서 확인한다.

## 8. 로컬 테스트·자원 산정

| 시험 | 확인할 내용·합격 기준 | 한계 |
|---|---|---|
| 기존 테스트 | pytest·ruff·mypy 명령·환경·결과 기록 | 도구/전용 DB 부재는 BLOCKED 또는 실패 원인 기록 |
| API/Redis | RPS·p95 latency·4xx/5xx·동시 요청·CPU/Memory·queue length·연결 | 임계치/SLO는 미결정, 측정 전 scale-out 판정 금지 |
| 영상 | 5/30/60초·이미지 1/6개·한글 자막, concurrency=1, FFmpeg CPU/RAM·생성 시간·scratch | fake provider로 검증하며 실제 유료 생성 성능과 구별 |
| 자원 | OOM 0, 기존 후보 API RSS p95 <limit 70%·Worker peak <75%·disk <70% | 확정 운영 임계치가 아닌 검토용 목표 |
| 관측성 | Prometheus 수집, Grafana metric/log 화면, Alloy→Loki 전달, API/Worker 구분·workflow_id/node/attempt 검색 | 로컬부터 검증, AWS 전체 stack·HA 구축 불필요 |
| 도메인/P0 | 버전/Retry 분리·SUCCESS 재사용·승인만으로 미게시, 중복/유실/불명확 복구 | 미구현 시험은 별도 Spec 필요, PASS로 간주하지 않음 |

CPU는 요청당 CPU초·RPS와 합성 wall time을, RAM은 Python·FFmpeg·다운로드 동시 peak를 측정한다. scratch에는 입력·중간본·최종본·재시도 잔여물을 포함하고 Alloy 등 수집기 자원도 별도 기록한다.

결과 양식: ID / 날짜 / commit·이미지 / CPU·RAM·disk / 입력·명령 / 지연·peak·실패 / PASS·FAIL·BLOCKED·미실행 / 증거 / 후속 조치. 이번 작업에서 앱·관측성 테스트는 실행하지 않았다.

## 9. 측정 후 확장

**API EC2 1대 → 병목 확인 → API EC2 다중화 → ALB + Auto Scaling 도입** 순서로 확장한다. API RPS·CPU/Memory·p95 latency·error rate·동시 요청을 Prometheus/Grafana에서 비교하고 AWS host 지표는 CloudWatch로 교차 확인한다.

DB·Redis·외부 provider·FFmpeg 병목을 먼저 구별한다. 다중화 전 인증/session·S3 파일 참조·Redis endpoint 및 장애 경계를 검토하며 Redis까지 인스턴스마다 복제하는 구조로 임의 확장하지 않는다. 측정 없이 ALB/Auto Scaling이나 ECS를 추가하지 않는다.

## 10. 인증·비밀·접근 제어

| 항목 | 유지할 원칙 | 미결정 |
|---|---|---|
| 사용자 | 서비스 로그인과 SNS OAuth 분리, 모든 API/SSE ownership 검사 | Cognito 제외 상태에서 인증 구현·User 매핑 |
| AWS 권한 | EC2 role·필요 S3 prefix 등 최소 권한, 정적 키 노출 금지 | 배포/관리 접근·정책 상세 |
| token/secret | SNS token 보호·회전·로그 마스킹 | 암호화·키 관리·secret 주입 수단; Secrets Manager 자동 채택 아님 |
| 관측 데이터 | token·개인정보·presigned URL 원문 노출 방지, 관리 화면 접근 제한 | 보관 기간·접근자·로그 필드 |
| 시험 | fake provider·합성 데이터, 유료 키·실제 SNS 자격증명 미주입 | 공개 배포는 P0 해소 후 별도 검토 |

## 11. 관측·헬스·복구

### 11.1 역할과 선택 근거

| 도구 | 역할 | 선택 근거·검증 |
|---|---|---|
| Prometheus | VORA 애플리케이션 metric 수집 | 처리량·지연·실패를 측정해 병목과 확장 필요성을 판단한다. 계측/수집 경로는 후속 구현·로컬 검증 대상이다. |
| Grafana | Prometheus metric과 Loki log 시각화, VORA 운영 Dashboard | API·Worker 성능과 관련 로그를 한 화면에서 확인한다. Dashboard 구현은 미검증이다. |
| Loki | FastAPI/Celery/Video Worker/Publication Worker 로그 중앙 저장 | 분리 host의 장애를 함께 검색하기 위해 선택한다. 저장 위치·retention은 미결정이다. |
| Grafana Alloy | API/Worker 로그 수집→Loki 전달, 필요 시 metric 수집 보조 | host별 로그 전달을 통일한다. Prometheus와 중복 scrape하지 않도록 수집 소유권을 정한다. |
| CloudWatch | EC2 CPU/Network/Disk, RDS CPU/Connections, AWS 로그·Alarm | AWS 인프라 상태를 앱 관측성과 구분한다. EC2 Memory·파일시스템 사용량은 기본 metric으로 가정하지 않고 agent 등 추가 수집을 검토한다. |

CloudWatch 기본 지표와 agent 수집 지표 구분은 [공식 agent metric 문서](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/metrics-collected-by-CloudWatch-agent.html)를 따른다. 추가 agent·custom metric 비용은 미견적이다.

### 11.2 Metric·로그 설계와 최소 검증

| 구분 | 수집/검증 대상 |
|---|---|
| HTTP | request count·RPS·latency histogram/p95·HTTP 4xx/5xx·동시 요청 |
| Workflow/Node | Workflow 성공/실패, NodeExecution 처리 시간 |
| Celery/영상 | task 성공/실패/Retry·queue length·영상 생성 시간 |
| Publication | 성공/실패를 Workflow 성공과 별도 집계 |
| 자원 | 프로세스 CPU/Memory·FFmpeg peak·scratch 사용량, host 지표와 비교 |
| 로그 | service=api/video-worker/publication-worker 등으로 구분, workflow_id/node/attempt 필드로 검색 |

업무 성공은 Engine/영구 상태 기준과 일치시켜 집계하고 Celery 전달 횟수·기술 Retry와 구별한다. 계측 hook·수집 주기·queue 길이 수집 방식·알람 임계치는 미결정이다.

workflow_id/attempt 같은 고유 ID를 Prometheus label이나 Loki의 고카디널리티 index label로 무제한 추가하지 않는다. 검색은 구조화 로그 필드 중심으로 설계한다([Loki label 지침](https://grafana.com/docs/loki/latest/get-started/labels/bp-labels/)). Alloy→Loki 전달 방식은 [Loki 개요](https://grafana.com/docs/loki/latest/get-started/overview/)를 참고한다.

- [ ] Prometheus에서 로컬 metric이 수집되고 Grafana에서 조회됨
- [ ] Alloy가 API/Worker 로그를 Loki로 전달하고 Grafana에서 구분됨
- [ ] 같은 workflow_id/node/attempt의 장애 로그를 검색할 수 있음
- [ ] token 등 민감정보가 로그에 없고 보관/자원 사용량이 기록됨

### 11.3 헬스·복구 한계

정적 /health만으로 DB readiness·Worker 소비·SSE 정상 여부를 판정하지 않는다. API/Redis host는 단일 장애점이고 RDS 분리가 Queue 유실·중복을 해결하지 않으므로 B05 복구 시험이 필요하다. RPO/RTO·backup/restore·외부 결과 불명확 처리 정책은 미결정이며 관측성은 영구 상태를 대신하지 않는다.

## 12. 최종 선택 근거와 향후 후보

| 최종 선택 | 근거 |
|---|---|
| EC2 API / Worker 분리 | FFmpeg CPU·RAM 부하가 API latency에 직접 영향을 주지 않도록 분리한다. |
| RDS PostgreSQL | Workflow·Approval·Attempt 등 영구 상태를 Queue/EC2 장애와 분리하여 보존한다. |
| API EC2의 Redis | Celery Queue/result backend/PubSub에 사용하며 영구 상태는 저장하지 않는다. 현재 규모에서는 관리형 Redis를 추가하지 않는다. |
| S3 | 입력 이미지·BGM·최종 영상·게시 media를 영속화해 host 간 로컬 파일 의존성을 제거한다. |
| Prometheus/Grafana, Loki/Alloy, CloudWatch | 각각 앱 성능·트래픽, 앱 로그, AWS 상태를 담당한다. 상세 근거와 검증은 11절에 한정한다. |

| 현재 제외 / 향후 확장 후보 | 재검토 조건 |
|---|---|
| ALB / Auto Scaling | 단일 API 병목 확인·다중화 결정 후 |
| ECS / Fargate, EKS | 배포·호스트 운영 또는 orchestration 요구가 커질 때 |
| ElastiCache | Redis 가용성·용량·운영 부담이 API 동거 한계를 넘을 때 |
| CloudFront | 정적 전송량·지연·캐시 필요성이 확인될 때 |
| Cognito | 사용자 인증 관리 요구로 도입 이점이 확인될 때; 현재 인증 자체를 생략하는 뜻 아님 |
| NAT Gateway | private compute egress가 필요할 때 |
| GPU | 외부 생성 대신 자체 GPU 연산 필요성이 확인될 때 |
| Zipkin, Tempo, 별도 OpenTelemetry tracing stack | metric/log만으로 분산 경로 진단이 부족할 때 |

위 서비스는 영구 제외가 아니며 이번 기본 구성·개별 시험에 추가하지 않는다. ECR 등 배포 보조 서비스도 자동 생성하지 않고 이미지 전달 필요에 따라 별도 판단한다.

## 13. ADR — 사용자 확정과 미결정 구분

| ADR | 최종 방향 / 상태 | 남은 판단 |
|---|---|---|
| 001 계정/리전 | 개인 계정·서울은 기존 제안, 교육 계정 재사용 제외 | 실제 계정·리전·quota·CIDR 미확인 |
| 002 범위 | **확정:** 전체 설계·로컬 우선, 승인된 개별 AWS 시험 | 시험 대상·일시·예산 |
| 003 계산 분리 | **확정:** EC2 API 1대·Worker 1대 분리 | 사양·FFmpeg 제한·운영 concurrency |
| 004 데이터/플랫폼 | **확정:** RDS PostgreSQL·API host Redis, ECS/ElastiCache 제외 | RDS Single-AZ vs Multi-AZ 운영 방식·backup·Redis 메모리/복구 |
| 005 파일 | **확정:** S3 영속화·불변 참조 원칙 | B02 구현 절차·key·orphan/missing 복구 |
| 006 네트워크/확장 | **확정:** DB subnet group은 최소 2 AZ의 subnet 포함; ALB/Auto Scaling/NAT 기본 제외, 측정 후 확장 | SG·접근·DNS/TLS 상세 |
| 007 도메인 | **확정:** Engine 상태 변경·DB Source of Truth·버전/Retry 분리 | B05 복구 수단/schema |
| 008 인증 | **확정:** ownership 필요, Cognito 기본 제외 | B01 인증·SSE 구현 방식 |
| 009 게시 | **확정:** 최종 승인과 Publish Action 분리, 성공 독립 | 불명확 결과 확인/재시도 정책 |
| 010 가용성 | 단일 API/Redis·Worker 한계 명시 | RPO/RTO·RDS Single-AZ vs Multi-AZ 운영 방식 |
| 011 관측 | **확정:** Prometheus/Grafana·Loki/Alloy·CloudWatch 역할 분리 | 배치 위치·사양·보관·계측·알람 |
| 012 비밀 | **확정:** token 보호·노출 방지 | 암호화/키·secret 주입 수단 |
| 013 배포 보조 | 이미지 전달 수단 미결정, ECR 필수 아님 | 배포 필요 발생 시 선택 |
| 014 종료 | **확정:** 시험 직후 정리·10/11 결과 및 후속 점검 인계 | 보존/삭제 범위·담당·비용 |

확정 근거는 이번 사용자 지시와 프로젝트 원칙이다. 강사 검토·실행 승인은 별도이며 미결정 구현을 확정된 것으로 표시하지 않는다.

## 14. 강사 검토 질문과 실행 카드

1. EC2 API + EC2 Worker + RDS + Redis + S3 구조가 VORA 규모에 적절한가?
2. Redis를 API EC2에 함께 두고 PostgreSQL만 RDS로 분리하는 선택이 적절한가?
3. Prometheus + Grafana + Loki + Alloy를 애플리케이션 관측성 구성으로 두는 것이 적절한가?
4. ALB/Auto Scaling 없이 측정 후 확장하는 전략이 적절한가?
5. AWS에서 실제 개별 검증할 최소 리소스는 무엇인가?

G0 이후 각 G1 카드에 다음을 기록한다.

- [ ] 목적·가설·선행 P0·담당·의존 자원
- [ ] CPU/RAM/storage·개수·네트워크·시험 시간
- [ ] 최신 견적·총액/상한·합격 기준·증거 위치
- [ ] 종료·보존/삭제 승인·잔여 비용 확인 담당/기한
- [ ] 강사 피드백·Architect 실행 승인·일시

현재 실행 카드는 미승인·미실행이다.

## 15. 3주 WBS와 마감 기준

| 기간 / ID | 작업 | 완료 증거 |
|---|---|---|
| 1주 09/21~09/27 / W01 | v1.3 최소 구조·P0·관측 역할·비용/미결정 제출, 강사 G0 검토 | 설계·5개 질문·피드백/대기 상태 |
| 1주 / W02 | 로컬 테스트·fake 합성·계측 및 로그 경로 준비 범위 확인 | 환경/기존 구현 확인, 필요한 후속 Task Spec |
| 2주 09/28~10/04 / W03 | 승인된 Spec 범위의 로컬 부하·도메인·Prometheus/Grafana/Loki/Alloy 검증 | metric·Dashboard·로그 검색·자원 측정 또는 차단 원인 |
| 2주 / W04 | G0 후 필요한 AWS 시험만 선정·견적·G1 | 카드별 승인/대기/제외 기록 |
| 2주 / W05 | 승인된 API/Worker/S3 및 필요한 RDS 시험·즉시 정리 | 개별 결과·비용·정리 증거, 동시 전체 구축 불필요 |
| 3주 10/05~10/09 / W06 | 로컬/개별 결과와 사양 가정 비교·확장 판단·잔여 점검 | 병목 근거·미검증·후속 과제 |
| 3주 10/10~10/11 / W07 | 설계·검증·종료/인계 제출 | 아래 체크리스트 |
| 10/11 이후 / W08 | 실제 시험이 있을 때 비용 반영·보존 만료 확인 | 담당·24~48h 후 및 다음 청구서 점검 기록 |

승인 지연 시 W01→W02→W03→W06→W07로 마감한다. W05는 미실행으로 남기고 전체 배포·Cloud Full E2E·AWS 관측 stack·유료 호출·실제 게시를 선행 조건으로 두지 않는다. 위 구현·시험은 후속 계획이며 이번 작업에서는 문서만 수정한다.

- [ ] 최종 구조·근거·P0 미결정·비용 불확실성 제출
- [ ] 로컬 앱/관측성 결과 또는 실패·차단 원인 제출
- [ ] AWS 시험별 실행 여부·결과·증거 구분, 미실행은 PASS 아님
- [ ] 실행한 자원의 정리·잔여 비용·담당·후속 날짜 인계

## 16. 근거와 확인 수준

코드 근거는 2절의 v1.1 조사 기록을 유지한다. 이번 수정에서 코드를 재조사하거나 AWS 계정·리소스를 조회하지 않았으며 최신 서울 가격도 확인하지 않았다. 관측성의 수집·로그 지침은 공개 공식 문서로 확인했으며 실행 검증과 구별한다.

| 분야 | 공식 근거 | 용도 |
|---|---|---|
| EC2/EBS | [T3](https://aws.amazon.com/ec2/instance-types/t3/), [EC2 가격](https://aws.amazon.com/ec2/pricing/on-demand/), [EBS 가격](https://aws.amazon.com/ebs/pricing/) | 기존 사양/단가 참고, 실행 전 재견적 |
| RDS/S3 | [RDS 가격](https://aws.amazon.com/rds/postgresql/pricing/), [S3 가격](https://aws.amazon.com/s3/pricing/) | 영구 저장·backup·요청/전송 비용 |
| 네트워크/관측 | [VPC 가격](https://aws.amazon.com/vpc/pricing/), [CloudWatch 가격](https://aws.amazon.com/cloudwatch/pricing/) | IPv4·전송·metric/log 비용 |
| Loki/Alloy | [Loki 개요](https://grafana.com/docs/loki/latest/get-started/overview/), [label 지침](https://grafana.com/docs/loki/latest/get-started/labels/bp-labels/) | 로그 전달·검색과 label 설계 |
| CloudWatch agent | [수집 metric](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/metrics-collected-by-CloudWatch-agent.html) | Memory·파일시스템 등 추가 수집 |

## 17. 문서 검증 기록

| 구분 | 기록 |
|---|---|
| v1.0 이력 | pytest·ruff·mypy 모듈 미설치로 실행 전 실패한 기존 기록 유지 |
| v1.3 | 문서 구조·로컬 링크·최종안 일관성·diff whitespace 확인 대상 |
| 앱/DB/관측 테스트 | 이번 수정에서 미실행, 코드/DB 변경 및 alembic 실행 없음 |
| Mermaid | 소스 작성, 시각 렌더링 미검증 |
| AWS·유료 API·SNS 게시 | 전부 미실행, 성공 주장 없음 |

문서 검사 결과는 완료 보고에 기록한다. 문서 PASS는 앱·관측성·AWS 검증 PASS를 뜻하지 않는다.

## 18. 개별 시험 종료·잔여 비용

**각 시험 직후 정리하고 10/11에는 수행 결과와 미완료 점검을 인계한다.** 실제 리소스 ID·보존/삭제 승인 범위만 대상으로 하며 전체 배포가 존재한다고 가정하지 않는다.

| 시점 | 확인할 항목 |
|---|---|
| 생성 전 | 시험 ID·Owner·리전·만료일·비용·보존/삭제 범위, 생성 후 resource ID 기록 |
| 시험 직후 | 신규 작업/스케줄 중단·진행 작업 확인, 결과/로그 확보 |
| 정리 시 | 필요한 데이터 보존 후 승인된 EC2·RDS·S3와 의존 자원 정리, shared/교육 자원 제외 |
| 당일 | EBS·snapshot·IP·restore DB·backup·S3 버전/multipart·로그·metric/alarm·관측성 저장 volume 잔여 |
| 24~48h 후·다음 청구서 | 비용 반영 지연 확인, 보존 항목의 기한·예상액·삭제 담당 기록 |
| AWS 미실행 | 이번 작업의 생성/정리 없음과 계정 상태 미확인을 명시; 계정 전체 무과금 주장 금지 |

EC2 stop만으로 EBS·보유 IP 비용이 없어지지 않으며 RDS도 중지를 영구 종료로 간주하지 않는다. 로컬 관측성 로그·metric 저장량도 보존 정책에 따라 정리하고 AWS에 배치했다면 해당 compute/storage 비용을 함께 확인한다.

보존 비용은 **실제 용량×단가×기간 + 요청/복원/전송**으로 계산한다. 종료 기준은 승인된 보존 목록 외 시험 유료 자원의 정리와 잔여 점검 인계이며, 10/11 이후 청구 확인은 완료 대신 대기로 기록한다.
