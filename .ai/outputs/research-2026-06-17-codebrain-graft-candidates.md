# Code Brain 접목 후보 신기술 — 딥리서치 보고 (2026-06-17)

딥리서치 1회(5각도 병렬 fan-out) 통계: 소스 26 fetch · 주장 114 추출 · 25 적대적 검증(3표 중 2표 반박 시 폐기) → **확정 23 / 기각 2**. 에이전트 108, 도구호출 661.

핵심 패턴(증거 다수): **컨텍스트 창 크기가 아니라 retrieval 품질이 repo 성능을 좌우**. 레포가 커질수록 RAG가 long-context를 이김(3-0), focused 컨텍스트가 full-history 덤프를 모든 모델에서 이김.

---

## 우선순위 접목 후보 (shortlist)

| # | 기술 | 접목 모듈 | 판정 | 근거(검증) |
|---|------|-----------|------|-----------|
| 1 | GEPA 리플렉티브 프롬프트 진화 | `prompt_growth` + `eval_loop` + `self_improve` | **ADOPT(파일럿)** | RL(GRPO) 대비 ~10–20%↑, 롤아웃 최대 35×↓, MIPROv2 상회 |
| 2 | 코드 특화 임베더 교체 | `embedding.py` | **ADOPT(파일럿)** | Qwen3-Embedding-8B 80.68 vs Gemini 74.66 MTEB-Code; CodeXEmbed-7B CoIR SOTA |
| 3 | 컨텍스트-로트 대응 컨텍스트 엔지니어링 | `context_budget` + `context_pack` + 훅 주입순서 | **ADOPT** | Chroma(18모델)+NoLiMa(피어리뷰); full~115k 읽으면 30–60%↓ |
| 4 | Sleep-time compute(오프라인 선계산) | `loopd` + worker pools + `memory_tier` | **PILOT** | test-time 연산 ~5×↓, 분할상환 2.5× |
| 5 | CodeRAG bigraph(요구사항↔코드 앵커) | `codegraph` + `search` | **PILOT** | DevEval +17.71 vs embedding-RAG |
| 6 | A-MEM 자기진화 메모리 | `memory_sync` + `procedural_memory` | PILOT/WATCH | Zettelkasten式 link 진화(3-0) |
| 7 | Temporal-KG (Zep/Graphiti) | 메모리 계층 | **WATCH** | DMR 94.8 vs 93.4(미미); 18.5%/90% 주장 **기각** |
| 8 | Qwen3-Reranker 업그레이드 | `reranker.py` | **WATCH** | 8B 임베더 대비 ~0.5pt 한계효용 |

---

## 테마별 상세

### T1. 코드 retrieval & context
- **코드 특화 임베더** (Qwen3-Embedding / CodeXEmbed=SFR-Embedding-Code): 현 `embedding.py` 모델을 코드 SOTA로 교체. 통합=모델 어댑터 교체 + 재인덱스. 위험=차원/지연/라이선스. **채택 전 voyage-code-3, Gemini Embedding 2(~84) 재벤치 필수.**
- **CodeRAG bigraph**: 레포를 요구사항↔코드 이분그래프로 모델링해 앵커 매핑. 기존 `codegraph`(tree-sitter/ast-grep/LSP) 위에 "requirement→symbol" 엣지 추가. "그래프 retrieval이 언어별 수작업·일반화 저하" 주장은 검증에서 **기각(1-2)** → 그래프 접근에 우호적.
- **reranker**: 이미 보유. Qwen3-Reranker는 강한 8B 임베더 위 ~0.5pt → 한계효용, 보류.
- 검증: RAG가 repo 성장 시 long-context 우위(3-0) → Code Brain의 retrieval-우선 설계 정당화.

### T2. 에이전트 장기 메모리
- **Sleep-time compute**: 유휴 시 worker pool/`loopd`로 repo 사실·요약·임베딩을 선계산해 `memory_tier` hot/warm에 적재. 단 벤치(GSM-Symbolic/AIME)는 수학·추론 → **코드 전이 미검증**, 파일럿 후 측정.
- **A-MEM**: 새 메모리가 기존 노트의 링크/태그를 갱신하는 자기진화. `memory_sync`/`procedural_memory`에 링크 진화 추가.
- **Temporal-KG(Zep/Graphiti)**: 이득 미미(DMR +1.4pt), 핵심 우위 주장 기각. 기존 tier+staleness+sync로 더 싸게 커버 → **watch**.

### T3. 컨텍스트 엔지니어링 & 토큰 최적화
- **컨텍스트 로트**: 길이에 따라 비균일 성능 저하, 낮은 의미유사도 needle일수록 심함 — Chroma(18모델) + 피어리뷰 NoLiMa로 **복제됨**. 대응: `context_pack`을 JIT/focused로 큐레이트·축약, `context_budget` 강화.
- **KV-cache 인지 정렬**: 훅 주입 프리픽스를 안정적으로 고정해 캐시 적중률↑(저비용·고ROI).
- **LLMLingua式 축약**: 컨텍스트 압축 파일럿.

### T4. 오케스트레이션 & 라우팅
- 대부분 Code Brain이 이미 보유(`task_router`/`route_floor`/`speculative`/workflows). 신규 가치는 **verifier/critic 모델**, deterministic workflow engine 정도 → 선별 watch.

### T5. 평가 & 자기개선
- **GEPA**(gepa-ai/gepa, 2507.19457): 리플렉티브 프롬프트 진화. RL 대비 적은 롤아웃으로 큰 이득, MIPROv2 상회. Code Brain은 `eval_loop`+`prompt_growth`+`self_improve`를 이미 보유 → **호스팅 비용 최저, 1순위 접목**.
- LLM-as-judge / trajectory eval 진전(2512.18552, 2511.13646): `eval_loop`·`trajectory` 보강 파일럿.

---

## Caveats (정직성)
- SOTA 급변 — 채택 시점 재벤치 필수(Gemini Embedding 2 ~84, voyage-code-3).
- 일부 저자 자기보고 수치(Zep DMR, Qwen reranker); Chroma는 벤더지만 NoLiMa로 복제.
- 헤드라인 일부 약한 베이스라인(CodeRAG +17.71은 vs embedding-RAG, vs no-RAG 아님).
- 메모리/sleep-time 벤치는 수학·추론·대화형 → **코드 에이전트 전이 미입증**.
- 기각된 주장: Zep LongMemEval 18.5%/90%-지연 우위; "그래프 코드 retrieval 일반화 저하".

## 주요 출처(primary)
- Qwen3-Embedding 2504.10046 · CodeXEmbed 2411.12644 · CoIR github.com/coir-team/coir · CodeRAG 2510.04905
- Sleep-time 2504.13171 · A-MEM 2502.12110 · Zep 2501.13956 · Chroma Context Rot · ReMem 2511.20857
- Anthropic context-engineering · LLMLingua 2403.12968 · GEPA 2507.19457 + gepa-ai/gepa
