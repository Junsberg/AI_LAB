# HANDOFF — 새 세션이 이어받기 위한 현재 상태

> 새 채팅에서 "docs/HANDOFF.md 읽고 이어서" 로 시작. 이 문서는 상태가 바뀔 때마다 갱신.

## 문서 지도
| 문서 | 용도 |
|---|---|
| `DESIGN.md` | 현재 설계 (v2) |
| `DECISIONS.md` | 결정 기록 — 왜 그렇게 했고 언제 되돌리는지 |
| `INVARIANTS.md` | 건강검진 규칙 17개: 왜·임계값·자동 조치·**오탐** |
| `RUNBOOK.md` | 장애 대응 절차 |
| `GO_LIVE_CHECKLIST.md` | 실매매 전 관문 |
| `reviews/` | 데일리 리뷰 (확신도 A/B/C 표기) |
| `checks/` | 6h 점검 루틴의 한 줄 기록 (날짜별) — 루틴 세션 대화는 메인 세션에서 못 보므로 파일로 남김 |
| `stats/` | 시간별 집계·건강검진 JSON (자동 커밋) |

## 프로젝트
솔라나 밈코인 시그널 엔진 + 고정 규칙 청산 + Claude Code 자기개선 루프.
소유자: DEX 경험 없음(CEX 선물 6년). **분석·판단은 Claude가, 리스크 감각 검토는 소유자가.**
절대 원칙: 설계·버그·로직 순서로 자산을 잃는 일 불허 → `docs/GO_LIVE_CHECKLIST.md` 전항 통과 전 실매매 금지.

## 인프라 (전부 무료)
| 구성 | 위치 | 비고 |
|---|---|---|
| 코드 | GitHub `Junsberg/AI_LAB` (public), 작업 브랜치는 세션마다 지정(09-27 후반: `claude/handoff-design-limits-et7v32`), 워크플로는 main 필요 → ff-merge 관행 | |
| DB | Supabase 프로젝트 `memebot` (`umjfzpdlzzatrjxumkgv`, 서울) | `saja-live`와 절대 섞지 말 것 |
| 스케줄 | Supabase pg_cron → GitHub workflow_dispatch (`public.gh_dispatch`, Vault `GH_PAT`) | GitHub 자체 cron은 불안정, 백업용 |
| 잡 | collect 10분 / enrich 매시 17분 / outcomes 매시 05·35분(20분 예산) / stats 매시 37분 / **paper 10분(:03, pg_cron `gh_paper`)** / replay(수동·코드 변경 시) / ci(main 외 푸시, 실제 Postgres) | `.github/workflows/` |
| 시크릿 | GitHub Secrets: `HELIUS_API_KEY`, `DATABASE_URL`. Supabase Vault: `GH_PAT` | 채팅에 절대 안 붙임 |
| 리뷰·점검 | 루틴 2개(데일리 08:00 KST, 6h 점검 05/11/17 UTC)가 **전용 세션 "memebot 루틴 전용 (opus)"(Opus 5.5, AI_LAB 작업 브랜치 체크아웃)** 에 바인딩. GitHub API·DB 도구 없음 → 파일 기반: `latest.json`·`health.json`·`actions.json`(stats 잡이 매시간 Actions 24h 집계). 결과는 `docs/reviews/`, `docs/checks/`. 코드 수정 시 브랜치 푸시까지만, main 머지는 CI 확인 후 메인 세션. "매번 새 세션" 방식 루틴은 레포가 안 붙어 실패하므로 쓰지 말 것 | 전용 세션 문맥이 커지면 create_session 으로 새로 만들고 루틴의 persistent_session_id 교체 |
| HL 카피봇 | 별도 레포 `claudecode_factory` — **수정 금지**, 읽기만 | |

## 파이프라인
GeckoTerminal new_pools → 배포자(rugcheck creator 1순위, RPC 폴백은 확신 없으면 skip) → rugcheck 리스크 스냅샷(풀 계정 제외)
→ enrich: 배포자 검증 백필 → 24h/72h 결과 평가(러그 정의 v1: lp_pull, price_collapse) → 자금 추적(3홉, CEX/허브에서 중단) → 클러스터 → 점수(평가된 토큰만)
→ stats: `docs/stats/*.json` 커밋

