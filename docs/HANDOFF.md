# HANDOFF — 새 세션이 이어받기 위한 현재 상태

> 새 채팅에서 "docs/HANDOFF.md 읽고 이어서" 로 시작. 이 문서는 상태가 바뀔 때마다 갱신.

## 문서 지도
| 문서 | 용도 |
|---|---|
| `DESIGN.md` | 현재 설계 (v2) |
| `DECISIONS.md` | 결정 기록 — 왜 그렇게 했고 언제 되돌리는지 |
| `INVARIANTS.md` | 건강검진 규칙 16개: 왜·임계값·자동 조치·**오탐** |
| `RUNBOOK.md` | 장애 대응 절차 |
| `GO_LIVE_CHECKLIST.md` | 실매매 전 관문 |
| `reviews/` | 데일리 리뷰 (확신도 A/B/C 표기) |
| `stats/` | 시간별 집계·건강검진 JSON (자동 커밋) |

## 프로젝트
솔라나 밈코인 시그널 엔진 + 고정 규칙 청산 + Claude Code 자기개선 루프.
소유자: DEX 경험 없음(CEX 선물 6년). **분석·판단은 Claude가, 리스크 감각 검토는 소유자가.**
절대 원칙: 설계·버그·로직 순서로 자산을 잃는 일 불허 → `docs/GO_LIVE_CHECKLIST.md` 전항 통과 전 실매매 금지.

## 인프라 (전부 무료)
| 구성 | 위치 | 비고 |
|---|---|---|
| 코드 | GitHub `Junsberg/AI_LAB` (public), 작업 브랜치 `claude/typesafe-jev-pricing-4tueat`, 워크플로는 main 필요 → ff-merge 관행 | |
| DB | Supabase 프로젝트 `memebot` (`umjfzpdlzzatrjxumkgv`, 서울) | `saja-live`와 절대 섞지 말 것 |
| 스케줄 | Supabase pg_cron → GitHub workflow_dispatch (`public.gh_dispatch`, Vault `GH_PAT`) | GitHub 자체 cron은 불안정, 백업용 |
| 잡 | collect 10분 / enrich 매시 17분 / stats 매시 37분 | `.github/workflows/` |
| 시크릿 | GitHub Secrets: `HELIUS_API_KEY`, `DATABASE_URL`. Supabase Vault: `GH_PAT` | 채팅에 절대 안 붙임 |
| 리뷰·점검 | 루틴 2개(데일리 08:00 KST, 6h 점검 05/11/17 UTC) — **매번 새 세션, Opus 5.5**, GitHub API·DB 도구 없음. 모든 수치는 파일: `latest.json`·`health.json`·`actions.json`(stats 잡이 매시간 Actions 24h 집계 기록). 코드 수정 시 브랜치 푸시까지만, main 머지는 CI 확인 후 사람/메인 세션 | 2026-09-26: Fable 세션 바인딩 루틴은 토큰 비용으로 비활성 |
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

## 진행 상태 (2026-09-24 저녁)
- 1~2주차 완료: 수집·계보·결과 평가·클러스터 점수·통계·리뷰 루틴·건강검진 16개·6h 점검 루틴
- 코드 리뷰 12건 + 데이터 검증으로 발견한 6건 수정 완료 (배포자 30% 오류, 풀 계정 오염, 본딩커브 유입, 자금 시각 역전, 구조 계정 배포자, 거래소 접착)
- 데이터: 토큰 ~1,100(졸업 풀 기준 시간당 30~60), 결과 평가는 09-24 밤부터
- **다음**: 3주차 — 트레이드 테이프 수집(추적 토큰의 매수·매도 지갑), 볼륨 스파이크=콜 시점 추정, KOL 선행 지갑 테이블. 4주차 — 페이퍼 러너.

## 알려진 제약
- 이 클라우드 세션은 외부 API 접근 불가 → 실통신 테스트는 GitHub Actions push 트리거로
- GeckoTerminal 30req/min, Helius 무료 크레딧 → 잡별 한도 코드에 고정
