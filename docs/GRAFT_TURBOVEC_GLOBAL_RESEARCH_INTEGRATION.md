# Graft × TurboVec × Semantica × 전세계 연구자료 기반 Code Brain 적용·검증서

> **상태:** bounded graph/PPR 기본 활성화 + 검증 및 후속 gate 명세
>
> **검토일:** 2026-08-22 (Asia/Seoul)
>
> **검토 범위:** [NanoNets/Graft](https://github.com/NanoNets/Graft), [RyanCodrai/turbovec](https://github.com/RyanCodrai/turbovec), [semantica-agi/semantica](https://github.com/semantica-agi/semantica), 관련 1차 연구논문·공식 구현·벤치마크
>
> **현재 문서의 역할:** 외부 프로젝트를 그대로 이식하지 않고, 검증된 계약만 Code Brain의 BM25·구조 그래프·context pack·라인 단위 평가에 실제 반영한 실행 기록과 후속 gate

## 0. 결론부터

### 채택할 것

1. **Graft의 결정론적 구조 그래프**: AST 기반 노드·엣지 추출, 명확한 confidence/provenance, 모호한 엣지 추측 금지.
2. **Graft의 2계층 인덱싱**: 쿼리 경로는 구조·검색 인덱스만 사용하고, LLM 요약·concept card는 비동기 파생 산출물로 분리.
3. **Graft의 freshness/원자성 규칙**: content hash, extractor stamp, 단일 writer lock, lock 획득 후 재확인, atomic replace, 약간 오래된 인덱스 fallback.
4. **Graft의 bounded personalized PageRank**: 현재 `graph_context.py`가 만든 bounded one-hop caller/callee 후보만 PPR로 재정렬하고, `contains` 같은 계층 엣지는 walk에서 제외.
5. **TurboVec의 stable-ID·allowlist·durability 경계**: 벡터를 path/slot에 직접 묶지 말고 `uint64` 외부 ID와 SQLite authoritative mapping을 둔다.
6. **TurboVec의 선택적 quantized dense rerank**: BM25 후보와 ACL/path 후보를 먼저 만들고, 그 allowlist 안에서만 TurboVec을 호출한다.
7. **Semantica의 typed provenance 패턴**: entity/activity/source generation/span/invalidated 상태를 작은 evidence envelope와 canonical receipt로 축소한다.
8. **세계 연구의 공통 결론**: 항상 검색하지 말고(Repoformer), 검색 결과를 구조 그래프로 좁히고(LocAgent/RepoGraph), 변경 영향도와 계획을 따로 계산하고(CodePlan), 최종 품질과 별도로 line-level 탐색을 평가한다(SWE-Explore).

### 채택하지 않을 것

- Graft의 vendor benchmark 수치를 Code Brain 성능으로 복사하지 않는다.
- TurboVec을 BM25의 대체품이나 전역 ANN 플랫폼으로 만들지 않는다.
- query hot path에서 LLM 요약·네트워크·자동 모델 설치를 수행하지 않는다.
- 모든 scope를 RRF로 섞지 않는다. 공통 score denominator가 있는 단일 corpus는 magnitude-preserving fusion을 사용하고, 독립 corpus federation에만 RRF를 사용한다.
- 파일 전체를 맞혔다는 이유만으로 line/span 정확도를 성공으로 판정하지 않는다.
- Semantica 전체 패키지, 범용 ContextGraph/ontology/GraphDB 스택, query-time entity embedding, 임의 min-max/boost fusion을 넣지 않는다.
- raw reasoning/chain-of-thought/source quote/query/code body를 provenance receipt에 저장하지 않는다.
- 실패한 embedding을 random vector로 대체하거나, 비원자 JSON overwrite를 성공으로 취급하지 않는다.

### 현재 판단

Code Brain에는 이미 다음 기반이 있다.

- BM25/FTS5 중심 검색과 `context_pack` budget 적용
- auto-refresh 및 dirty/hash mismatch 대응
- Python AST 기반 `code_symbols`·`code_calls`
- caller/callee·call path·blast radius·architecture summary
- opt-in ONNX MiniLM dense layer와 BM25→dense→RRF 경로

따라서 **새 검색기를 하나 더 만드는 것이 아니라**, 현재 경로에 다음 두 파생 계층을 붙이는 것이 정답이다.

```text
source files
    │
    ├─ deterministic fingerprint ──► FTS5/BM25 + typed code graph
    │                                  │
    │                                  ├─ repo map / ego graph / PPR
    │                                  └─ exact source spans / refs-only
    │
    └─ optional embedding ───────────► float32 baseline
                                       │
                                       └─ optional TurboVec sidecar

query
  └─ validate → freshness → BM25 seed → graph expansion
                → optional dense allowlist rerank → score fusion
                → source span assembly → budget/abstention → context_pack
```


### 0.1 2026-08-22 실제 반영 상태

아래는 설계가 아니라 현재 작업트리에 구현된 범위다. 기본 호출은 `v2` graph/PPR이며 `representation=legacy`가 명시적 무그래프 롤백 경로다.

| 항목 | 상태 | 실제 표면 |
|---|---|---|
| `context_pack` v2 | **반영** | `legacy|v2|skeleton|refs-only`, lexical ranking 비변경, graph context bounded append |
| exact function span retrieval | **반영** | 함수 chunk가 provenance inner join에서 탈락하던 결함을 수정하고 source path + `chunk_path` + line span을 반환 |
| graph evidence envelope | **반영** | schema v2, extractor origin, `evidence_kind`, confidence, target resolution, generation, indexed/current content hash, invalidation |
| stale source suppression | **반영** | hash mismatch면 snippet·summary를 억제하고 `[STALE_SOURCE]` 표시 |
| canonical context receipt | **반영** | query·source body 없이 policy/generation/path/span만 canonical JSON SHA-256으로 결속 |
| graph 표현 축소 | **반영** | full/skeleton/refs-only와 64 KiB cap |
| bounded personalized graph rank | **기본 활성화** | 이미 bounded된 one-hop call graph에 stdlib-only PPR 적용; 2 hops/2,048 nodes/1,600 edges/25 iterations 상한 |
| line/span 품질 gate | **반영** | production `context_pack v2` 임시 인덱스를 file/exact/overlap/rank-weighted coverage로 평가하며 `make eval` 7번째 축으로 강제 |
| TurboVec | **보류** | stable ID/manifest/allowlist/float32 shadow/Recall·p95 gate와 실제 병목 증거가 생길 때만 도입 |
| Semantica package | **거부** | 패키지·무거운 의존성·범용 graph/reasoning stack은 설치하지 않고 provenance 의미만 흡수 |

**정직한 한계:** 이 구현은 Code Brain을 외부 벤치마크상 세계 1위로 증명한 것이 아니다. 현재 증명한 것은 legacy 롤백, exact-span 경로, source trust, bounded context/PPR, deterministic receipt, 반복 호출 무증가 계약, production eval contract다. 세계 1위 주장은 고정 corpus의 Recall/MRR/nDCG/line coverage와 p95를 경쟁 기준선에 대해 반복 측정한 뒤에만 가능하다.

### 0.2 기본 활성화·workspace 전파 증거

2026-08-22 현재 기본 `context_pack`을 `v2`로 전환하고, 이미 상한이 적용된 one-hop call graph 후보에 PPR를 연결했다. PPR는 query마다 메모리에서만 계산하며 파일·네트워크·optional package를 사용하지 않는다.

```text
max_hops=2
max_nodes=2048
max_edges=1600
iterations=25
restart=0.25
context bytes <= mode budget (balanced 기본 4096 bytes)
result limit <= 100
```

함께 닫은 durability/determinism 결함:

1. 최신 SQLite schema에서 read마다 `CREATE IF NOT EXISTS`/`user_version`을 다시 쓰던 경로에 완전-schema fast path를 추가했다.
2. ripgrep fallback의 filesystem event 순서와 bounded prefix가 receipt를 흔들던 문제를 `rg --sort path`와 canonical path/line 정렬로 제거했다.
3. installer가 source의 사용자 scratch `.ai/eval/`까지 전파할 수 있던 경로를 명시적으로 제외했다. 공식 gate `.ai/evals/`는 계속 전파한다.

검증 결과:

- source: 현재 non-CLI runtime `1880 passed, 5 skipped`; proof/activation/installer/CLI focused `160 passed`; 직전 full-suite snapshot은 `2033 passed, 5 skipped, 18 failed`(모두 기존 global-kit strict-doctor fixture); `make eval` `31/31`; lint 통과. docs check는 모든 선행 문서 검사를 통과한 뒤 동일 strict-doctor 실패로 종료한다.
- source live: warm-up 후 기본 `context_pack` 50회에서 receipt 1개, retrieval 파일 목록·크기·SHA-256 무변경, 4,083/4,096 bytes.
- 12개 consumer: 전부 index rebuild 성공, 기본 v2/policy/bounds 확인, 5회 receipt 동일, SQLite/generation 파일 무변경, context budget 준수.
- 전파 전후 모든 consumer의 branch·HEAD·Code Brain 외 status와 기존 `.ai/eval/` byte snapshot이 동일했다.

| consumer | index files | graph/PPR live | branch 보존 |
|---|---:|---|---|
| FlightRealSchedule | 588 | applied | `develop` |
| FlightRealSchedule-nextgen | 586 | applied | `codex/nextgen-flight-platform` |
| actraflow | 747 | applied | `develop` |
| blurivo | 800 | applied | `develop` |
| chatgpt2codex-private | 730 | applied | `develop` |
| fluxwright | 208 | policy active; fixture에 추출 call edge 없음 | `develop` |
| gain-mesh | 220 | applied | `develop` |
| model-forge | 175 | applied | `develop` |
| modern-transport-tycoon | 316 | applied | `develop` |
| navio | 3641 | applied; rg fallback 결정론 재검증 | `develop` |
| noop | 49 | policy active; fixture에 추출 call edge 없음 | `develop` |
| noop2 | 46 | policy active; fixture에 추출 call edge 없음 | `develop` |

strict doctor는 activation과 별개인 기존 project/global-kit 상태 때문에 12개 모두 비정상 종료했다. 공통 원인은 `global_kit_source_health`/`global_kit_install_drift`이고, 일부는 기존 `layout`, `storage_limits`, `audit_chain`, `secret_scan`도 보고했다. 현재 source strict doctor의 잔여 실패는 `global_kit_install_drift` 한 개다. 이 작업은 해당 사용자/운영 상태를 자동 수정하지 않았다.

수시 검증 명령은 `/cb-proof [query]`다. 인자 없이 실행하면 call edge가 있는 symbol을 자동 선택하고, query를 주면 legacy/v2를 같은 조건에서 비교한다. 정답 path/span까지 평가하려면 원시 CLI를 사용한다.

```bash
.ai/bin/ai context prove "결제 검증 호출 경로" \
  --expected-path backend/src/payment.py \
  --start-line 40 --end-line 90 \
  --repeats 5 --json
```

출력은 양쪽 top paths/path rank/span overlap/p50·p95, PPR 적용·ranked node 수, context budget, signature·receipt 결정론, warm-up 후 SQLite/generation/evidence 파일 무변경을 한 번에 판정한다. 정답을 주지 않은 실행의 `effect.status=unmeasured`는 품질 우열을 주장하지 않고 구조·결정론·내구성만 증명했다는 뜻이다.

---

## 1. 검토 방법: 프로젝트별 5회 반복 검토

세 저장소를 같은 기준으로 **다섯 개의 독립 검토 pass**로 나눠 pinned commit의 README, 핵심 구현, 테스트, persistence/security 경로를 교차 확인했다. Semantica는 architecture/retrieval/reasoning/persistence·MCP/benchmark의 다섯 pass로 추가 검증했다. 아래 표는 단순 요약이 아니라 Code Brain에 반영할 판정표다.

### 1.1 고정한 외부 기준점

| 프로젝트 | 검토 기준점 | 확인한 성격 |
|---|---|---|
| Graft | [`65a76e5`](https://github.com/NanoNets/Graft/commit/65a76e5edd4098e0c7f4749d1e87f15ed741d069) | `@nanonets/graft` 0.12.0 계열, 구조 graph·ask·MCP·refresh·telemetry·테스트 |
| TurboVec | [`ccab9f3`](https://github.com/RyanCodrai/turbovec/commit/ccab9f325e6ce2a270a87daf01ae4e443bcf2d49) | 1.0.0, Rust core·Python binding·v7 sync·stable-ID·adversarial tests |
| Semantica | [`5c6b40f`](https://github.com/semantica-agi/semantica/commit/5c6b40f36ceb8963ec76a9f0113363546b8675e8) | package 0.6.6, MIT, ContextGraph·retrieval·provenance·reasoning·persistence·MCP·tests |

### 1.2 다섯 pass 결과

| Pass | Graft | TurboVec | Semantica | Code Brain 판정 |
|---|---|---|---|---|
| 1. 표면·구조 | deterministic graph와 LLM enrichment 분리 | quantized Rust index와 optional integration | 4천 줄대 in-memory ContextGraph, broad all-in-one API | framework 이식 대신 작은 typed 계약만 채택 |
| 2. retrieval·자료구조 | typed node/edge, conservative resolution | slot과 stable ID 분리, allowlist | fallback-heavy retriever, query-time embedding, 임의 boost/min-max | lexical ranking 유지; graph는 context append, dense는 gate 후 |
| 3. reasoning·provenance | relation/confidence와 body hash | calibration/format metadata | PROV형 lineage는 유용하나 raw reasoning/source quote 저장 위험 | evidence refs와 canonical receipt만 저장; CoT 금지 |
| 4. persistence·MCP·보안 | atomic checkpoint, lock/re-probe | v7 checksum/redo/durable write | ContextGraph direct overwrite, MCP load API 불일치; SSRF 방어 일부 강점 | 기존 atomic/private I/O와 strict MCP schema 유지 |
| 5. tests·benchmark·한계 | graph/freshness 테스트; 수치는 vendor claim | recall/filter/persistence/adversarial tests | mock/stub·missing benchmark 경로·품질 qrels 부족 | production qrels·5회 반복·line span·p95가 release proof |

### 1.3 검토 중 반복해서 확인한 공통 원칙

1. **Derived artifact는 source보다 권위가 높아질 수 없다.** graph·summary·vector 모두 source snapshot과 generation을 가리켜야 한다.
2. **불확실성을 결과에 남긴다.** extracted와 inferred를 같은 점수로 제공하지 않는다.
3. **필터를 후처리하지 않는다.** ACL/path/scope 후보 ID를 검색 kernel에 전달하고, 결과 개수를 `min(k, allowed_count)`로 보장한다.
4. **원자적 업데이트와 검색 품질을 함께 테스트한다.** 정상 파일에서 잘 찾는 것만으로는 충분하지 않다.
5. **검색을 거부할 수 있어야 한다.** 검색이 이득이 없는 질의에 context를 덧붙이는 것은 오류다.

---

## 2. Graft 정밀 검토와 Code Brain 반영안

### 2.1 구조: 결정론적 Tier 1 + 선택적 의미 Tier 2

Graft의 핵심은 **graph를 먼저 만들고 의미 요약을 나중에 붙이는 것**이다.

- **Tier 1:** tree-sitter로 파일·symbol·import·call·inheritance 등을 추출한다. 네트워크와 LLM 없이 재현 가능하다.
- **Tier 2:** 파일별 `summary`와 `crux`를 LLM으로 보강한다. `body_hash`가 같으면 재계산하지 않고, 실패해도 구조 graph는 남는다.
- graph 저장 시 `body_text` 같은 검색용 원문은 graph payload에서 제거하고, agent가 요청할 때 source 파일에서 읽는다.
- checkpoint는 atomic write이며, 중단 후 같은 `body_hash`의 완료 결과를 재사용한다.

Code Brain에 적용할 때는 현재 `search.py`의 FTS/codegraph 증분 갱신과 같은 source set을 사용해야 한다. 별도 파일 열거기를 만들면 ignore 규칙과 source hash가 갈라진다.

**적용 결정:**

```text
source enumeration/fingerprint = existing search.py contract
symbol/call extraction         = codegraph.py 확장
graph persistence              = existing SQLite + schema_version
LLM summary/concept            = offline derived worker only
query context                  = graph_context.py / context_pack orchestration
```

### 2.2 graph node/edge 계약

Graft의 [`types.ts`](https://github.com/NanoNets/Graft/blob/65a76e5edd4098e0c7f4749d1e87f15ed741d069/src/graph/types.ts)는 node에 path, span, signature, exported, origin, `body_hash`, summary state를 보관하고 edge에 relation과 confidence를 둔다. 이 계약을 Code Brain의 relational schema로 옮긴다.

```text
graph_nodes(
  node_id, repo_rel_path, scope, qualname, kind,
  start_line, end_line, signature, exported,
  body_hash, source_generation, extractor_stamp,
  summary_state, summary, origin
)

graph_edges(
  edge_id, source_id, target_id, relation,
  confidence, resolver, source_path, source_line,
  source_generation, is_external
)
```

초기 relation 집합:

```text
contains       # 계층 표시용; graph walk에는 기본 제외
imports
calls
references
implements
extends
produces       # optional semantic relation
configures     # optional semantic relation
validates      # optional semantic relation
```

confidence 집합:

```text
extracted      # 동일 파일/정확한 AST·LSP 확인
lsp_resolved   # LSP로 확인
inferred       # unique cross-file name 등 규칙 기반 추론
unresolved     # 원문만 보존, target node로 승격하지 않음
```

resolution 규칙은 Graft의 [`resolve.ts`](https://github.com/NanoNets/Graft/blob/65a76e5edd4098e0c7f4749d1e87f15ed741d069/src/graph/resolve.ts)처럼 보수적으로 고정한다.

1. 동일 파일 exact match를 우선한다.
2. cross-file은 unique match일 때만 `inferred`로 연결한다.
3. 후보가 여러 개면 추측하지 말고 unresolved/raw target으로 남긴다.
4. member call은 receiver type/owner-qualified method가 없으면 bare-name fallback을 하지 않는다.
5. 표준 라이브러리·vendor·외부 package는 `is_external=true`로 표시하고 기본 walk에서 제외한다.

### 2.3 freshness와 증분 갱신

Graft의 [`refresh.ts`](https://github.com/NanoNets/Graft/blob/65a76e5edd4098e0c7f4749d1e87f15ed741d069/src/graph/refresh.ts)와 [`fingerprint.ts`](https://github.com/NanoNets/Graft/blob/65a76e5edd4098e0c7f4749d1e87f15ed741d069/src/graph/fingerprint.ts)에서 가져올 규칙:

- stat(`size`, `mtime`)은 빠른 후보 판정일 뿐 최종 source truth가 아니다.
- build 시에는 실제 bytes hash를 확인한다.
- extractor/parser 버전이 바뀌면 내용이 같아도 graph를 무효화한다.
- writer lock을 기다린 뒤 drift를 다시 probe해 중복 rebuild를 막는다.
- refresh는 Tier 1 구조만 갱신하고 query hot path에서 LLM/network를 부르지 않는다.
- graph가 갱신 실패하면 직전 atomic snapshot을 사용하되 `freshness=stale`를 반환한다.
- graph가 아예 없으면 read path가 무거운 build를 몰래 시작하지 않는다. 명시적 rebuild 또는 사전 worker가 필요하다.

Code Brain용 manifest 최소 형태:

```json
{
  "schema_version": 2,
  "source_generation": "sha256:...",
  "extractor_stamp": "codegraph-py-v2",
  "indexed_files": 123,
  "graph_nodes": 456,
  "graph_edges": 789,
  "dense_generation": "gen-00017",
  "status": "current"
}
```

### 2.4 ranking: BFS에서 ego graph/PPR로 확장

Graft의 [`graphrank.ts`](https://github.com/NanoNets/Graft/blob/65a76e5edd4098e0c7f4749d1e87f15ed741d069/src/ask/graphrank.ts)는 lexical seed distribution에서 시작하는 personalized PageRank를 사용한다. 기본 아이디어는 Code Brain에도 유효하지만, 기존 `blast_radius()`와 역할을 섞지 않는다.

| 기능 | 목적 | 기본 walk |
|---|---|---|
| `callers/callees` | 정확한 방향성 질문 | directed BFS |
| `blast_radius` | 변경 영향도 | reverse caller BFS |
| `ego_graph` | seed 주변 맥락 | 1-hop directed/undirected 옵션 |
| `repo_map` | 전체 orientation | in-degree hub/hotspot 요약 |
| `ppr_rank` | lexical seed 주변 관련도 | personalized random walk |

초기 PPR 설정은 **가설값**으로 둔다.

```text
alpha/restart = 0.25
iterations    = 25
max_hops      = 2
max_nodes     = 10,000
max_edges     = 50,000
walk_relations = calls, references, imports, implements, extends
```

`contains`는 repo tree를 표현하는 데는 필요하지만 모든 파일이 부모·자식으로 연결되어 hub가 되는 것을 막기 위해 walk에서 제외한다. 그래프가 커지면 PPRGo 계열의 residual/truncation을 비교하되, 먼저 bounded deterministic implementation을 출하한다.

### 2.5 scope fusion

Graft의 [`fuse.ts`](https://github.com/NanoNets/Graft/blob/65a76e5edd4098e0c7f4749d1e87f15ed741d069/src/ask/fuse.ts)가 해결한 문제는 Code Brain multi-project/workspace에도 그대로 있다.

- 단일 graph의 여러 scope는 **공통 IDF·공통 length denominator**로 lexical score를 계산한다.
- 약한 scope가 자기 내부 rank-1이라는 이유만으로 강한 scope를 밀어내지 않게 `participation gate`를 둔다.
- identifier/path hit가 전혀 없는 body-only scope는 다른 scope에 실제 identifier hit가 있을 때 억제한다.
- 공통 score scale을 공유할 수 없는 독립 repo federation에만 RRF를 쓴다.

현재 Code Brain의 동적 RRF k(`30..120`, `N=1024`에서 약 `60`)는 유지한다. 단, **RRF를 만능 fusion으로 확장하지 않는다.**

### 2.6 Graft에서 그대로 복사하지 말아야 할 것

- LLM이 만든 concept link를 extracted dependency와 동일한 신뢰도로 노출하지 않는다.
- generic language parser가 file-only fallback을 했는지 결과에 표시한다.
- `crux`의 line pointer를 source truth로 사용하지 않는다. source를 다시 읽어 exact span을 검증한다.
- telemetry는 allowlist contract를 별도로 검토하고, Code Brain의 path/query/secret redaction 경계를 재사용한다.
- README의 “4x cheaper”, “SWE-bench 54→66” 등은 Graft 자체 실험 주장이다. Code Brain의 release gate 수치로 쓰지 않는다.

---

## 3. TurboVec 정밀 검토와 Code Brain 반영안

### 3.1 무엇을 제공하는가

TurboVec은 Google Research의 TurboQuant 계열을 Rust SIMD 구현으로 제공한다. [TurboQuant 원 논문](https://arxiv.org/abs/2504.19874)은 random rotation과 좌표별 scalar quantization으로 online/data-oblivious quantization을 설명한다. TurboVec README와 [API 문서](https://github.com/RyanCodrai/turbovec/blob/ccab9f325e6ce2a270a87daf01ae4e443bcf2d49/docs/api.md)는 2/3/4-bit, incremental ingest, save/load/sync, allowlist filtering을 구현 계약으로 둔다.

핵심은 **정확한 원본 벡터 검색을 대체하는 것이 아니라, 후보 집합의 dense rerank 비용과 저장량을 줄이는 파생 인덱스**라는 점이다.

### 3.2 API 의미와 함정

#### `TurboQuantIndex`

- vector를 positional slot에 보관한다.
- `swap_remove`는 O(1)이지만 마지막 slot이 이동한다.
- slot 번호를 Code Brain의 logical node ID로 사용하면 삭제 후 allowlist가 틀린다.
- `dim`은 첫 add에서 추론할 수 있지만, 차원은 양수·8의 배수·최대 16384라는 계약을 지킨다.
- Python 경계는 2-D contiguous `float32`; NaN/Inf/과대한 입력을 거부한다.

#### `IdMapIndex`

- 외부 `uint64` ID와 내부 slot을 분리한다.
- 삭제는 ID map과 `swap_remove`를 함께 갱신한다.
- allowlist는 stable ID 기준으로 전달한다.
- `k`는 허용 ID 수보다 커도 결과를 padding하지 않고 `min(k, n_allowed)`개만 반환한다.

**Code Brain에서는 `IdMapIndex` 계약을 필수로 삼는다.** positional API를 직접 노출하지 않는다.

### 3.3 TurboQuant calibration

TurboVec의 TQ+ calibration은 대표적인 sample을 별도로 넣어야 한다. 문서상 1024~2048행 정도가 실용적인 starting point지만, 이는 Code Brain corpus에 대한 보장이 아니다.

적용 규칙:

1. embedding model·normalization·dimension을 먼저 고정한다.
2. repository/language/scope 분포를 반영한 대표 sample을 deterministic seed로 뽑는다.
3. calibration hash를 manifest에 기록한다.
4. calibration 후 vector를 추가하며, 이미 추가된 vector의 calibration 상태가 바뀌면 조용히 섞지 말고 full rebuild를 예약한다.
5. quantized recall이 float32 baseline gate를 통과하지 못하면 BM25 또는 float32 dense로 자동 degrade한다.

### 3.4 persistence와 crash consistency

TurboVec v7 sync는 alternating commit headers, block checksum/digest, redo operation을 사용한다. `write(durable=true)`는 temp sibling + atomic rename + 가능한 directory fsync 경로를 갖고, `durable=false`는 속도 우선 모드다.

Code Brain은 이를 그대로 source DB와 한 transaction이라고 가정하면 안 된다. 파일과 SQLite 사이에는 cross-file atomicity가 없기 때문이다.

권장 2-phase generation protocol:

```text
1. SQLite에 새 generation을 pending으로 기록
2. 새 *.tvim sidecar를 sibling temp에 build
3. TurboVec durable write + atomic rename
4. sidecar manifest와 ids_hash를 검증
5. SQLite transaction으로 active_generation flip
6. 이전 generation은 grace period 뒤 GC
```

reader는 `active_generation`과 sidecar manifest가 모두 일치할 때만 사용한다. crash가 나면 old generation으로 안전하게 돌아가고, orphan sidecar는 query path가 아니라 maintenance가 치운다.

### 3.5 concurrency·Python·보안에서 가져올 것

TurboVec Python binding의 [Rust/Python 경계](https://github.com/RyanCodrai/turbovec/blob/ccab9f325e6ce2a270a87daf01ae4e443bcf2d49/turbovec-python/src/lib.rs)는 다음 방어를 보여준다.

- shape/dtype/C-contiguous 검증 후 buffer를 snapshot한다.
- lock을 기다리는 동안 GIL을 잡고 있지 않는다.
- concurrent readers와 exclusive writer를 분리한다.
- fork 뒤에 상속된 thread pool을 재사용하지 않고 process-local pool을 만든다.
- numpy bool mask를 Rust `bool` reference로 위험하게 해석하지 않는다.

Code Brain Python runtime에 당장 Rust binding을 넣지 않더라도, adapter contract에 이 검증을 요구한다.

필수 reject 목록:

```text
wrong ndim / dtype / contiguous flag
NaN / ±Inf / absurdly large coordinate
dim mismatch / unsupported bit width
negative or overflowing k
unknown allowlist ID policy violation
truncated / corrupt / huge-declared-count index
manifest model/dim/normalization/generation mismatch
```

### 3.6 TurboVec의 한계

- TurboVec은 Code Brain의 전체 corpus 규모와 query distribution에서 검증되지 않았다.
- quantization은 recall을 잃을 수 있다. “메모리 절감”만으로 enable하지 않는다.
- stable ID를 사용해도 path rename, symbol split/merge, chunk re-segmentation은 logical ID migration이 필요하다.
- 현재 Code Brain의 작은 corpus에서는 BM25 + source span이 더 빠르고 정확할 수 있다.
- billion-scale ANN 연구(DiskANN, HNSW, ScaNN)를 TurboVec 성능으로 대체해 해석하지 않는다.

---

## 4. Semantica 5회 정밀 검토와 Code Brain 판정

### 4.1 기준점과 전체 판단

검토 기준은 main의 [`5c6b40f`](https://github.com/semantica-agi/semantica/commit/5c6b40f36ceb8963ec76a9f0113363546b8675e8), package 0.6.6, MIT license다. [`pyproject.toml`](https://github.com/semantica-agi/semantica/blob/5c6b40f36ceb8963ec76a9f0113363546b8675e8/pyproject.toml)의 기본 의존성은 NumPy/Pandas/SciPy/scikit-learn/UMAP/spaCy/Transformers/Torch/SentenceTransformers/RDFLib/NetworkX/FAISS/FastEmbed/ONNX 계열까지 넓다.

**판정:** Semantica는 Code Brain의 대체 검색기나 core dependency가 아니다. provenance vocabulary, explicit/inferred 구분, valid/record time의 의미, bounded lineage만 참고한다. 범용 graph/reasoning/vector stack은 중복·운영비·보안 surface가 너무 크다.

### 4.2 Pass 1 — ContextGraph·decision·temporal model

[`context_graph.py`](https://github.com/semantica-agi/semantica/blob/5c6b40f36ceb8963ec76a9f0113363546b8675e8/semantica/context/context_graph.py)는 node/edge/index/RLock/decision/temporal/persistence를 한 클래스에 모은 대형 in-memory graph다. `valid_from/valid_until`, 명시적 causal relation, decision lifecycle은 vocabulary로는 유용하다.

그러나 Code Brain에는 이미 SQLite source index, code symbol/call graph, memory tombstone/fold, generation marker가 있다. 두 번째 generic ContextGraph와 TemporalVersionManager를 넣으면 source of truth가 갈라진다.

**채택:**

- `extracted|inferred|unknown` evidence kind와 resolver를 분리한다.
- source generation, indexed/current content hash, span, invalidated 상태를 한 evidence envelope로 묶는다.
- `previous version`과 `derived from`의 의미를 구분하되 기존 memory 저장 owner는 유지한다.

**거부:** generic graph backend, Neo4j/FalkorDB, 두 번째 temporal snapshot owner, heuristic influence를 causal truth로 승격하는 정책.

### 4.3 Pass 2 — retrieval·GraphRAG·chunking

[`context_retriever.py`](https://github.com/semantica-agi/semantica/blob/5c6b40f36ceb8963ec76a9f0113363546b8675e8/semantica/context/context_retriever.py)는 vector/graph/memory 결과를 합치고 graph entity/relationship을 query 시점에 embedding한다. source별 min-max normalization, 고정 weight와 여러 boost를 사용한다. candidate pool에 따라 score가 변하고 calibration/qrels 근거가 없으므로 Code Brain 기본 ranking에 넣지 않는다.

[`vector_store.py`](https://github.com/semantica-agi/semantica/blob/5c6b40f36ceb8963ec76a9f0113363546b8675e8/semantica/vector_store/vector_store.py)는 embedding 실패 시 random vector fallback을 제공한다. 이는 명시적 실패가 아니라 조용한 품질 붕괴이므로 Code Brain에서는 금지한다.

[`methods.py`](https://github.com/semantica-agi/semantica/blob/5c6b40f36ceb8963ec76a9f0113363546b8675e8/semantica/split/methods.py)의 entity-aware chunking은 자연어 NER 경계에는 아이디어가 있지만 exact source span/stable chunk ID/content hash가 약하고 ontology-aware 경로는 entity-aware 위임이다. 코드에는 현재 AST/function chunk가 더 정확하다.

**채택:** scope/allowlist/freshness를 먼저 적용하고 graph context assembly를 lexical ranking과 분리한다. entity/relation enrichment가 필요해도 query hot path가 아니라 deterministic offline index 단계에서만 수행한다.

### 4.4 Pass 3 — provenance·reasoning·보안

Semantica provenance의 entity/activity/agent/lineage/invalidation 개념은 유용하다. 반면 ContextGraph decision API는 `reasoning` 원문을 최대 10,000자까지 node property로 저장하고 vector/retrieval 경로에서도 이를 사용한다. raw reasoning, chain-of-thought, scratchpad, source quote, arbitrary metadata는 secret·PII·내부 추론 누출 surface다.

Code Brain에는 다음 축소 계약만 적용했다.

```text
schema_version
retrieval policy / representation
source generation
relative path + verified line span
extractor origin / evidence kind / confidence / target resolution
fresh|stale|unknown + invalidated
canonical references
receipt_id = sha256(canonical JSON)
```

`context_receipt`에는 query text, query hash, source body, snippet, summary, raw reasoning을 넣지 않는다. 동일 입력·generation·참조 집합은 동일 receipt ID를 만든다. event timestamp를 포함한 audit와 deterministic replay receipt를 분리한다.

### 4.5 Pass 4 — persistence·concurrency·MCP

ContextGraph `save_to_file()`은 최종 JSON을 직접 overwrite하고 `fsync + same-directory temp + os.replace + previous generation` 계약이 없다. load도 기존 graph를 clear한 뒤 순차 삽입하므로 중간 실패 원자성이 약하다. 이 방식은 채택하지 않는다.

provenance SQLite의 WAL, busy timeout, `BEGIN IMMEDIATE`, rollback, operation-scoped connection은 참고할 만하지만 Code Brain의 기존 private/atomic I/O와 generation owner를 대체하지 않는다.

pinned tree에는 `semantica.mcp_server`와 top-level `mcp` 경로가 공존하며 persistence init은 존재하지 않는 `ContextGraph.load()`를 호출한다(`load_from_file()`만 존재). schema에 min/max를 적는 것과 dispatcher에서 실제 강제하는 것도 분리되어 있다. Code Brain은 이미 required/minLength/bounds/repeated rejection stop-order와 safe error envelope를 갖고 있으므로 이를 유지한다.

### 4.6 Pass 5 — tests·benchmark·license

테스트 표면은 넓지만 retrieval 테스트 다수가 mock/wrapper/formula 검증이며 Recall/MRR/nDCG/exact-span 품질 gate가 아니다. README가 참조하는 `benchmarks/benchmarks_runner.py`와 `benchmarks/requirements.txt`는 pinned tree에 없으므로 역사적 성능 수치를 Code Brain 근거로 사용하지 않는다.

MIT license라 패턴 참고 자체는 가능하지만, 라이선스 허용은 architecture 적합성·품질·보안 증명이 아니다. Semantica 코드를 복사하거나 패키지를 설치하지 않았고, 의미 계약만 독자 구현했다.

### 4.7 채택·보류·거부 요약

| 판정 | 항목 |
|---|---|
| 즉시 채택 | typed graph provenance, explicit/inferred 구분, source generation/hash/span/invalidation, canonical no-query receipt |
| 후순위 | bounded rule/causal analysis, temporal validity vocabulary, offline ontology-like validator, decision lineage schema |
| 거부 | Semantica package, ContextGraph monolith, raw reasoning/source quote 저장, random embedding fallback, arbitrary fusion boost, query-time graph embedding, direct JSON overwrite, duplicate MCP surface |

---

## 5. 전세계 연구자료 추가 보완: 적용 우선순위

아래는 관련 자료를 “유명하므로 나열”한 표가 아니라 **Code Brain에서 실제 설계 결정을 바꾸는 정도**로 분류한 것이다. 논문 수치는 각 논문의 데이터·모델·벤치마크 조건에 한정한다.

| 자료 | 핵심 관찰 | Code Brain 적용 |
|---|---|---|
| [TurboQuant](https://arxiv.org/abs/2504.19874) | rotation + scalar quantization으로 online quantization을 노린다 | TurboVec은 optional sidecar로만 도입; float32 recall gate 필수 |
| [Fast-TurboQuant](https://arxiv.org/abs/2606.21448) | 구조화된 projection으로 quantization 비용을 줄이려는 후속 preprint | watchlist; 현재 구현에 복사하지 않고 build-time CPU benchmark 대상으로만 둔다 |
| [Product Quantization](https://hal.science/hal-00514462/document) | subspace codebook으로 저장·검색 비용을 줄인다 | quantizer 선택 시 distortion/recall/bytes를 함께 측정 |
| [Optimized Product Quantization](https://www.cv-foundation.org/openaccess/content_cvpr_2013/papers/Ge_Optimized_Product_Quantization_2013_CVPR_paper.pdf) | space decomposition을 최적화하면 quantization accuracy가 달라진다 | calibration/rotation을 model metadata와 함께 versioning; “bits만 낮추기” 금지 |
| [ScaNN / anisotropic quantization](https://research.google/pubs/accelerating-large-scale-inference-with-anisotropic-vector-quantization/) | MIPS에서는 query 방향 오차가 더 중요할 수 있다 | cosine/IP metric별 별도 eval; L2 reconstruction error만 gate로 쓰지 않음 |
| [HNSW](https://arxiv.org/abs/1603.09320) | 계층 graph traversal로 ANN을 빠르게 만든다 | TurboVec 도입 전 dense baseline 후보; graph memory와 update cost 측정 |
| [FAISS](https://arxiv.org/abs/1702.08734) | PQ와 효율적 k-selection의 대규모 baseline | quantized/full-scan/ANN을 같은 qrels와 hardware에서 비교 |
| [DiskANN](https://www.microsoft.com/en-us/research/?p=634449) | SSD-backed graph가 대규모·고 recall·낮은 latency를 함께 노린다 | corpus가 커질 때만 별도 storage-backed track; 현재 sidecar 설계와 혼합하지 않음 |
| [RRF](https://research.google/pubs/reciprocal-rank-fusion-outperforms-condorcet-and-individual-rank-learning-methods/) | score scale을 모를 때 rank fusion의 강한 단순 baseline | 독립 corpus에만 RRF; 공통 denominator가 있으면 magnitude fusion |
| [LocAgent](https://arxiv.org/abs/2503.09089) | files/classes/functions/imports/invocations/inheritance의 heterogeneous graph로 localization | typed graph와 multi-hop ego context, relation별 filtering 도입 |
| [RepoGraph](https://arxiv.org/abs/2410.14684) | repository-level graph를 기존 SWE agent에 plug-in할 수 있다 | graph를 agent tool surface로 만들되, graph confidence와 source proof를 함께 반환 |
| [CodePlan](https://arxiv.org/abs/2309.12499) | incremental dependency + change may-impact + adaptive plan | `blast_radius`를 “검색 결과”가 아니라 impact manifest/plan seed로 승격 |
| [Agentless](https://arxiv.org/abs/2407.01489) | localization→repair→patch validation의 단순한 단계 분리가 강한 baseline | `context_pack`을 localization 단계와 source verification 단계에 재사용 |
| [RepoCoder](https://arxiv.org/abs/2303.12570) | retrieval→generation을 반복하면 초안이 다음 retrieval query가 된다 | 후속 단계에서만 iterative retrieval; 기본 query는 한 번, 비용 상한 필수 |
| [Repoformer](https://proceedings.mlr.press/v235/wu24a.html) | 불필요한 retrieval은 noisy context와 latency를 만들 수 있다 | retrieval gate/abstention을 first-class 결과로 추가 |
| [SWE-Explore](https://arxiv.org/abs/2606.07297) | file-level hit가 강해도 line-level ranking이 병목이다 | line span qrels, coverage/ranking/efficiency 평가를 별도 gate로 추가 |
| [PPRGo](https://arxiv.org/pdf/2007.01570) | 큰 graph의 PPR은 근사·truncation이 필요하다 | 그래프 상한을 먼저 두고, 규모가 커질 때 residual 기반 PPR을 비교 |

### 5.1 연구자료를 Code Brain 방식으로 해석하는 안전 규칙

- 논문의 “accuracy improvement”는 Code Brain의 `Recall@k`, `MRR`, `nDCG`, line coverage로 재측정한다.
- 서로 다른 embedding model·corpus·hardware 수치를 직접 비교하지 않는다.
- preprint·vendor README·single dataset case study는 아이디어 근거이지 release proof가 아니다.
- 연구에서 좋은 방법도 query cost, update cost, ACL filtering, crash recovery를 통과하지 못하면 Code Brain 기본값이 될 수 없다.

---

## 6. 현재 Code Brain과의 정확한 접점

| 현재 파일 | 이미 있는 계약 | 이번 문서에서의 변경 방향 |
|---|---|---|
| `.ai/runtime/src/ai_core/search.py` | BM25 query, function chunk/span, dirty/hash auto-refresh, `context_pack` budget | v2·graph/PPR·canonical receipt를 기본 활성화; explicit legacy rollback 유지 |
| `.ai/runtime/src/ai_core/graph_context.py` | caller/callee/related symbol, schema v2, full/skeleton/refs-only, 64 KiB, source hash/generation/stale suppression | typed multi-language relation과 resolved target 품질을 후속 확장 |
| `.ai/runtime/src/ai_core/codegraph.py` | Python AST symbol/call, BFS path, reverse blast radius, architecture summary | relation·confidence·scope·generation을 일반화; 기존 Python 결과는 byte-compatible 유지 |
| `.ai/runtime/src/ai_core/autoresearch/dense.py` | ONNX MiniLM, 384-dim float32 blob, 50K-token gate, derived rebuild | float32 baseline을 보존하고 `DenseIndex` adapter 뒤에 TurboVec을 추가 |
| `.ai/runtime/src/ai_core/autoresearch/hybrid.py` | BM25 pool 30 → cosine → dynamic RRF → optional reranker, 실패 시 BM25 | current Stage 1은 유지; Code Brain context route는 allowlist/stable-ID/graph expansion을 별도 추가 |
| `.ai/autoresearch/STAGE1_HYBRID.md` | dense opt-in/no-deps default, dim mismatch degrade | TurboVec flag·manifest·recall gate를 같은 opt-in 원칙으로 문서화 |
| `docs/prd.md` | held-out 평가, schema version, derived index metadata, BM25-first 원칙 | graph/dense generation·line-level eval·cross-file atomicity를 추가 |
| `.ai/evals/cases/*.jsonl` | production code/memory/tool eval과 `line_span_retrieval` qrels | line/span 축은 반영; dense filtered/sync adversarial fixture는 TurboVec gate 전 추가 |

### 6.1 닫힌 gap과 남은 gap

**이번 구현으로 닫힌 것:** 기본 context-pack v2, bounded one-hop PPR, skeleton/refs-only, exact function span 노출, graph source generation/hash trust, stale evidence 억제, typed provenance, canonical no-query receipt, read-path SQLite 무변경 fast path, production line/span eval 및 `make eval` 연결.

**남은 것:**

1. cross-file call target은 아직 lexical resolution이다. owner/type/LSP proof가 없으면 inferred로만 다뤄야 한다.
2. PPR은 bounded one-hop 후보 재정렬에 연결했다. 더 넓은 repo map/다중 relation 확장은 held-out qrels ablation 전에는 연결하지 않는다.
3. repo map/impact manifest와 imports/implements/extends의 일관된 multi-language schema가 남았다.
4. TurboVec용 stable `uint64` ID, float32 shadow baseline, allowlist kernel, calibration/generation manifest는 아직 없고 실제 dense bottleneck도 증명되지 않았다.
5. 현재 line/span production fixture는 최소 회귀 gate다. 세계 수준 주장을 위해 실제 저장소 snapshot의 30~50개 이상 qrels와 5회 latency 반복이 필요하다.

---

## 7. 목표 계약: 바로 구현 가능한 API·자료구조

### 7.1 실제 `context_pack` v2 payload

기본 호출은 `representation=v2` payload를 반환한다. 기존 payload가 필요한 소비자는 `representation=legacy`를 명시한다.

```json
{
  "context_pack_version": 2,
  "representation": "refs-only",
  "results": [
    {
      "path": "src/a.py",
      "chunk_path": "src/a.py:pkg.fn",
      "start_line": 10,
      "end_line": 31,
      "qualname": "pkg.fn",
      "kind": "function",
      "reason": "lexical_symbol_chunk",
      "context_rank": 1
    }
  ],
  "lexical_refs": [],
  "graph_context": {
    "schema_version": 2,
    "ranking_policy": "bounded-personalized-pagerank-over-one-hop",
    "ranking_applied": true,
    "ranking_parameters": {"max_hops": 2, "max_nodes": 2048, "max_edges": 1600, "iterations": 25},
    "representation": "refs-only",
    "source_generation": "...",
    "results": [
      {"path": "src/a.py", "span": {"start_line": 10, "end_line": 31}, "reason": "seed"}
    ]
  },
  "retrieval_trace": {
    "lexical_policy": "bm25",
    "fusion": "bounded_context_append",
    "ranking_mutated": false,
    "lexical_ranking_mutated": false,
    "graph_ranking_applied": true,
    "dense_rerank": false
  },
  "context_receipt": {
    "schema_version": 1,
    "context_pack_version": 2,
    "representation": "refs-only",
    "graph": {"source_generation": "...", "status": "used"},
    "references": [],
    "receipt_id": "sha256:..."
  },
  "context_budget": {
    "max_bytes": 65536,
    "bytes": 0,
    "truncated": false,
    "graph_truncated": false
  }
}
```

규칙:

- `v2`가 default이며 기존 lexical results/snippet을 유지하고 graph context만 bounded append한다.
- `legacy`는 새 graph/receipt 필드가 없는 명시적 롤백 경로다.
- `skeleton`/`refs-only`는 lexical source body를 path/span 중심으로 바꾼다.
- graph가 실패해도 lexical 결과는 유지하며 ranking을 변경하지 않는다.
- stale graph source의 snippet·summary는 억제한다.
- receipt는 query/본문/요약/CoT를 저장하지 않고 canonical policy/generation/reference만 해시한다.
- absolute/traversal/colon path와 무효 span은 reference에서 제외한다.

### 7.2 dense adapter 계약

TurboVec을 직접 `search.py`에 import하지 말고 아래 작은 인터페이스로 감싼다.

```python
class DenseIndex(Protocol):
    def build(self, rows: Iterable[DenseRow], manifest: DenseManifest) -> None: ...
    def upsert(self, rows: Iterable[DenseRow]) -> None: ...
    def remove(self, ids: Iterable[int]) -> None: ...
    def prepare(self) -> None: ...
    def search(self, query: Sequence[float], *, k: int,
               allow_ids: Collection[int] | None = None) -> list[DenseHit]: ...
    def sync(self, *, durable: bool = True) -> None: ...
    def load(self, manifest: DenseManifest) -> None: ...
```

`DenseManifest` 최소 필드:

```text
schema_version
repo_id / scope
source_generation / corpus_hash
index_generation
embedding_model / model_hash
dimension / dtype / metric / normalization
quantizer / bit_width / calibration_hash
ids_hash / row_count
format_version
```

불일치 시 dense를 억지로 사용하지 않고 `dense_status=disabled_manifest_mismatch`와 함께 BM25/graph로 degrade한다.

### 7.3 stable ID 규약

```text
logical_key = repo_rel_path + "::" + qualname + "::" + segment_ordinal
content_hash = hash(normalized source span)
```

- SQLite가 `logical_key → uint64 dense_id`를 authoritative하게 보관한다.
- source span 내용이 바뀌어도 같은 logical node면 ID를 유지하고 vector를 replace한다.
- symbol split/merge·path rename처럼 identity가 바뀌면 새 ID를 만들고 old ID를 tombstone한다.
- `dense_id`는 slot 번호·row offset·absolute path가 아니다.
- `ids_hash`는 sidecar와 SQLite가 같은 ID 집합인지 검증하는 데 쓴다.

### 7.4 hybrid query 순서

초기 tuning 가설은 config로 바꾸며, 아래 값은 release 상수가 아니다.

```text
lexical_pool = 256
graph_seed_count = 16
graph_hops = 1
dense_pool = 128
final_context_count = 20
PPR alpha = 0.25
```

실행 순서:

1. query를 길이·control character·scope/ACL 기준으로 검증한다.
2. source/index freshness를 probe한다. stale이면 cheap refresh, 실패하면 stale flag를 단다.
3. Repoformer식 gate로 “외부 context가 이득 없는 질의”를 먼저 abstain할 수 있게 한다. 초기에는 deterministic symbol/path/short-query heuristic으로 시작한다.
4. 항상 가능한 BM25/FTS5로 lexical seed를 만든다.
5. seed symbol/path에서 typed graph를 1-hop 확장한다. `contains`·external edge는 기본 walk에서 제외한다.
6. scope/ACL/changed-file allowlist를 먼저 만들고 dense index에 전달한다.
7. float32 또는 TurboVec dense rerank를 **allowlist 내부**에서만 수행한다.
8. 같은 corpus에서 score가 비교 가능하면 lexical+graph+dense magnitude fusion을 사용한다. 독립 corpus일 때만 RRF를 적용한다.
9. exact source span을 읽고 tests/config/docs 관계를 별도 표시한다.
10. byte/token budget을 적용하고, top hit margin이 낮거나 source proof가 없으면 `abstention`을 반환한다.

---

## 8. 구현 순서와 파일별 작업 명세

### Phase 0 — 계약·관측만 추가

목표: 먼저 측정 가능한 구조를 만든 뒤, bounded PPR를 기본 활성화한다.

| 대상 | 작업 | 상태 |
|---|---|---|
| `search.py` | v2 기본, legacy rollback, trace·receipt·exact function span | **완료** |
| `graph_context.py` | schema/provenance/hash/generation/stale/full·skeleton·refs-only | **완료** |
| `.ai/evals/` | production query→qrels→path/span 평가 | **완료**, 최소 fixture 3개 |
| config/API | dependency·network 없이 v2 기본 + explicit legacy rollback | **완료** |

### Phase 1 — Graft형 graph context v1

| 대상 | 작업 | 상태 |
|---|---|---|
| `codegraph.py` | relation/confidence/source span/generation을 DB schema로 일반화 | **후속**; 현재 output envelope에서 우선 제공 |
| `graph_context.py` | skeleton/refs-only, bounded context | **완료** |
| `graph_context.py` | ego graph/repo map | **후속** |
| `graph_rank.py` | deterministic bounded PPR | **완료·기본 연결**, bounded one-hop 후보에만 적용 |
| `search.py` | graph context를 lexical ranking 비변경으로 결합 | **완료** |
| CLI/MCP | `context_pack` representation enum·dispatch | **완료** |

### Phase 2 — typed graph와 impact manifest

1. Python 외 언어는 parser가 실제로 지원하는 범위부터 추가한다.
2. imports/implements/extends를 `is_external`과 함께 저장한다.
3. `blast_radius` 출력에 `changed_paths`, `changed_symbols`, `distance`, `relation`, `confidence`, `source_span`을 추가한다.
4. CodePlan식으로 다음 edit candidate를 `impact_manifest`로 저장하되, 자동 수정은 수행하지 않는다.

```json
{
  "schema_version": 1,
  "changed_paths": ["src/a.py"],
  "changed_symbols": ["pkg.a.fn"],
  "impacted": [
    {"symbol": "pkg.b.call", "distance": 1, "relation": "calls", "confidence": "extracted"}
  ],
  "requires_review": ["ambiguous cross-file edge"]
}
```

### Phase 3 — TurboVec adapter, shadow mode

1. 현재 float32 dense 결과를 ground truth candidate ranking으로 고정한다.
2. `DenseManifest`, stable ID table, generation protocol을 먼저 구현한다.
3. TurboVec backend는 optional import로 두고 unavailable/mismatch/error 시 즉시 float32 또는 BM25로 내려간다.
4. shadow mode에서는 결과에 영향을 주지 않고 `recall@k`, `latency`, `bytes`, `filtered_exactness`만 기록한다.
5. calibration/bit width별로 5회 반복해 median과 분산을 남긴다.

### Phase 4 — filtered hybrid canary

enable 조건을 모두 만족해야 한다.

- manifest source/model/dim/normalization/generation 일치
- quantized recall gate 통과
- allowlist 결과가 허용 ID 밖으로 나가지 않음
- torn write/corrupt file/unknown ID 테스트 통과
- p95가 현재 float32/BM25보다 악화되지 않거나, memory 절감 이득이 명시됨
- default-off rollback이 한 환경변수로 가능

### Phase 5 — 규모가 커진 뒤에만 ANN 비교

corpus가 실제로 커져 full-scan dense가 병목이 될 때 HNSW/FAISS/DiskANN/ScaNN을 같은 adapter contract에 넣어 비교한다. 그 전에는 의존성과 운영 surface를 늘리지 않는다.

---

## 9. 검증 계획과 실행 증거: “좋아 보임”을 release proof로 착각하지 않기

### 9.1 검색 품질

최소 지표:

```text
Recall@1/@5/@20
MRR@20
nDCG@20
file coverage
line/span coverage
rank-weighted line coverage
precision / over-inclusion
abstention precision
```

SWE-Explore의 방향대로 file-level과 line-level을 분리한다. 성공 trajectory에서 실제 읽힌 line range를 qrels로 만들고, 고정 line/token budget에서 평가한다.

### 9.2 운영·성능

```text
p50/p95 query latency
index build latency
incremental update latency
first-query warmup latency
sidecar bytes/vector
RSS during build/search
crash recovery time
stale fallback rate
BM25/graph/dense/RRF contribution ratio
```

### 9.3 필수 regression matrix

| 영역 | 테스트 |
|---|---|
| graph determinism | cold build와 incremental build의 graph/manifest byte identity |
| freshness | 같은 size·mtime edit, extractor stamp change, uncommitted file, deleted file |
| concurrency | 두 query가 동시에 stale를 볼 때 rebuild 한 번만 수행; lock 후 re-probe |
| failure | failed rebuild 후 old snapshot 유지; query path가 LLM/network를 호출하지 않음 |
| graph trust | ambiguous edge drop, external edge exclusion, confidence 보존, exact span 재확인 |
| fusion | single-scope parity, weak-scope gate, shared denominator, independent-scope RRF |
| dense shape | ndim/dtype/dim/NaN/Inf/contiguous/k/unknown ID reject |
| dense correctness | float32 대비 quantized recall, deletes/replacements, stable ID/slot 이동 |
| filtering | allowlist 밖 결과 0건, 결과 수 `min(k, allowed)` 정확, stale allowlist 처리 |
| persistence | durable rename, torn header/block, checksum/digest, huge declared count memory guard |
| threading | reader/writer churn, fork/spawn 환경, no deadlock, no GIL-held blocking |
| rollout | graph는 explicit legacy payload parity, dense는 off 시 기존 behavior, rollback 경로 고정 |

### 9.4 출하 gate 제안

아래는 첫 gate의 **시작값**이며, 실제 Code Brain held-out 결과로 조정한다.

1. explicit legacy에서 기존 targeted payload가 유지되고, default/explicit v2가 동일하다.
2. graph mode가 BM25-only 대비 line coverage를 낮추지 않고, token budget을 초과하지 않는다.
3. TurboVec `Recall@20`이 float32 baseline 대비 절대 2 percentage point 이상 떨어지지 않는다. 떨어지면 enable하지 않는다.
4. filtered search는 모든 fixture에서 허용 ID 밖 결과 0건, padding 0건이다.
5. sidecar corruption/truncation/crash recovery fixture가 old-generation fallback으로 종료된다.
6. warm-up 뒤 기본 `context_pack` 50회 반복에서 파일 목록·크기·SHA-256이 변하지 않는다.
6. 5회 반복의 median뿐 아니라 최악 run의 p95와 실패율도 기록한다.
7. vendor/paper 수치는 위 gate를 대신할 수 없다.

### 9.5 2026-08-22 실행 증거

| 검증 | 결과 | 해석 |
|---|---:|---|
| context/graph/search/MCP 집중 회귀 | `179 passed` | v2 representation, receipt, graph trust, function span, legacy 경로 통과 |
| 전체 runtime 회귀 (`test_cli.py` 제외) | `1869 passed, 5 skipped` | 기존 strict-doctor fixture 실패군을 분리한 나머지 전 범위 통과 |
| repo eval 계약 | `9 passed` | 일곱 축 lockstep와 `31/31` strict-complete 기대값 통과 |
| `make eval` | `31/31 passed` | `line_span_retrieval`을 포함한 일곱 production axis 통과 |
| line/span production axis 5회 | 매회 `3/3`, normalized SHA-256 동일 | latency를 제외한 관측 결과가 다섯 번 byte-equivalent |
| `make lint` | 통과 | Python/static repository lint 통과 |
| 실제 CLI smoke | `ok=true`, v2/refs-only/graph schema 2/receipt schema 1 | receipt에는 raw query가 없고 `sha256:` receipt ID가 유효 |

전체 `make test` snapshot은 변경 관련 lockstep 테스트를 고치기 전 `2026 passed, 5 skipped, 19 failed`였다. 그중 이 wave가 추가한 eval-axis 불일치 한 건은 수정 후 `test_repo_evals.py` `9 passed`로 닫았다. 나머지 18건은 기존 `test_cli.py`의 global-kit 복제 fixture 누락 또는 managed install drift를 거쳐 strict-doctor 계열에서 실패한다. 현재 working tree의 strict doctor도 별도로 `.codex/AGENTS.md` managed-rule drift와 기존 audit-chain `prev_sha_mismatch` 때문에 실패한다. 이 둘은 retrieval 구현 실패가 아니며, 사용자 설정·감사 원장을 임의로 덮어쓰거나 repair하지 않았다.

따라서 이 문서가 증명하는 것은 **새 retrieval/context 경로의 집중 회귀와 production eval 통과**까지다. 저장소 전체 release-ready 또는 외부 세계 1위는 아직 증명하지 않는다.

---

## 10. 운영·보안·롤백 계약

### 10.1 제안 flag

실제 이름은 기존 config naming과 맞춰 구현하되 의미는 아래처럼 고정한다.

```text
CODEBRAIN_CONTEXT_DENSE=off|float32|turbovec
CODEBRAIN_CONTEXT_DENSE_ALLOWLIST=1
CODEBRAIN_CONTEXT_MODE=full|skeleton|refs-only
CODEBRAIN_CONTEXT_SHADOW=0|1
```

graph/PPR의 실제 기본값은 `context_pack` v2다. `representation=legacy`가 즉시 롤백 경로다. TurboVec은 계속 default-off이며 shadow는 결과를 바꾸지 않는다.

### 10.2 신뢰 경계

- source query와 graph refresh는 local/no-network.
- LLM summary는 별도 offline worker이며, query timeout과 독립이다.
- vector model은 사전 캐시된 artifact만 사용한다. query 중 다운로드·설치하지 않는다.
- telemetry/audit에는 path, raw query, code body, repo name, secret을 넣지 않는다.
- graph summary와 dense vector는 source의 derived artifact로 분류하고, source generation mismatch 시 사용하지 않는다.

### 10.3 rollback

1. `CODEBRAIN_CONTEXT_DENSE=off`로 TurboVec을 즉시 끈다.
2. 호출별 `representation=legacy`로 graph/PPR를 즉시 끈다.
3. SQLite source/FTS/codegraph는 유지한다.
4. sidecar는 active generation flip 없이 orphan으로 남기고 maintenance에서만 삭제한다.
5. rollback 후에도 `retrieval_policy`, `freshness`, `dense_status`, `graph_status`를 audit에 남겨 원인을 추적한다.

---

## 11. 구현자가 바로 실행할 체크리스트

### 시작 전

- [x] `git status`로 기존 dirty work를 보존했다.
- [x] 현재 `develop`/remote divergence를 동기화·rebase하지 않고 작업 범위를 분리했다.
- [x] source/FTS/codegraph owner를 기존 SQLite/index generation으로 유지했다.
- [ ] 실제 저장소 snapshot 기반 30~50개 held-out query/path/line qrels를 고정한다.

### 첫 구현 wave

- [x] `context_pack` v2 기본과 backward-compatible explicit legacy rollback
- [x] graph evidence kind/confidence/generation/hash/invalidation
- [x] skeleton/refs-only와 bounded graph append
- [x] bounded deterministic PPR 엔진
- [x] exact function span retrieval과 source revalidation
- [x] query·body·CoT 없는 canonical context receipt
- [x] production line/span eval을 `make eval`에 연결
- [x] bounded one-hop ego 후보 PPR 연결과 결정론/무증가 회귀
- [ ] repo map과 multi-relation PPR ablation fixture 확대

### 두 번째 구현 wave

- [ ] `DenseManifest`와 stable `uint64` mapping
- [ ] float32 adapter가 먼저 통과
- [ ] TurboVec adapter optional import
- [ ] allowlist-in-kernel와 exact result count
- [ ] durable sidecar + SQLite generation flip
- [ ] corrupt/torn-write/concurrency/fork tests

### enable 전

- [x] default/explicit v2 parity와 explicit legacy rollback targeted test
- [ ] 대규모 held-out graph ablation
- [ ] float32 vs TurboVec recall
- [x] production line/span 결과 5회 반복 동일성
- [ ] 전체 held-out graph/dense 5회 p50/p95/IQR
- [x] 최소 production line-level qrels 통과
- [ ] rollback flag 실증
- [ ] benchmark 주장과 Code Brain 측정치를 별도 표기

---

## 12. 근거 파일과 재검토 링크

### Graft pinned source

- [graph types](https://github.com/NanoNets/Graft/blob/65a76e5edd4098e0c7f4749d1e87f15ed741d069/src/graph/types.ts)
- [incremental graph build](https://github.com/NanoNets/Graft/blob/65a76e5edd4098e0c7f4749d1e87f15ed741d069/src/graph/build.ts)
- [refresh/lock/re-probe](https://github.com/NanoNets/Graft/blob/65a76e5edd4098e0c7f4749d1e87f15ed741d069/src/graph/refresh.ts)
- [fingerprint/drift](https://github.com/NanoNets/Graft/blob/65a76e5edd4098e0c7f4749d1e87f15ed741d069/src/graph/fingerprint.ts)
- [tree-sitter extraction](https://github.com/NanoNets/Graft/blob/65a76e5edd4098e0c7f4749d1e87f15ed741d069/src/graph/extract.ts)
- [conservative edge resolution](https://github.com/NanoNets/Graft/blob/65a76e5edd4098e0c7f4749d1e87f15ed741d069/src/graph/resolve.ts)
- [summary/crux checkpoint](https://github.com/NanoNets/Graft/blob/65a76e5edd4098e0c7f4749d1e87f15ed741d069/src/graph/enrich.ts)
- [repo map](https://github.com/NanoNets/Graft/blob/65a76e5edd4098e0c7f4749d1e87f15ed741d069/src/graph/map.ts)
- [personalized PageRank](https://github.com/NanoNets/Graft/blob/65a76e5edd4098e0c7f4749d1e87f15ed741d069/src/ask/graphrank.ts)
- [scope-aware fusion](https://github.com/NanoNets/Graft/blob/65a76e5edd4098e0c7f4749d1e87f15ed741d069/src/ask/fuse.ts)
- [MCP tool surface](https://github.com/NanoNets/Graft/blob/65a76e5edd4098e0c7f4749d1e87f15ed741d069/src/mcp/tools.ts)
- [Graft test suite](https://github.com/NanoNets/Graft/tree/65a76e5edd4098e0c7f4749d1e87f15ed741d069/test)

### TurboVec pinned source

- [API and persistence contract](https://github.com/RyanCodrai/turbovec/blob/ccab9f325e6ce2a270a87daf01ae4e443bcf2d49/docs/api.md)
- [Rust core](https://github.com/RyanCodrai/turbovec/blob/ccab9f325e6ce2a270a87daf01ae4e443bcf2d49/turbovec/src/lib.rs)
- [stable-ID wrapper](https://github.com/RyanCodrai/turbovec/blob/ccab9f325e6ce2a270a87daf01ae4e443bcf2d49/turbovec/src/id_map.rs)
- [atomic durable I/O](https://github.com/RyanCodrai/turbovec/blob/ccab9f325e6ce2a270a87daf01ae4e443bcf2d49/turbovec/src/io.rs)
- [v7 sync container](https://github.com/RyanCodrai/turbovec/blob/ccab9f325e6ce2a270a87daf01ae4e443bcf2d49/turbovec/src/io_v7.rs)
- [Python binding/thread/fork boundary](https://github.com/RyanCodrai/turbovec/blob/ccab9f325e6ce2a270a87daf01ae4e443bcf2d49/turbovec-python/src/lib.rs)
- [release changelog and adversarial coverage](https://github.com/RyanCodrai/turbovec/blob/ccab9f325e6ce2a270a87daf01ae4e443bcf2d49/CHANGELOG.md)

### Semantica pinned source

- [package/dependencies](https://github.com/semantica-agi/semantica/blob/5c6b40f36ceb8963ec76a9f0113363546b8675e8/pyproject.toml)
- [ContextGraph/decision/persistence](https://github.com/semantica-agi/semantica/blob/5c6b40f36ceb8963ec76a9f0113363546b8675e8/semantica/context/context_graph.py)
- [context retrieval/fusion/reasoning](https://github.com/semantica-agi/semantica/blob/5c6b40f36ceb8963ec76a9f0113363546b8675e8/semantica/context/context_retriever.py)
- [provenance schemas](https://github.com/semantica-agi/semantica/blob/5c6b40f36ceb8963ec76a9f0113363546b8675e8/semantica/provenance/schemas.py)
- [provenance SQLite storage](https://github.com/semantica-agi/semantica/blob/5c6b40f36ceb8963ec76a9f0113363546b8675e8/semantica/provenance/storage.py)
- [vector fallback behavior](https://github.com/semantica-agi/semantica/blob/5c6b40f36ceb8963ec76a9f0113363546b8675e8/semantica/vector_store/vector_store.py)
- [chunking implementations](https://github.com/semantica-agi/semantica/blob/5c6b40f36ceb8963ec76a9f0113363546b8675e8/semantica/split/methods.py)
- [MCP package server](https://github.com/semantica-agi/semantica/blob/5c6b40f36ceb8963ec76a9f0113363546b8675e8/semantica/mcp_server/__init__.py)
- [top-level MCP session](https://github.com/semantica-agi/semantica/blob/5c6b40f36ceb8963ec76a9f0113363546b8675e8/mcp/session.py)
- [MIT license](https://github.com/semantica-agi/semantica/blob/5c6b40f36ceb8963ec76a9f0113363546b8675e8/LICENSE)

### 현재 Code Brain source

- [`search.py`](../.ai/runtime/src/ai_core/search.py)
- [`graph_context.py`](../.ai/runtime/src/ai_core/graph_context.py)
- [`graph_rank.py`](../.ai/runtime/src/ai_core/graph_rank.py)
- [`line_span_eval.py`](../.ai/evals/line_span_eval.py)
- [`test_context_pack_v2.py`](../.ai/runtime/tests/test_context_pack_v2.py)
- [`test_line_span_eval.py`](../.ai/runtime/tests/test_line_span_eval.py)
- [`codegraph.py`](../.ai/runtime/src/ai_core/codegraph.py)
- [`dense.py`](../.ai/runtime/src/ai_core/autoresearch/dense.py)
- [`hybrid.py`](../.ai/runtime/src/ai_core/autoresearch/hybrid.py)
- [`STAGE1_HYBRID.md`](../.ai/autoresearch/STAGE1_HYBRID.md)
- [`docs/prd.md`](prd.md)

---

## 13. 최종 판정

**Graft는 graph/context/freshness 설계를 보강하는 직접 채택 대상이고, Semantica는 typed provenance·canonical receipt 의미만 선택 채택한다. TurboVec은 병목·stable-ID·float32 shadow·Recall/p95 gate가 증명될 때만 optional filtered dense sidecar로 검토한다.**

구현 순서는 반드시 다음이어야 한다.

```text
typed graph + exact spans + canonical receipt
  → production line-level eval
  → ego graph/repo map/PPR ablation
  → float32 dense adapter
  → TurboVec shadow/recall gate
  → filtered hybrid canary
  → 규모가 커진 경우에만 HNSW/FAISS/DiskANN/ScaNN 비교
```

이 순서를 지키면 Code Brain의 현재 BM25·graph·안전 기본값을 보존하면서, 세 프로젝트와 세계 연구자료에서 검증된 부분만 작은 단계로 흡수할 수 있다.

**외부 수치의 최종 취급:** Graft/TurboVec/Semantica README, 논문, case study의 성능 수치는 방향성·실험 설계 참고로만 사용한다. Code Brain release proof는 이 문서의 held-out qrels, 5회 반복, line-level coverage, filtered correctness, durability/concurrency 테스트로 다시 만들어야 한다.
