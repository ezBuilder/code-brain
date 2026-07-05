# Code Brain — 소개 및 접목 기술 전수 (Tech Dossier)

> Version: v0.5.0 · Repo: https://github.com/ezBuilder/code-brain · License: Apache-2.0
> 용도: 딥리서치 입력 자료. 모든 항목은 코드/릴리스 기준(추측 아님). 출처·라이선스·상태(implemented/pilot/watch/reject) 명시.

> **v0.5.0 추가(딥리서치 통합비판 도출, 전부 opt-in·오프라인·stdlib·기본 OFF)**: ① Memory DAG 엣지 — `record_decision`에 contradicts/derives_from/expires_at 관계(만료 결정 surface 제외). ② MCP resources(`AI_MCP_RESOURCES`) — plan/report/handoff/session을 `codebrain://` 읽기전용 리소스로. ③ cAST 구조 청킹(`AI_AST_CHUNK`) — Python stdlib ast 재귀분할+형제병합(검색 recall pilot). 보류=DraCo dataflow(연구급). 1차 출처: cAST arXiv 2506.15655 / astchunk(MIT) / DraCo 2405.19782.

---

## 1. 한 줄 정의

**Code Brain은 AI 코딩 에이전트(Claude Code · Codex CLI · Google Antigravity)에 "프로젝트 로컬, 오프라인 우선"의 기억·코드검색·훅 정책·MCP 도구·감사추적·자기개선을 한 워크스페이스(`.ai/`)로 주입하는 에이전트 하네스/인프라**다. 레포 하나를 "에이전트 운영 계층"으로 바꾼다.

## 2. 핵심 설계 원칙 (다른 메모리 도구와의 차별점)

- **로컬 우선·오프라인**: 훅/MCP 핫패스는 네트워크·LLM 호출 금지(결정론). 인덱스·메모리는 레포 안 `.ai/`.
- **결정론 + 측정 게이팅**: 자기개선/라우팅 변화는 ratchet(실측 회귀 없을 때만 채택)으로만 반영.
- **승인 게이트 불가침**: 보안/결제/파괴/배포/시크릿은 자동 실행 금지. 훅이 우회를 차단.
- **리트리벌 우선**: 컨텍스트 창 크기보다 검색 품질. JIT/focused 컨텍스트로 토큰 절약.
- **install-into**: Claude/Codex/Antigravity 3개 하네스에 동일 계약으로 설치(플랫폼 비종속).
- **redaction & 감사**: MCP/진단/외부 출력 비식별화, 해시체인 JSONL 감사.

---

## 3. 네이티브 서브시스템 (built-in, ~75 모듈 / MCP 도구 61종 / 스킬 8종)

### 3.1 검색 & 리트리벌
- BM25/FTS5 코드 검색(`search.py`), contentless FTS5, stale 자동 refresh.
- 임베딩(`embedding.py`, ONNX MiniLM 옵션 `[dense]`), reranker(`reranker.py`).
- `context_pack`(에이전트용 압축 컨텍스트) + `context_budget`(토큰 예산·KV-cache 인지 프리픽스).
- 청크 필터(`chunk_filter.py`), 코드베이스 지도(`codebase_map.py`).

### 3.2 코드 인텔리전스
- 콜그래프(`codegraph.py`, tree-sitter/AST; callers/callees/symbol/trace/impact/architecture, 현재 Python).
- hashline 편집 앵커(`hashline.py`, 줄+해시로 stale-edit 방지) — `code_read_hashline`.
- ast-grep 구조 검색(`astgrep_integration.py`), AST 검증(`ast_verify.py`).
- LSP-as-MCP(`lsp.py`, multilspy per-call, `code_find_references`/`code_goto_definition`).

### 3.3 메모리 (계층형·타입형)
- 메모리 티어 hot/warm/cold + retention_score(연속 감쇠·타입 salience·reinforcement) — MemGPT식 page-in/out(`memory_tier.py`, `memory_hot.py`).
- decisions/todos/lessons/procedures/session-notes/handoff/evidence(append-only JSONL).
- 통합 회상(`memory_recall.py`), 결정 필터(`list_decisions`), 절차 기억(`procedural_memory.py`).
- staleness 감지(`memory_staleness.py`), Mac↔VPS 동기(`memory_sync.py`), 해시체인 감사(`audit_*`).

### 3.4 오케스트레이션 (멀티에이전트 워커 풀)
- loopd 토큰프리 디스패치 컨트롤플레인(`loopd.py`) + 파일 큐 inbox/processing/done/dead + lease 복구(`loop_engineering.py`).
- 워커 레지스트리/프로필/런치(tmux)(`worker_*`, `tmux_adapter.py`).
- 모델 라우팅: task 분류(`task_router.py`) + 학습형 최소-티어 floor(`route_floor.py`, ratchet).

