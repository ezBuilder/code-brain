# Code Brain 딥리서치 업그레이드 라운드 (2026-07-30)

> 방식: 4각도 병렬 딥리서치(학술 논문 · GitHub OSS · 커뮤니티(Reddit/HN/블로그) · 공식 표면(Claude Code v2.1.220 / Codex rust-v0.146.0 / MCP 2026-07-28)) → 기존 보유(6/21 라운드 + v0.5~0.6 접목분) 대조 → 진짜 증분만 채택.
> 원칙: 자체 코퍼스 재측정 없는 외부 벤치 수치는 채택 근거에서 제외. 기존 결론(BM25 lexical-first, 오프라인, ratchet)은 2026-07 기준으로도 재확인됨(Cursor semsearch·LlamaIndex·Anthropic context-engineering 모두 hybrid/lexical-first 합의).

## 이번 라운드 채택(구현 완료)

1. **식별자 서브토큰 이중발행 인덱싱 (schema v9, 기본 ON, `AI_SEARCH_SUBTOKENS=0` 킬스위치)**
   - unicode61은 snake_case만 분해하고 camelCase는 한 토큰 → split-word 질의가 camel 식별자를 놓침(실측 재현). contentless FTS5라 파생 텍스트를 자유롭게 색인 가능 → 인덱스 시점에 camel/숫자 경계 파트를 부가 발행.
   - 근거: identifier-aware dual-emission BM25 토크나이제이션 +27.8%(Java)/+22.5%(Ruby) (arXiv:2605.18561); CORE-Bench "정확 식별자 매칭이 lexical의 핵심 가치"(arXiv:2606.11864). Swift/JS 프로젝트(garim·navio·FlightRealSchedule)에 직접 효익.
2. **`code_retrieval` 평가축 재도입(7/21 보류 스냅샷의 최소 재랜딩) + camelCase 골든 케이스**
   - `ranking_metrics.py`+`code_retrieval_eval.py`+cases 4건, `make eval` 게이트 포함(23/23). 랭킹 변경의 회귀 가드이자 향후(PageRank/스켈레톤 등) 채택 판정 기반. run.py에 `assert_field_at_most/at_least` 단언 추가.
   - 근거: Sourcegraph/CoIR/CORE-Bench 공통 패턴(골든쿼리→Recall@K/MRR CI 게이트); 커뮤니티 합의 "임베딩 추가 전 자체 리트리벌 평가부터".
3. **legacy-schema 인덱스 쿼리 자가복구**
   - 스키마 버전 범프 후 첫 질의가 RuntimeError로 죽는 업그레이드 경로를 corrupt-index와 동일한 자동 재빌드로 치유(11개 설치 프로젝트의 v9 이행 무마찰).
4. **MCP tool annotations (2026 스펙 정합)**
   - 큐레이션된 read-only 31종 `readOnlyHint:true`, write 15종 명시(+sandbox_execute destructive/openWorld, rebuild idempotent). 불확실 도구는 무주석(fail-safe). Codex `writes` 승인모드·Claude 권한 UX와 정렬.

## 검증 결과(이 라운드)

- `pytest test_search_subtokens.py` 8/8 · search 스위트 45+124 green · `test_mcp_server.py` 11/11 · `test_repo_evals.py` 8/8.
- `make eval` 23/23(신규 축 포함) · `make lint` ok · `docs-check` ok · doctor green(audit_chain은 `ai audit repair-chain`으로 선치유, 1505건).
- 라이브: 실레포 인덱스 v9/3789청크, split-word 질의가 camelCase 보유 파일을 상위 반환.

## 고신호 이월 후보(미채택, 우선순위순 — todo 등록)

1. **PageRank 개인화 repo-map + 1-hop ego-graph를 context_pack에**: aider 알고리즘(가중 def/ref 그래프·personalization·토큰 예산 이분탐색) + RepoGraph(+avg 32.8% 상대, plug-in) + LocAgent(그래프+BM25 상보, 94.16% file Acc@5) + HippoRAG2 PPR. codegraph 테이블 재사용, 워커 선계산. M.
2. **Agentless식 skeleton/refs-only 모드**: chunk_meta(qualname/lines)로 시그니처 아웃라인 + hashline 앵커만 반환하는 detail level. Meta-RAG 60-75% 토큰 절감. S/M.
3. **쿼리측 심볼 부스트/변형 확장**: qualname 정확일치 보너스 + camel↔snake 변형. LLM 질의재작성은 금지(CORE-Bench: 개악 빈번). S.
4. **주입 컨텍스트 토큰 예산 doctor 체크**: SessionStart+UserPromptSubmit 주입 합계 측정·상한(±1k tokens, Cherny 가이드) + 규칙 수 상한(컴플라이언스 선형 감쇠 근거). obs에 계측 존재 → 게이트만 추가. S.
5. **MCP 스키마 슬리밍**: 61종 도구 설명·스키마 토큰 감량(~65tok/tool 목표), `response_format: concise|detailed`(Anthropic 206→72tok 사례), 그래프 3종 통합 검토. 커뮤니티 최다 실측 불만(55k/66k tok). M.
6. **킷 현대화(공식 표면)**: commands→skills(agentskills.io, Codex `.agents/skills` 미러) · Codex `[hooks]` 포팅(JSON 계약 거의 동일) · PreToolUse `permissionDecision`/`updatedInput` 활용 · `mcp_tool` 훅 타입으로 프로세스 스폰 제거 · UserPromptSubmit 30s 신예산 유의 · 플러그인 패키징. ※ kits/ 는 이전 세션 dirty 작업과 겹침 — 그 정리 후 진행. M/L.
7. **메모리 recall 시 working-tree 검증(코드 우선) + 3회 재등장 승격**: 실무자 실측 "30세션 후 메모리 1/3 부패". memory_staleness 확장. M.
8. **번들 소비를 위한 재랜딩 잔여물**: code_navigation/memory_retrieval 축, dense_retrieval/index_control (7/21 스냅샷, git 히스토리 보존).

## 기각/보류 재확인

- 임베딩·리랭커 기본화: Cursor 데이터상 1,000+파일 대형 레포에서만 유의미(+12.5% avg는 조건부), 스테일 부담 2배 → 기존 opt-in 유지, code_retrieval 축으로 측정 후 판단.
- LLM 질의 재작성(CORE-Bench 개악), snapcompact 소비(비전 의존), memanto ITS(클라우드), FastCoder(하네스 부적합) — 기존 기각 유지.

*1차 출처: arXiv 2605.18561 · 2606.11864 · 2503.09089(LocAgent) · 2410.14684(RepoGraph) · 2506.15655(cAST) · 2508.02611(Meta-RAG) · 2502.14802(HippoRAG2) · aider repomap · Sourcegraph Cody blog · Cursor semsearch · Anthropic effective-context-engineering/writing-tools · code.claude.com/docs(hooks/skills/mcp/memory) · developers.openai.com/codex(hooks/config/skills) · modelcontextprotocol.io 2026-07-28 changelog · claude-mem · Continue.dev indexing · buger/probe · oraios/serena.*
