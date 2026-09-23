# memebot — 설계 문서 v1

## 목표
월 적립식 소액(포지션당 수십~수백만 원)으로 솔라나 밈코인 시장에서 **속도가 아닌 정보·시점**의 우위로 수익을 내고, Claude Code가 로그를 읽어 **오프라인 가설 검증**으로 전략을 계속 개선한다.

## 원칙
1. **비용 0으로 시작** — Helius Free, Supabase Free, t.me/s 스크랩, 로컬 Windows PC. 유료 스트림(LaserStream)은 시그널이 돈을 번 뒤에.
2. **오픈소스 우선, 회사 유지보수 레포만 실행 경로에** — Chainstack, Helius, Jito, Jupiter, GMGN. 그 외는 읽기 전용 참고.
3. **LLM은 실시간 판단 안 함** — 진입은 `strategy/scorer.py`, 청산은 `execution/exits.py`의 고정 규칙. LLM은 `params.yaml`을 가설 단위로 바꾸고 리플레이+페이퍼로 검증.
4. **`risk:` 블록은 동결** — PreToolUse 훅이 자동 편집 차단. 소유자만 손으로 수정.

## 시그널
| 시그널 | 아이디어 | 모듈 |
|---|---|---|
| Deployer Lineage | 배포자 자금 트리 → 결정적 클러스터 ID → 과거 러그/10x 이력으로 0..1 점수 | `lineage/` |
| KOL Reverse-Map | 콜 시점 × 온체인 매수 상관 → "콜 전에 항상 사는 지갑" 발굴, 그 지갑 매수 = 시그널 | `signals/kol_reverse.py` |
| Survivor | 첫 펌프 후 -70%↓, 데브 물량 소진, 홀더 유지 → 2차 파동 | `signals/survivor.py` |
| Narrative (후순위) | 카테고리 자금 유입 초기에 바스켓 | 미구현 |

콜 소스: 공개 채널 `t.me/s/<channel>` 스크랩(계정 불필요) + 온체인 볼륨 스파이크 역추정.

## 청산 규칙 (우선순위 순)
hard_stop → deployer/KOL 매도 감지 → 홀더 급감 → 볼륨 사망 → 2x에서 원금 회수 → 트레일링 스탑

## 자기 개선 루프
```
/review-performance  → docs/reviews/날짜.md + HYPOTHESIS 1줄
/propose-change      → hyp/* 브랜치, params.yaml 키 1개 ±30% 이내, hypotheses 행
/evaluate-change     → 리플레이(결정적) → 페이퍼 7일·n≥30 → 1 SE 초과 개선 시 merge, 아니면 revert
```
가드레일: 7일당 1건만 채택, 2회 revert된 변경은 60일 금지, n<30 채택 금지.

## 데이터 (Supabase)
`tokens · wallets · wallet_edges · token_outcomes · cluster_scores · calls · trades · signals · positions · fills · hypotheses` — `db/migrations/001_init.sql`

## 보안
- 드레이너 레포 패턴: lockfile에 npm 외 `resolved` URL, 2013~2016년 생성 계정, 이슈 비활성, 키워드 스팸 설명, `t.me` 링크, `postinstall` 스크립트, 커밋된 빌드 산출물
- 실거래 지갑은 소액 핫월렛, 키는 `.env`만, 다른 키가 있는 머신에서 서드파티 봇 실행 금지

## 로드맵
| 주 | 범위 |
|---|---|
| 1 | 스캐폴딩(완료), pump.fun 리스너, 스키마, 계보·시그널·청산 순수 로직 + 테스트 |
| 2 | Lineage 트레이서 실데이터 적재, token_outcomes 평가기, cluster_scores 갱신 잡 |
| 3 | 콜 스크래퍼 스케줄, 트레이드 테이프 수집, precursor 지갑 테이블 |
| 4~6 | 페이퍼 러너 + 리뷰 스킬 실사용, 시그널 생존 판정 |
| 7 | 소액 실거래 |