### 3.5 자기개선 & 평가
- prompt_growth(결정론적 프롬프트 진화·ratchet), eval_loop(pass-rate fitness), self_improve(저렴한 non-self judge → M_core 게이트 → ratchet).
- speculative(패턴 마이닝), trajectory(궤적 요약), precall(사전 위험 규칙).

### 3.6 안전 / 거버넌스
- redaction(`redact.py`), secret scan(`secret_scan.py`, git-baseline), commit secret 가드(`commit_guard.py`), stream guard, self-write guard.
- policy(CI read-only 강제), trust(`trust.py`), security findings 추적.

### 3.7 통합 / 하네스 / 자율연구
- 훅(SessionStart/UserPromptSubmit/PreToolUse/PostToolUse/Stop/SubagentStart·Stop/PreCompact·PostCompact)(`hooks.py`).
- MCP 서버(`mcp_server.py`, 61 tools, compact/usage/core 프로필), sandbox 실행(요약+exec_id)(`sandbox.py`).
- autoresearch(`autoresearch/*`): sandbox 실행·citation 검증·deepresearch 루프.
- install/upgrade/render/doctor/release-gate, 글로벌 킷(`global_kit.py`), Antigravity/Codex/Claude 설정 생성(`mcp_config.py`).

---

## 4. 접목(graft)된 외부 기술 — 출처별

### A. memanto (MIT, moorcheh-ai) → **v0.3.0 구현**
> 출처 레포: https://github.com/moorcheh-ai/memanto · 분석: `.ai/outputs/research-2026-06-18-memanto-graft-analysis.md`
> 차용 원칙: memanto의 클라우드(Moorcheh)·LLM 의존부는 이식 안 함 — *아이디어/포맷만* 로컬 재구현.

| ID | 접목 기술 | 무엇 | 상태 |
|----|-----------|------|------|
| G2 | **통합 회상(answer-over-memory)** | decisions·failures·lessons·procedures를 confidence×relevance×recency로 통합 랭킹 + 인용 블록(LLM 합성 없음). `memory_recall.py`, MCP `memory_recall` | implemented |
| G3 | **결정 필터 조회** | kind/status/tag/source/text 온디맨드 필터(fold-by-id·retired 제외). `list_decisions` MCP/CLI | implemented |
| G4 | **오프라인 충돌 탐지** | 모순 결정쌍(토큰중첩+극성) advisory 탐지, `conflicts.jsonl`(결정 미변경), page_out flag-gated. `memory_conflicts.py` | implemented(advisory) |
| G10 | **메모리 워크플로 스킬 2종** | `diagnose-with-memory`, `tdd-with-memory`(회상-선행/저장-후행) | implemented |

memanto에서 **차용한 개념**(provenance): 타입형 메모리 온톨로지, temporal/recency 가중, confidence+provenance 메타, 충돌(writeback) 탐지, 무LLM-추출 즉시검색.

### B. oh-my-openagent (OmO) / lazycodex (MIT, code-yeongyu) → **v0.4.0 구현**
> 출처 레포: https://github.com/code-yeongyu/oh-my-openagent (엔진), https://github.com/code-yeongyu/lazycodex (Codex 배포 래퍼)
> 분석: `.ai/outputs/research-2026-06-19-lazycodex-omo-graft-analysis.md`
> 차용 원칙: OmO의 Codex/OpenCode 라이프사이클·spawn_agent 결합 배선은 이식 안 함 — *메커니즘/설계*만 CB 네이티브 재작성.

| ID | 접목 기술 | 무엇 | 상태 |
|----|-----------|------|------|
| G1 | **acceptance 완료 게이트** | reviewer "pass"를 sandbox 결정론적 재실행으로 머신 검증 + typed evidence. `acceptance.py`, `loop --require-acceptance`/`loop acceptance` | implemented(opt-in) |
| G2 | **내구 plan 상태머신(Boulder식)** | 체크박스=상태, 디스크 재유도, 크래시/compaction 생존. `plan_state.py`, `ai plan` | implemented |
| G3 | **Stop-훅 continuation 루프(ultrawork식)** | plan 0 남을 때까지 자동 재프롬프트. `loop_continuation.py`. opt-in(`AI_LOOP_CONTINUATION`)·카운터/wall-clock cap·보안 block 미덮음 | implemented(opt-in, bounded) |
| G4 | **per-task 모델 fallback 체인** | transient(rate-limit/quota/overload) 분류→다른 패밀리/티어 재큐(MAX_ATTEMPTS). `error_classifier.py` | implemented |
| G5 | **LSP-as-MCP** | multilspy per-call(Python/pyright), `code_find_references`/`code_goto_definition`, 옵셔널·graceful. `lsp.py` | implemented(opt-in dep) |
| G9 | **Read 트리거 디렉토리 컨텍스트** | 편집 파일 상위 AGENTS.md/CLAUDE.md를 세션당 1회 주입. `dir_context.py`(`AI_DIR_CONTEXT`) | implemented(opt-in) |
| G11 | **behavior-lock 리팩터 규율** | 회귀테스트 GREEN 선행·SKIP-not-GUESS·KEEP리스트. `safe-refactor`/`lean-review` 스킬 보강 | implemented |
| G12 | **evidence-tier triage** | risk tier(loopd 분류기 재사용)로 evidence 깊이 스케일. `autonomous_harness.py` | implemented |
| — | **완료 규율(early-stop 방지)** | "N개 전부 완료, '계속할까요' 금지" 규칙 + AI_LOOP_CONTINUATION. 킷 rules/{CLAUDE,AGENTS}.md | implemented(글로벌) |

