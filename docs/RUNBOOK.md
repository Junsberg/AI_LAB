# RUNBOOK — 장애 대응

## 0. 어디를 먼저 보나
1. `docs/stats/health.json` → status, 실패 검사 이름
2. GitHub Actions 최근 실행 (`collect`/`enrich`/`outcomes`/`stats`) 실패 여부 → 로그
3. `docs/stats/latest.json` counts 추세 (어제 파일과 비교)

## 1. 수집 정지 (collect_alive)
| 확인 | 명령/위치 | 조치 |
|---|---|---|
| pg_cron이 요청을 보내는가 | Supabase SQL: `select * from cron.job_run_details order by start_time desc limit 10` | 실패면 `public.gh_dispatch` 재생성 마이그레이션 |
| GitHub이 요청을 받는가 | Actions 탭에 `workflow_dispatch` 실행이 10분마다 있는가 | 없으면 Vault `GH_PAT` 만료 → 재발급 |
| 실행은 되는데 0건인가 | collect 로그 `poll.done seen=… new=…` | seen=0 → GeckoTerminal 장애/변경, new=0 → 정상(신규 없음) |

## 2. 배포 직후 크래시
- push 트리거 스모크 실행이 실패하면 **그 커밋이 원인**. 로그의 Traceback 마지막 줄부터.
- 흔한 원인: SQL 파라미터 타입 추론 실패(`%s::text` 캐스팅), 새 컬럼 마이그레이션 미적용(Supabase MCP `apply_migration` 먼저), 외부 API 응답 형태 변경.
- 롤백: `git revert <sha>` → main ff. 데이터는 건드리지 않는다.

## 3. 무료 한도
| 서비스 | 한도 | 증상 | 조치 |
|---|---|---|---|
| GeckoTerminal | ~30 req/min | 429, outcomes 적체 | `GT_SLEEP` 상향, 배치 축소 |
| Helius Free | 월 크레딧 | RpcError -32429 / 401 | 트레이서 한도(40/h) 하향, 대시보드에서 잔량 확인 |
| rugcheck | 비공개 | 429 | 배치 60→30 |
| Supabase Free | 500MB, 커넥션 | 삽입 실패 | 오래된 raw 데이터 정리(`trades` 우선) |
| GitHub Actions | 공개 레포 무제한 | — | — |

## 4. 데이터 오염 발견 시
1. **범위 특정**: 어느 컬럼, 어느 기간, 어느 코드 버전(커밋)부터
2. **재계산 가능한가**: 파생 값(cluster_id, cluster_scores, 리스크 스냅샷)은 재계산 → 코드 수정 후 해당 행의 meta 플래그 초기화
3. **원천 값이면**(deployer, funded_by): 검증 소스로 교정(rugcheck creator, 재추적) — 삭제하지 말고 `meta.*_old`에 이전 값 보존
4. 교정 후 **불변식 재검사** 값이 0으로 돌아오는지 확인
5. `docs/DECISIONS.md`에 기록

## 5. 세션이 죽었을 때 (클라우드)
- 새 채팅에서 `docs/HANDOFF.md` 읽고 이어서. 루틴(08:00 리뷰, 6h 점검)은 세션에 묶여 있으므로 새 세션에서 재생성.

## 6. 실거래 단계 (아직 미적용)
- 킬스위치: `KILL` 파일 생성 → 신규 진입 중단 + 전량 청산 (구현 예정)
- 일일 손실 캡 도달 → 당일 자동 정지, 재시작해도 유지