## 결정 사항
- 프레임워크 안 씀. Python + Postgres + Claude Code 스킬/훅
- `strategy/params.yaml`이 유일한 튜닝 지점, `risk:` 블록은 훅으로 동결
- LLM은 실시간 판단 안 함(연구 근거: 고정 청산 > LLM 재량 청산). 오프라인 가설 1개 → 리플레이 → 페이퍼 7일·n≥30 → commit/revert
- 텔레그램 계정 없음 → 콜은 온체인 볼륨 스파이크 역추정
- 로컬 PC(Windows 10)는 실거래 단계에서만 필요. 그때 새 세션에서 연결

## 진행 상태 (2026-09-27 17:40 UTC)
- 1~2주차 완료. 3일치 데이터: 토큰 ~4,500(24h 유입 ~1,150), 결과 평가 ~3,300, 배포자 추적 73%.
- 09-25~27 수정(전부 main, CI 통과): outcomes 429 처리·20분 예산·pools/multi 배치(회당 9→100건) / 결과 규칙 v2(캔들 글리치 제거: 몸통 기준·거래량 0 제외·1000배 초과 no_data, 전량 재평가 완료) / 건강검진 `peak_multiple_sane` 추가 / `cluster_size_sane` 허브형만 경고 / 독립 리뷰 4건(배치 내 1개 실패 격리, 예산 확인, actions 집계 1000건, SQL 테스트 재실행성).
- **데이터가 말하는 것** (리뷰 09-26·09-27, 확신도 B): 졸업 토큰 고점 배수 중앙값 1.00(무차별 진입=손실), 10x 5%, 러그 ≥17%(하한). 계보로 갈림: 단독 배포자 러그 24% vs 4+ 클러스터 9%. 릴레이 체인 클러스터 `63410261…`(배포자 63, 토큰 129) 러그 0/91·10x 30% — 빌더형. 팜형 `45a29e…` 러그 55%. 핵심 가설 첫 확인.
- **09-27 17:08Z 적용(main)**: ① 유동성 고점 바닥값(`meta.reserve_usd_at_seen`, 신규 수집분부터) ② 결과 규칙 v3 — 첫 24h 5분 캔들(`before_timestamp`=생성+24h, 288개), `RULES_CHANGED_AT`=17:14:40Z(v3 첫 실행) 전량 1회 재평가, 최신 토큰부터. 첫 v3 실행이 2콜/토큰으로 회당 51건(429 병목) → 80h 이하 토큰은 5분봉 1콜+시간봉 합성으로 수정(`aa76eed`). 재평가 ~3,600건은 하루 이상 걸릴 수 있음 — `outcome_backlog`는 신규만 세므로 정상이어야 함. 기준선(16:37Z): 평가 3,563 / lp_pull 8 / price_collapse 662(p50 고점 1.00) / >100x 1.2%.
- **알려진 설계 한계(남은 것)**: ③ unknown 자금원 12% → WSOL 랩·언랩(syncNative/closeAccount)은 `swap`으로 분류 ④ 클러스터 ID — 09-27 이전 ID 상속으로 해결.
- **페이퍼 러너 설계(확정, DECISIONS 09-27)**: 진입 = 클러스터 점수 ≥0.9·평가 ≥10 + 기존 게이트, 계보 단독 진입 모드. 진입가 = `seen_at` 시점 GT 가격, 슬리피지 = CPMM 충격+수수료, 청산 = 5분 캔들 재생(손절은 wick 저가·갭은 시가, 익절은 몸통, 동시 충족 시 손절 우선), 홀더·배포자·KOL 청산은 '측정 불가'로 비활성 명시. `paper.yml` 10분, 리스크 캡은 실행기에서 강제, 대조군 = 같은 시각 무작위 졸업 토큰+같은 청산.
- **09-27 19:00Z 페이퍼 가동**: `execution/paper_runner.py`(결정·관리), `execution/sim.py`(청산 재생, 리플레이와 공용), 마이그레이션 003(Supabase 적용 완료), stats `paper_summary`·`paper_exit_reasons`·`paper_decisions_24h`. 대조군 = `mode='paper_control'`(게이트 통과·미선정 토큰의 결정적 5%, 동시 10개). 보유 한도 24h. 리플레이 리포트 `docs/stats/replay_lineage_v0.json`.
- **⚠️ 리플레이 발견(09-27 19:10Z, `replay_lineage_v0.json`)**: 선정 63건 중 58건이 클러스터 `63410261` 하나. 진입(졸업+15분) 이후 거래가 없는 토큰이 대부분 — no_entry 33, 즉시 dead_volume 28(-5.8% = 비용만), hard_stop 2. 전략 평균 -3.6%±5.9%·승률 3% vs 대조군 +17.8%±20.1%·29%(n=17). 해석: ① 이 클러스터의 러그 0·10x 30%는 **졸업 직후 수분 안의 움직임**(우리 진입 전) + 거래가 끊겨 가격이 안 무너진 것 → 점수 인공물. ② 러그 정의가 '거래 소멸'을 clean으로 셈. 페이퍼 첫 진입 3건 중 2건(`7ed91376`)도 진입 후 캔들 없음. → **09-28 규칙 v4 적용**(DECISIONS): `dead`·`entry_multiple`, 클러스터 점수 clean에서 dead 제외·10x는 진입 후 기준, 재평가는 `rules_version<4` 전량(~1.5일, 최신 토큰부터). 마이그레이션 004 적용 완료. **초기 v4 수치(09-28 00:58Z, 최신 토큰 159건 재평가)**: dead 95(60%) — 대부분 진입 시점에 이미 거래 없음(entry_multiple 없음 82), 러그 아닌 토큰의 entry_multiple p50 1.04·p90 6.13, price_collapse p90 5.46. 즉 졸업 토큰 다수가 1시간 안에 죽고, 진입 후에도 상위 10%는 6배 여지 있음. 클러스터 51곳에 dead 반영. 01:37Z: dead 185, 페이퍼 전략 청산 14(평균 +13.5%±22%, 승률 15%) vs 대조군 8(-7.4%±9.4%) — 표본 작음. **관찰**: `funding_precedes_launch` 0→1(01:10Z부터, v1 트랜잭션 수정 이후) — enrich 자동 재추적 대상. 3건 이상으로 늘면 DECISIONS의 되돌림 조건(버전 0 복귀) 검토. 재평가 후 리플레이 재실행(`replay.yml` 수동) → 선정 건수·전략 vs 대조군 재비교.
- **09-29 전환**: v0 진입 0건 확인(v4로 빌더 클러스터 점수 0.99→0.18), 대조군 +410%는 시뮬레이터 글리치(단일 5분봉 ~70배 가격에 전량 매도). → ① 규칙 v5·시뮬레이터 2캔들 확인·`paper_ret_sane` ② 진입 규칙 `lineage_v1`(DECISIONS 09-29). stats 페이퍼 요약은 전략별로 분리 — **v0 결과(글리치 포함)는 판정에 쓰지 말 것**. 리플레이 리포트 `docs/stats/replay_lineage_v1.json`(09-29 01:45Z): 표본 240 중 183 평가(예산 소진). 판정 first_launch 113(62%)·not_trading 52·enter 14. 전략 n=13 평균 +1.3%±24.6%(중앙 −18%, 승률 31%) vs 대조군 n=96 +5.7%±22.8% — 대조군 최대 +2,130%(30분 거래량 $1.47M, 6h 보유 — 글리치 아닌 실제 러너로 보임) 제외 시 −16.7%. **관찰(가설 후보, 지금 변경 금지)**: 거래량 최상위 러너 2건(30분 $1.5M·$6.3M)이 모두 first_launch로 제외됨 → 첫 발행 제외가 러너를 거르는지 페이퍼 2주 데이터로 검증 후 `propose-change`.
- **09-30**: v1 첫 하루 전략 36건 +35.9%±15.7%(승률 53%). 대조군을 '살아 있는데 계보로 제외된 토큰'으로 재정의(DECISIONS 09-30) — 이전 대조군(v1 15건)은 비교에 쓰지 말 것. lp_pull 급증은 바닥값 재분류로 확인(버그 아님).
- **다음 단계**: v3 재평가 완료(~1.5일) 후 값 범위 재확인 → 페이퍼 v1 2주(10-13) 중간 판정: 전략 vs 대조군 mean_ret±stderr, 진입 건수(첫 주 <5건이면 완화 가설) → 4주·청산 100건 → `GO_LIVE_CHECKLIST.md`. 3주차 원안(트레이드 테이프·KOL 선행 지갑)은 페이퍼 결과 보고 착수.
- 작업 흐름: 브랜치 푸시 → ci 성공 확인 → `git push origin <작업 브랜치>:main` → 경로 변경된 워크플로 smoke 실행 로그 확인 → 다음 stats로 값 검증. "status ok"가 아니라 **값 범위**를 확인할 것(09-25 고점 배수 56억 배가 ok로 통과한 전례).

## 알려진 제약
- 이 클라우드 세션은 외부 API 접근 불가 → 실통신 테스트는 GitHub Actions push 트리거로
- GeckoTerminal 30req/min, Helius 무료 크레딧 → 잡별 한도 코드에 고정