OmO에서 **차용한 개념**: verified-completion(독립 Oracle), 사람이 읽는 plan=상태, 카테고리별 모델 라우팅+fallback, LazyVim식 zero-config 배포.

### C. 학술/SOTA 딥리서치 채택 (papers) → 일부 네이티브로 실현
> 분석: `.ai/outputs/research-2026-06-17-codebrain-graft-candidates.md`(5각도 fan-out·적대적 검증)

| 기술 | 매핑/상태 | 1차 출처 |
|------|-----------|----------|
| **GEPA 리플렉티브 프롬프트 진화** | prompt_growth/eval_loop/self_improve 설계에 반영 | arXiv 2507.19457 + gepa-ai/gepa |
| **컨텍스트-로트 대응 컨텍스트 엔지니어링** | context_pack/context_budget JIT·KV-cache 정렬 | Chroma Context Rot, NoLiMa |
| **코드 특화 임베더** | embedding.py 어댑터(채택 시 재벤치 전제) | Qwen3-Embedding 2504.10046, CodeXEmbed 2411.12644 |
| **MemGPT식 메모리 페이징** | memory_tier hot/warm/cold page-in/out | MemGPT 2310.08560 |
| **Sleep-time compute(오프라인 선계산)** | loopd/worker pool 선계산(pilot/watch) | 2504.13171 |
| **A-MEM 자기진화 메모리** | memory_sync/procedural 링크 진화(watch) | 2502.12110 |
| **CodeRAG bigraph** | codegraph 위 요구사항↔코드(pilot) | 2510.04905 |
| **Temporal-KG(Zep/Graphiti)** | tier+staleness로 저비용 대체(watch) | 2501.13956 |

---

## 5. 평가했으나 보류/기각 (deep-research 참고용)

- **memanto**: 타입 분류기(G1)·신뢰도 재구성(G7)·휴리스틱 추출기(G9)·MD-event-log(G12)·LangGraph 어댑터(G14) → **기각**(죽은 코드/개인비서 의미론/CB 상위집합/미션역전).
- **OmO**: comment-checker(닫힌 Go 바이너리, reject), ultraresearch EXPAND(autoresearch가 상위, reject), 병렬 fan-out join·계층 AGENTS.md 생성·review-work 멀티레인·reset/continue·hashline auto-remap → **watch**(이미 보유/소비자 없음).
- **SOTA**: Qwen3-Reranker 업그레이드(한계효용, watch), Zep DMR 우위 주장(적대검증 기각).

## 6. 라이선스 / 프로버넌스

- Code Brain: **Apache-2.0**. memanto: MIT, OmO/lazycodex: MIT → 아이디어 차용 호환(코드 이식 아님, 전부 CB 네이티브 재구현).
- 모든 접목은 "메커니즘/설계 차용 + 로컬·오프라인 재작성"이며 외부 클라우드/LLM 결합부는 미이식.

## 7. 1차 출처 (papers & repos)

- 레포: moorcheh-ai/memanto · code-yeongyu/oh-my-openagent · code-yeongyu/lazycodex · gepa-ai/gepa · coir-team/coir
- 논문: GEPA 2507.19457 · Qwen3-Embedding 2504.10046 · CodeXEmbed 2411.12644 · CodeRAG 2510.04905 · MemGPT 2310.08560 · Sleep-time 2504.13171 · A-MEM 2502.12110 · Zep 2501.13956 · LLMLingua 2403.12968 · NoLiMa · Chroma "Context Rot"
- Anthropic context-engineering, OpenAI GPT-5.x model docs(OmO 라우팅 근거)

---
*생성: Code Brain v0.4.0 기준. 상세 분석 3건은 `.ai/outputs/research-2026-06-{17,18,19}-*.md`.*
