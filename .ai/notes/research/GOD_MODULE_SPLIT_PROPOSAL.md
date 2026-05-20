# Code Brain God Module 분할 설계 제안서

**작성 일자**: 2026-05-20  
**대상**: cli.py (1042줄), hooks.py (1683줄), recommend.py (1258줄), search.py (1191줄)  
**총 4174줄**, 현황 분석 기준

---

## 1. 현황: 함수 인벤토리

### 1.1 cli.py (1042줄)

| 함수명 | 라인 | 책임 | 영향 범위 |
|--------|------|------|----------|
| `build_parser()` | 25–432 | 모든 subcommand 파서 정의 | **높음** (진입점) |
| `emit()` | 434–441 | JSON/텍스트 출력 | **낮음** (순수) |
| `main()` | 444–1042 | 명령 디스패치 (if/elif 100+개) | **매우 높음** (핫패스) |

**핵심 issue**: `main()`이 모든 subcommand 핸들러를 직접 포함.

**subcommand 도메인**:
- `version`, `config show` (상태 조회)
- `render` (manifest 생성)
- `doctor` (헬스 체크)
- `worker health/status/stop` (워커 IPC)
- `queue enqueue/lease/complete/fail/recover-expired/archive-dead/dead/status` (스케줄러)
- `trust init/list/revoke` (머신 신뢰)
- `secrets status` (시크릿 저장소)
- `inbox request/list/approve/reject` (승인 루프)
- `notify enqueue` (알림)
- `obs log/metrics/search/usage/slo/health-summary` (옵저빌러티)
- `diagnostics bundle/prune` (진단)
- `migrate` (업그레이드)
- `upgrade plan/apply/rollback/clean-cache` (버전)
- `hook <name>` (이벤트 핸들러 진입)
- `memory append-event/decision/todo/session/tier/...` (메모리)
- `search rebuild/query/context-pack` (검색)

**호출 관계** (cli.py → 내부):
```
main() →
  ├── read_payload() [hooks.py]
  ├── handle_hook() [hooks.py]
  ├── render(), run_checks(), load_config() [다른 모듈]
  ├── search.rebuild(), search.query(), search.context_pack()
  └── 다양한 도메인 함수 (worker, queue, inbox, trust, memory, obs, ...)
```

---

### 1.2 hooks.py (1683줄)

| 함수명 | 라인 범위 | 책임 | 영향 범위 |
|--------|---------|------|----------|
| 상수 & 환경 설정 | 1–100 | HOT_PATH_TARGET_MS, INJECTION_HOOKS, MAX_INJECTION_BYTES | 낮음 |
| `_env_enabled()` | 49–50 | 환경변수 파싱 | 낮음 |
| `_env_disabled()` | 53–54 | 환경변수 파싱 | 낮음 |
| `_injection_marker_path()` | 57–58 | 캐시 경로 | 낮음 |
| `_max_injection_bytes_for()` | 61–64 | Hook별 주입 크기 제한 | 낮음 |
| `_target_ms_for()` | 67–70 | Hook별 타겟 ms | 낮음 |
| `_maybe_apply_delta()` | 73–99 | UserPromptSubmit 중복 제거 | **중간** |
| `_spawn_background_rebuild()` | 102–135 | 백그라운드 인덱스 재구축 | **중간** |
| `_spawn_sleep_time_jobs()` | 137–240 | Stop hook idle 시간 계산 | **중간** |
| `_recently_surfaced_ids()` | 241–287 | 최근 추천 쿨다운 | **중간** |
| `_cooldown_score()` | 289–306 | Ebbinghaus 감소 함수 | **낮음** (순수) |
| `_cooldown_weights()` | 309–380 | 다중 cooldown 가중치 | **낮음** (순수) |
| `_adaptive_half_life()` | 383–427 | 사용자 만족도 기반 반감기 | **중간** |
| `_candidate_raw_strength()` | 429–449 | 후보 강도 점수 | **낮음** (순수) |
| `_importance_from_strength()` | 451–464 | 강도→중요도 변환 | **낮음** (순수) |
| `_candidate_summary_line()` | 466–481 | 후보 요약 라인 생성 | **낮음** (순수) |
| `_adaptive_min_signal_from_satisfaction()` | 483–523 | 만족도 기반 signal 조정 | **중간** |
| `_is_compact_mode()` | 525–526 | 환경 플래그 | **낮음** |
| `_compact_section_line()` | 529–539 | 컴팩트 모드 라인 | **낮음** (순수) |
| `_recommendation_section()` | 541–640 | **추천 렌더링 (핫패스)** | **매우 높음** |
| `_cand_importance()` [nested] | 593–604 | 후보 중요도 계산 | **중간** |
| `_audit_dependency_paths()` | 646–662 | 감사 경로 수집 | **낮음** |
| `_codex_global_memory_path()` | 664–666 | Codex 글로벌 경로 | **낮음** |
| `_recommend_memory_deps()` | 668–683 | 추천 메모리 의존성 | **중간** |
| `_cached_recommend_invoke()` | 686–725 | 추천 캐싱 래퍼 | **높음** |
| `_skill_recommendation_context()` | 729–763 | 슬래시 명령 추천 컨텍스트 | **높음** |
| `.invoke()` [nested] | 733–750 | 슬래시 추천 계산 | **높음** |
| `_try_autonomous_accept()` | 766–852 | 자동 승인 로직 | **중간** |
| `_agent_recommendation_context()` | 854–884 | sub-agent 추천 | **높음** |
| `.invoke()` [nested] | 856–871 | agent 추천 계산 | **높음** |
| `_precall_recommendation_context()` | 887–918 | precall 룰 추천 | **높음** |
| `.invoke()` [nested] | 889–905 | precall 추천 계산 | **높음** |
| `_federated_summary_context()` | 921–959 | 다중 프로젝트 컨텍스트 | **중간** |
| `_satisfaction_summary_context()` | 960–1034 | 만족도 요약 | **중간** |
| `_compact_meta_line()` | 1036–1116 | 컴팩트 메타 라인 | **낮음** (순수) |
| `read_payload()` | 1119–1123 | JSON 페이로드 파싱 | **낮음** |
| `handle_hook()` | 1126–1337 | **Hook 디스패치 (핫패스)** | **매우 높음** |
| `codex_wire_output()` | 1349–1397 | Codex 출력 변환 | **낮음** |
| `_handle_lifecycle_event()` | 1399–1543 | 라이프사이클 이벤트 | **높음** |
| `build_context()` | 1546–1683 | **Hook 컨텍스트 빌드 (핫패스)** | **매우 높음** |

**핵심 issue**: 
- `handle_hook()`이 모든 hook 유형 처리 (SessionStart, PreToolUse, Stop, SubagentStop, PostToolUse, UserPromptSubmit)
- `build_context()`이 모든 hook별 컨텍스트 생성 (6개 hook × 3–4개 추천 섹션)
- 추천 렌더링, cooldown, 자동 승인이 섞여있음

**Hook 이벤트별 책임**:

| Hook | 함수 | 라인 | 책임 |
|------|------|------|------|
| SessionStart | `build_context()`, `_skill_recommendation_context()` | 1546+, 729+ | 세션 시작 메모리 주입 |
| UserPromptSubmit | `_maybe_apply_delta()`, `build_context()` | 73+, 1546+ | Delta 제거, 컨텍스트 |
| PreToolUse | `handle_hook()` 일부 | 1137–1174 | Precall 평가 |
| PostToolUse | (없음) | — | 관찰 전용 |
| Stop | `_spawn_sleep_time_jobs()`, `_spawn_background_rebuild()`, `handle_hook()` | 137+, 102+, 1213+ | 인덱스, idle 시간 |
| SubagentStop | `handle_hook()` | 1213+ | 백그라운드 작업 |

---

### 1.3 recommend.py (1258줄)

| 함수명 | 라인 | 책임 | 영향 범위 |
|--------|------|------|----------|
| 데이터클래스 | 60–92 | Signals, Candidate, CatalogEntry | 기초 |
| `catalog_path()` | 108–110 | 카탈로그 경로 | 낮음 |
| `_slugify()` | 112–117 | 슬러그 생성 | 낮음 |
| `_sha256()` | 119–121 | 해시 | 낮음 |
| `_candidate_id()` | 123–125 | 후보 ID | 낮음 |
| `_danger_match()` | 127–133 | 위험 패턴 필터 | 낮음 |
| `_hyphen_encode_path()` | 135–137 | 경로 인코딩 | 낮음 |
| `_claude_global_dir()` | 139–141 | Claude 글로벌 | 낮음 |
| `_codex_memories_path()` | 143–147 | Codex 메모리 | 낮음 |
| `gather_signals()` | 149–184 | **신호 수집 (핫패스)** | **높음** |
| `_bash_head_cache_path()` | 186–188 | Bash 캐시 경로 | 낮음 |
| `_compute_bash_heads()` | 190–214 | Bash 명령 집계 | **중간** |
| `_write_bash_head_cache()` | 216–226 | Bash 캐시 쓰기 | 낮음 |
| `_spawn_bash_head_cache_rebuild()` | 228–269 | Bash 캐시 재구축 | **중간** |
| `_gather_bash_heads()` | 271–290 | Bash 헤드 수집 | **중간** |
| `_gather_claude_global()` | 292–306 | Claude 글로벌 메모리 | **중간** |
| `_gather_codex_global()` | 308–342 | Codex 글로벌 메모리 | **중간** |
| `cluster_candidates()` | 344–370 | **후보 클러스터링 (핫패스)** | **높음** |
| `_signal_strength()` | 372–383 | 신호 강도 계산 | 낮음 |
| `_signal_kind()` | 385–391 | 신호 종류 분류 | 낮음 |
| `_per_signal_max()` | 393–404 | 신호별 최대값 | 낮음 |
| `_normalized_strength()` | 406–415 | 정규화된 강도 | 낮음 |
| `_evidence_snippets()` | 417–427 | 증거 스니펫 | 낮음 |
| `_candidates_from_decision_tags()` | 429–456 | 결정 태그 추천 | **중간** |
| `_candidates_from_todo_tokens()` | 458–488 | Todo 토큰 추천 | **중간** |
| `_is_meaningful_bigram()` | 490–499 | 의미있는 2-gram | 낮음 |
| `_candidates_from_audit_actions()` | 501–520 | 감사 동작 추천 | **중간** |
| `_candidates_from_codex_groups()` | 522–554 | Codex 그룹 추천 | **중간** |
| `_candidates_from_codex_keywords()` | 556–587 | Codex 키워드 추천 | **중간** |
| `_candidates_from_bash_heads()` | 589–606 | Bash 명령 추천 | **중간** |
| `_draft_body_for_bash_head()` | 608–617 | Bash 바디 드래프트 | 낮음 |
| `_adaptive_min_signal()` | 619–636 | 만족도 기반 signal 조정 | **중간** |
| `_adaptive_min_signal_lower()` | 638–682 | Signal 하향 조정 | **중간** |
| `compact_skill_catalog()` | 684–780 | **카탈로그 압축 (IO)** | **높음** |
| `_is_path_like_task_group()` | 782–795 | 경로 유사 필터 | 낮음 |
| `_draft_body_*()` (6개) | 797–845 | 바디 드래프트 | 낮음 (순수) |
| `list_catalog()` | 847–876 | **카탈로그 읽기 (IO)** | **높음** |
| `_persist_entry()` | 878–1071 | **엔트리 쓰기 (IO)** | **높음** |
| `recommend()` | 1011–1071 | **추천 메인 (핫패스)** | **매우 높음** |

**핵심 issue**:
- `gather_signals()` + `cluster_candidates()` + `recommend()`이 중추
- `_candidates_from_*()` (6개 함수)가 신호 소스별 분산
- Catalog IO와 clustering이 섞여있음

**책임별 그룹**:
1. **신호 수집** (Signals): gather_signals, _gather_bash_heads, _gather_claude_global, _gather_codex_global
2. **후보 생성** (Candidate extraction): _candidates_from_decision_tags, _candidates_from_todo_tokens, _candidates_from_audit_actions, _candidates_from_codex_groups, _candidates_from_codex_keywords, _candidates_from_bash_heads
3. **클러스터링 & 점수** (Scoring): cluster_candidates, _signal_strength, _signal_kind, _per_signal_max, _normalized_strength, _adaptive_min_signal
4. **카탈로그 IO** (Persistence): catalog_path, list_catalog, _persist_entry, compact_skill_catalog
5. **추천 메인 루프**: recommend (위 4가지 조합)

---

### 1.4 search.py (1191줄)

| 함수명 | 라인 | 책임 | 영향 범위 |
|--------|------|------|----------|
| 상수 | 15–100 | SCHEMA_VERSION, SKIP_PATH_PREFIXES, TEXT_SUFFIXES, SKIP_* | 기초 |
| `db_path()` | 103–105 | DB 경로 | 낮음 |
| `connect()` | 107–113 | DB 연결 | 낮음 |
| `init_schema()` | 115–132 | 스키마 초기화 | 낮음 |
| `drop_schema()` | 134–147 | 스키마 삭제 | 낮음 |
| `create_schema()` | 149–218 | **스키마 정의 (복잡)** | 낮음 |
| `rebuild()` | 220–244 | **재구축 진입점 (핫패스)** | **높음** |
| `_rebuild_incremental_inner()` | 246–380 | **증분 재구축 (핫패스)** | **높음** |
| `_delete_chunk_rows_keep_fts()` | 382–389 | FTS 행 삭제 | 낮음 |
| `_rebuild_inner()` | 391–431 | **전체 재구축 (IO 집약)** | **높음** |
| `_codegraph_enabled()` | 433–436 | 코드그래프 플래그 | 낮음 |
| `_insert_function_chunks_for_python()` | 438–504 | **Python AST 청킹 (중요)** | **중간** |
| `_insert_codegraph_for_path()` | 506–598 | **코드그래프 삽입 (복잡)** | **중간** |
| `_insert_chunk_embedding()` | 600–621 | Dense 임베딩 (선택) | **낮음** (선택 기능) |
| `_bm25_weights()` | 623–646 | BM25 가중치 | 낮음 |
| `_function_chunks_for_python()` | 648–697 | **Python 함수 분할** | **중간** |
| `_compute_rrf_k()` | 699–731 | RRF k 동적 계산 | 낮음 |
| `_looks_like_code_symbol()` | 733–746 | 심볼 여부 판별 | 낮음 |
| `retrieval_policy_for_query()` | 748–770 | **쿼리 정책 선택** | **중간** |
| `_index_state_from_conn()` | 772–782 | 인덱스 상태 | 낮음 |
| `_rg_fallback_enabled()` | 784–787 | Ripgrep 폴백 | 낮음 |
| `_rg_fallback()` | 789–869 | **Ripgrep 폴백 (IO)** | **중간** |
| `query()` | 871–1017 | **쿼리 메인 (핫패스)** | **매우 높음** |
| `context_pack()` | 1019–1023 | 컨텍스트 팩 | **높음** |
| `observability()` | 1025–1083 | 옵저빌러티 보고 | **중간** |
| `indexed_bytes_for_paths()` | 1085–1101 | 인덱스 크기 | 낮음 |
| `configured_retriever()` | 1103–1112 | 리트리버 설정 | 낮음 |
| `iter_text_files()` | 1114–1132 | 파일 반복 (IO) | **낮음** |
| `candidate_files()` | 1134–1147 | 후보 파일 (IO) | **낮음** |
| `summarize()` | 1149–1155 | 스니펫 요약 | 낮음 |
| `snippet_from_file()` | 1157–1185 | **파일 스니펫 추출** | **중간** |
| `escape_fts_query()` | 1187–1191 | FTS 이스케이프 | 낮음 |

**핵심 issue**:
- `query()`가 BM25/dense RRF, 리랭커, rg 폴백을 모두 포함
- `rebuild()` → `_rebuild_inner()` / `_rebuild_incremental_inner()`이 복잡 (파이썬 AST 청킹, codegraph, embedding)
- Indexing (chunk 생성) + Querying (검색 실행) + Observability가 섞여있음

**책임별 그룹**:
1. **스키마 & DB** (Schema): db_path, connect, init_schema, drop_schema, create_schema
2. **재구축/인덱싱** (Rebuild): rebuild, _rebuild_inner, _rebuild_incremental_inner, _insert_function_chunks_for_python, _insert_codegraph_for_path, _insert_chunk_embedding, _function_chunks_for_python
3. **쿼리 & 검색** (Query): query, _looks_like_code_symbol, retrieval_policy_for_query, _rg_fallback, _bm25_weights, _compute_rrf_k, context_pack
4. **옵저빌러티 & 유틸** (Observability): observability, indexed_bytes_for_paths, configured_retriever, iter_text_files, candidate_files, summarize, snippet_from_file, escape_fts_query

---

## 2. 분할안 (Proposed Modules)

### 2.1 cli.py 분할 (현재: 1042줄 → 목표: 300–400줄/모듈)

| 신규 모듈 | 함수 | 예상 줄 | 설명 |
|----------|------|--------|------|
| `cli_core.py` | build_parser(), main() [dispatch 로직만 250줄], emit() | 300 | 파서 + 주요 디스패치 (hook 핸들러 제거) |
| `cli_commands_memory.py` | memory append-event, decision/add, todo/add/close, session/append, tier, audit/rebuild | 150 | 메모리 도메인 핸들러 |
| `cli_commands_search.py` | search rebuild, query, context-pack | 100 | 검색 도메인 핸들러 |
| `cli_commands_obs.py` | obs log, metrics, search, usage, slo, health-summary | 120 | 옵저빌러티 핸들러 |
| `cli_commands_queue.py` | queue enqueue/lease/complete/fail/recover-expired/archive-dead/dead/status | 130 | 큐 핸들러 |
| `cli_commands_admin.py` | version, config, render, doctor, worker, trust, secrets, inbox, notify, diagnostics, migrate, upgrade | 150 | 관리 핸들러 |

**마이그레이션 전략**:
1. `cli_core.py`에서 main() dispatcher를 390줄 → 100줄로 축소
   ```python
   # 현재
   if args.command == "memory" and args.memory_command == "append-event":
       ...
   
   # 분할 후
   if args.command == "memory":
       from .cli_commands_memory import handle_memory_command
       return handle_memory_command(root, args, payload, as_json)
   ```

2. 각 `cli_commands_*.py`는 `def handle_<domain>_command(root, args, payload, as_json) -> int` 구현
3. `emit()`은 `cli_core.py`에 남김 (재사용성)

---

### 2.2 hooks.py 분할 (현재: 1683줄 → 목표: 300–400줄/모듈)

| 신규 모듈 | 함수 | 예상 줄 | 설명 |
|----------|------|--------|------|
| `hooks_core.py` | handle_hook() [200줄], read_payload(), codex_wire_output(), 상수 | 300 | Hook 진입점 + 기초 |
| `hooks_injection.py` | _maybe_apply_delta(), _max_injection_bytes_for(), _target_ms_for(), _injection_marker_path() | 80 | Delta 제거 + 크기 제한 |
| `hooks_rebuild.py` | _spawn_background_rebuild(), _spawn_sleep_time_jobs() | 150 | 백그라운드 작업 |
| `hooks_recommendation.py` | _recommendation_section(), _cached_recommend_invoke(), _skill_recommendation_context(), _agent_recommendation_context(), _precall_recommendation_context(), _recommendation_section._cand_importance(), .invoke() 체인 | 300 | 추천 렌더링 (핫) |
| `hooks_cooldown.py` | _recently_surfaced_ids(), _cooldown_score(), _cooldown_weights(), _adaptive_half_life(), _adaptive_min_signal_from_satisfaction() | 150 | Cooldown/Ebbinghaus 로직 |
| `hooks_context.py` | build_context(), _audit_dependency_paths(), _codex_global_memory_path(), _recommend_memory_deps(), _federated_summary_context(), _satisfaction_summary_context(), _compact_meta_line() | 250 | Hook별 컨텍스트 빌드 |
| `hooks_autonomy.py` | _try_autonomous_accept(), _handle_lifecycle_event() | 200 | 자동 수락 + 라이프사이클 |
| `hooks_scoring.py` | _candidate_raw_strength(), _importance_from_strength(), _candidate_summary_line(), _is_compact_mode(), _compact_section_line() | 100 | 후보 점수 + 렌더 유틸 |

**마이그레이션 전략**:
1. `hooks_core.py` → `handle_hook()` 300줄 축소 (context 빌드 분리)
2. Hook 이벤트별로는 아직 분할 **안 함** (복잡도 높음) → 추후 T12
3. 책임별 분할만 먼저 수행

---

### 2.3 recommend.py 분할 (현재: 1258줄 → 목표: 250–350줄/모듈)

| 신규 모듈 | 함수 | 예상 줄 | 설명 |
|----------|------|--------|------|
| `recommend_core.py` | Signals, Candidate, CatalogEntry, recommend() [메인 루프만 120줄] | 300 | 핵심 루프 + 데이터클래스 |
| `recommend_signals.py` | gather_signals(), _bash_head_cache_path(), _compute_bash_heads(), _write_bash_head_cache(), _spawn_bash_head_cache_rebuild(), _gather_bash_heads(), _gather_claude_global(), _gather_codex_global() | 200 | 신호 수집 |
| `recommend_candidates.py` | _candidates_from_decision_tags(), _candidates_from_todo_tokens(), _candidates_from_audit_actions(), _candidates_from_codex_groups(), _candidates_from_codex_keywords(), _candidates_from_bash_heads(), _draft_body_*() (6개) | 250 | 후보 생성 (신호원별) |
| `recommend_scoring.py` | cluster_candidates(), _signal_strength(), _signal_kind(), _per_signal_max(), _normalized_strength(), _adaptive_min_signal(), _adaptive_min_signal_lower() | 150 | 점수 + 클러스터링 |
| `recommend_catalog.py` | catalog_path(), list_catalog(), _persist_entry(), compact_skill_catalog() | 150 | Catalog IO |
| `recommend_util.py` | _slugify(), _sha256(), _candidate_id(), _danger_match(), _hyphen_encode_path(), _claude_global_dir(), _codex_memories_path(), _is_path_like_task_group(), _evidence_snippets(), _is_meaningful_bigram() | 100 | 유틸 함수 |

**마이그레이션 전략**:
1. `recommend_core.py`에서 recommend() 주요 루프 유지 (import만 변경)
2. 신호 소스별로 `_candidates_from_*()` 그룹화
3. Catalog IO는 완전히 분리

---

### 2.4 search.py 분할 (현재: 1191줄 → 목표: 250–350줄/모듈)

| 신규 모듈 | 함수 | 예상 줄 | 설명 |
|----------|------|--------|------|
| `search_core.py` | query(), context_pack(), 상수 | 300 | 쿼리 메인 + 진입점 |
| `search_schema.py` | db_path(), connect(), init_schema(), drop_schema(), create_schema() | 100 | DB 스키마 |
| `search_rebuild.py` | rebuild(), _rebuild_inner(), _rebuild_incremental_inner(), _delete_chunk_rows_keep_fts() | 200 | 인덱스 재구축 (IO 집약) |
| `search_chunking.py` | _insert_function_chunks_for_python(), _insert_codegraph_for_path(), _insert_chunk_embedding(), _function_chunks_for_python(), _codegraph_enabled() | 200 | 청킹 + AST 분석 |
| `search_query.py` | retrieval_policy_for_query(), _looks_like_code_symbol(), _rg_fallback(), _bm25_weights(), _compute_rrf_k(), _index_state_from_conn(), _rg_fallback_enabled() | 180 | 쿼리 전략 + 검색 |
| `search_util.py` | iter_text_files(), candidate_files(), summarize(), snippet_from_file(), escape_fts_query(), indexed_bytes_for_paths(), configured_retriever(), observability() | 150 | 파일 + 스니펫 유틸 |

**마이그레이션 전략**:
1. `search_core.py` query() 유지 (import 만 변경)
2. `search_rebuild.py` → 스토리지 레이어 (IO, 파일 시스템)
3. `search_query.py` → 검색 전략 (알고리즘)
4. `search_chunking.py` → AST 복잡도 격리

---

## 3. 위험 분석

### 3.1 최고 위험 분할: hooks.py → hooks_recommendation.py

**사유**:
1. `_recommendation_section()` (100줄)이 SessionStart/UserPromptSubmit 두 hook의 핫 패스
   - 타겟: 200ms (SessionStart), 200ms (UserPromptSubmit)
   - 추천 후보 렌더링 + cooldown 점수 계산 + 컴팩트 모드 선택
2. `.invoke()` 체인 (3개, 총 50줄)이 비동기 import를 포함
   - 순환 import 위험 (recommend.py → hooks.py → hooks_recommendation.py → recommend.py?)
3. Ebbinghaus 감소 함수와 cooldown이 강하게 결합되어 있음

**회귀 포인트**:
- SessionStart 응답 시간 +50ms 이상 → 즉시 감지 (SLO 위반)
- Cooldown 점수 산정 오류 → 추천 중복도 증가 → 사용자 만족도 하락
- Import 순환 → 런타임 에러

**완화**:
1. Import는 함수 내부에서만 수행 (이미 그런 상태)
2. Cooldown 로직 테스트 커버리지 >= 90%
3. 성능 테스트: `ai memory session --json` + `AI_PERF_LOG=1`로 latency 추적

---

### 3.2 높은 위험: search.py → search_rebuild.py

**사유**:
1. `_rebuild_incremental_inner()` (130줄) + `_rebuild_inner()` (40줄) = 170줄의 복잡한 SQL 조작
2. Python AST 청킹 (`_insert_function_chunks_for_python()`, `_function_chunks_for_python()`)이 75줄 규모
3. Codegraph 삽입이 90줄 규모
4. 한 번의 청크 생성 오류 → 검색 쿼리가 garbage 반환 → 모든 IDE 통합 실패

**회귀 포인트**:
- 구문 정확성 (Python AST 파싱 경계)
- 스키마 호환성 (SCHEMA_VERSION)
- 증분 vs 전체 모드 구분

**완화**:
1. 분할 전 `test_search_*.py` 커버리지 >85% 필수
2. 증분 재구축 테스트 추가 (mock를 통한 100행 이상 파일 청킹)
3. Smoke test: `ai search query --json "def main"` 성공 여부

---

### 3.3 중간 위험: recommend.py → recommend_signals.py

**사유**:
1. `gather_signals()`가 audit, todos, decisions, Claude/Codex 글로벌을 동시 수집 (IO 집약)
2. Bash head 캐시가 비동기 재구축 (I/O race condition 가능)
3. 글로벌 메모리 경로 오류 → 신호 수집 실패 → 추천 전무

**회귀 포인트**:
- 글로벌 메모리 경로 변경 → gather 실패
- Bash cache I/O race
- Codex 메모리 버전 호환성

**완화**:
1. Mock fixture로 경로 오류 감지
2. I/O race 테스트 (concurrent write)
3. Fallback: gather_signals 실패 → 기본 추천만 반환

---

### 3.4 중간 위험: cli.py → cli_commands_*.py (6개 파일)

**사유**:
1. main() dispatcher가 100줄 → 6개 import로 분산
2. Import 실패 → 전체 CLI 불가 (명령 하나 오류 → 모든 명령 실패)
3. Argument parsing이 cli_core.py에 남아있음 → 버그 수정 시 2곳 수정 필요

**회귀 포인트**:
- 모듈 로딩 실패 시 graceful degradation 부족
- Argument 추가 시 parser 누락 가능

**완화**:
1. Import는 lazy (필요할 때만)
2. `cli_commands_*.py`는 pure function (부작용 최소화)
3. 전체 테스트: `ai version`, `ai help`, 모든 subcommand dry-run

---

## 4. 점진 마이그레이션 계획

### 4.1 단계별 구성 (6–8개 PR, 각 PR당 +/-300줄 이내)

**PR#1: search.py → search_schema.py** (목표: 안정성 높음, 변경 최소)
- 변경: +100줄, -0줄
- 신규: `search_schema.py` (db_path, connect, init_schema, drop_schema, create_schema)
- 변경: `search.py`에서 import 추가
- 테스트: `test_search_schema.py` (스키마 생성/삭제 smoke)
- 위험: **낮음**
- PR 크기: 100줄 미만

**PR#2: search.py → search_util.py + search_core.py (일부)**
- 변경: +150줄, -0줄
- 신규: `search_util.py` (iter_text_files, candidate_files, summarize, snippet_from_file, escape_fts_query, indexed_bytes_for_paths, configured_retriever)
- 변경: `search.py` import
- 테스트: `test_search_util.py` (파일 처리, 스니펫 추출)
- 위험: **낮음**
- PR 크기: 150줄

**PR#3: recommend.py → recommend_util.py + recommend_catalog.py**
- 변경: +250줄, -0줄
- 신규: `recommend_util.py` (6개 유틸), `recommend_catalog.py` (catalog IO 4개 함수)
- 변경: `recommend.py` import
- 테스트: Catalog persistence test
- 위험: **낮음**
- PR 크기: 250줄

**PR#4: cli.py → cli_core.py** (dispatcher 축소)
- 변경: -250줄, +50줄
- 신규: `cli_core.py` (build_parser 축소, emit 유지, main dispatcher 간소화)
- 변경: 기존 cli.py 100줄 삭제
- 테스트: `ai version`, `ai help` smoke test
- 위험: **중간** (진입점 변경)
- PR 크기: 200줄

**PR#5: cli.py 도메인별 분할** (4개 파일: memory, search, obs, queue)
- 변경: +300줄, -200줄
- 신규: `cli_commands_memory.py`, `cli_commands_search.py`, `cli_commands_obs.py`, `cli_commands_queue.py`
- 변경: cli_core.py dispatcher (4개 domain 추가)
- 테스트: 각 subcommand dry-run
- 위험: **중간** (도메인별 격리 필요)
- PR 크기: 300줄

**PR#6: cli.py 도메인별 분할 2** (2개 파일: admin, worker/queue 추가)
- 변경: +200줄, -150줄
- 신규: `cli_commands_admin.py` (version, config, doctor, render, etc.)
- 변경: cli_core.py dispatcher
- 테스트: admin command 전체
- 위험: **중간**
- PR 크기: 200줄

**PR#7: hooks.py 책임별 분할 1** (injection + rebuild)
- 변경: +150줄, -0줄
- 신규: `hooks_injection.py` (delta 제거), `hooks_rebuild.py` (background jobs)
- 변경: hooks.py import
- 테스트: injection marker, rebuild spawn
- 위험: **낮음**
- PR 크기: 150줄

**PR#8: hooks.py 책임별 분할 2** (cooldown + scoring)
- 변경: +150줄, -0줄
- 신규: `hooks_cooldown.py`, `hooks_scoring.py`
- 변경: hooks.py import
- 테스트: Ebbinghaus decay, strength scoring
- 위험: **낮음**
- PR 크기: 150줄

**PR#9: hooks.py 책임별 분할 3** (추천 & 컨텍스트) ← 최고 위험
- 변경: +300줄, -0줄
- 신규: `hooks_recommendation.py`, `hooks_context.py`
- 변경: hooks.py (handle_hook 축소), build_context import 변경
- 테스트: SessionStart latency (<250ms), recommendation rendering
- 위험: **높음**
- PR 크기: 300줄

**PR#10: recommend.py 책임별 분할** (signals + candidates + scoring)
- 변경: +300줄, -0줄
- 신규: `recommend_signals.py`, `recommend_candidates.py`, `recommend_scoring.py`
- 변경: `recommend_core.py` import
- 테스트: Signal gathering, candidate generation, clustering
- 위험: **중간** (신호 수집 복잡도)
- PR 크기: 300줄

**PR#11: search.py 청킹 분할** (rebuilding 분리)
- 변경: +200줄, -0줄
- 신규: `search_chunking.py`, `search_rebuild.py` (통합 rebuild logic)
- 변경: `search_core.py` import
- 테스트: Python AST 청킹, codegraph insertion
- 위험: **높음** (구문 정확성)
- PR 크기: 200줄

**PR#12: search.py 쿼리 분할** (query strategy 분리)
- 변경: +150줄, -0줄
- 신규: `search_query.py` (RRF, BM25, policy selection)
- 변경: `search_core.py` import, query() 축소
- 테스트: Retrieval policy selection, RRF weighting
- 위험: **낮음** (순수 함수)
- PR 크기: 150줄

---

### 4.2 우선순위 (회귀 최소화 기준)

**즉시 수행 (주 1–2)**:
1. PR#1 (search_schema) — 스키마 격리, 테스트 용이
2. PR#2 (search_util) — 파일 I/O 분리
3. PR#7 (hooks_injection) — delta 로직 독립

**단계 2 (주 3–4)**:
4. PR#3 (recommend catalog) — IO 격리
5. PR#4 (cli_core) — dispatcher 축소 (신중함 필요)

**단계 3 (주 5–6)**:
6. PR#5, PR#6 (cli 도메인별) — 작은 단위로 병렬 가능

**단계 4 (주 7–8)**:
7. PR#8 (hooks cooldown) — 점수 함수 분리
8. PR#9 (hooks 추천) — **최고 위험, 성능 테스트 필수**

**단계 5 (주 9–10)**:
9. PR#10 (recommend 신호) — 신호 수집 분리
10. PR#11 (search 청킹) — AST 분리
11. PR#12 (search 쿼리) — 쿼리 분리

---

## 5. 테스트 전략

### 5.1 각 PR별 테스트 체크리스트

#### PR#1 (search_schema)
- [ ] `test_search_schema.py`: create_schema() → FTS5 생성 확인
- [ ] `test_search_schema.py`: drop_schema() → 테이블 삭제 확인
- [ ] `test_search_schema.py`: init_schema() migration path 확인
- [ ] smoke: `ai search query --json "test"` 실행 (기존 DB 사용)

#### PR#2 (search_util)
- [ ] `test_search_util.py`: iter_text_files() → SKIP_DIRS 동작 확인
- [ ] `test_search_util.py`: candidate_files() → .git 제외 확인
- [ ] `test_search_util.py`: snippet_from_file() → 경계 처리
- [ ] smoke: `ai search context-pack --json "def main"` latency <100ms

#### PR#3 (recommend catalog)
- [ ] `test_recommend_catalog.py`: _persist_entry() → JSONL 형식
- [ ] `test_recommend_catalog.py`: list_catalog() → 파싱 정확성
- [ ] `test_recommend_catalog.py`: compact_skill_catalog() → 중복 제거
- [ ] smoke: catalog.jsonl 손상 시 fallback 동작

#### PR#4 (cli_core)
- [ ] `test_cli.py`: build_parser() → 모든 subcommand 등록 확인
- [ ] `test_cli.py`: main("version") → version 출력
- [ ] `test_cli.py`: main("--json") flag 동작
- [ ] smoke: `ai --json version`, `ai help` 동작
- [ ] **성능**: CLI 로딩 시간 변화 <10ms (기준: 50ms)

#### PR#5–6 (cli 도메인)
- [ ] `test_cli_commands_*.py`: 각 domain handler 존재 + 반환 타입 확인
- [ ] dry-run: 모든 subcommand `--dry-run` 옵션 동작
- [ ] smoke: `ai memory append-event`, `ai search rebuild`, `ai obs metrics` 실행

#### PR#7 (hooks_injection)
- [ ] `test_hooks_injection.py`: _maybe_apply_delta() → SHA 일치 여부
- [ ] `test_hooks_injection.py`: _injection_marker_path() → 경로 정확성
- [ ] `test_hooks_injection.py`: MAX_INJECTION_BYTES 상수 동작
- [ ] **성능**: delta_skipped=True 경로 <20ms
- [ ] smoke: `AI_DELTA_NOTICE_VERBOSE=1 ai hook UserPromptSubmit`

#### PR#8 (hooks_cooldown)
- [ ] `test_hooks_cooldown.py`: _cooldown_score() → Ebbinghaus decay 수식 검증
- [ ] `test_hooks_cooldown.py`: _adaptive_half_life() → satisfaction 기반 가중치
- [ ] `test_hooks_cooldown.py`: _recently_surfaced_ids() → cooldown_hours 동작
- [ ] **성능**: cooldown 계산 <50ms (1000개 후보)
- [ ] validate: decay curve 그래프 (도메인 전문가 리뷰)

#### PR#9 (hooks_recommendation) ← 최고 우선순위 성능 테스트
- [ ] `test_hooks_recommendation.py`: _recommendation_section() → 후보 정렬 정확성
- [ ] `test_hooks_recommendation.py`: .invoke() 체인 → import 순환 없음
- [ ] `test_hooks_recommendation.py`: _cached_recommend_invoke() → 캐시 히트율 >70%
- [ ] **성능 (필수)**: SessionStart latency <250ms (ai memory session)
  - [ ] 기준: 사전 분할 후 5회 측정 평균
  - [ ] 분할 후: 5회 측정 평균
  - [ ] SLO: Δ < +30ms (상한)
- [ ] **성능**: UserPromptSubmit latency <200ms
- [ ] smoke: `AI_COMPACT_RECOMMENDATIONS=1 ai hook UserPromptSubmit`
- [ ] validate: recommendation 순서 변경 없음

#### PR#10 (recommend 신호)
- [ ] `test_recommend_signals.py`: gather_signals() → 모든 신호 소스 수집
- [ ] `test_recommend_signals.py`: _gather_bash_heads() → Bash 캐시 race 없음
- [ ] `test_recommend_signals.py`: _gather_claude_global() → 경로 오류 시 fallback
- [ ] mock: Codex 글로벌 부재 시 empty signal
- [ ] smoke: `ai memory tier --json` → signal gathering 부재 시도 발생 안 함

#### PR#11 (search 청킹) ← 두 번째 최고 위험
- [ ] `test_search_chunking.py`: _function_chunks_for_python() → AST 파싱 정확성
  - [ ] syntax error handling
  - [ ] 중첩 함수, decorator 처리
- [ ] `test_search_chunking.py`: _insert_codegraph_for_path() → tree-sitter 출력
- [ ] **스키마 호환성**: SCHEMA_VERSION bump 필요 확인
- [ ] smoke: `ai search rebuild --json` → 인덱스 크기 변화 <10%
- [ ] validate: 검색 결과 변화 없음 (`ai search query "def main"`)

#### PR#12 (search 쿼리)
- [ ] `test_search_query.py`: retrieval_policy_for_query() → symbol vs keyword 판별
- [ ] `test_search_query.py`: _compute_rrf_k() → dynamic k 계산
- [ ] `test_search_query.py`: _rg_fallback() → ripgrep 폴백 동작
- [ ] **성능**: query() latency 변화 <±5%
- [ ] validate: 쿼리 순서 변경 없음

### 5.2 전체 통합 테스트

```bash
# 분할 후 회귀 테스트
pytest .ai/runtime/tests/test_*.py -v --tb=short -x

# 성능 추적
export AI_PERF_LOG=1
time .ai/bin/ai hook SessionStart --json < test_payload.json
# 기준: <1500ms

# 검색 정확성
.ai/bin/ai search query --json "def main" | jq '.results | length'
# 기준: >0 (결과 있음)

# CLI 호환성
for cmd in version config doctor memory obs search; do
  .ai/bin/ai $cmd --help > /dev/null || echo "FAIL: $cmd"
done
```

---

## 6. 롤백 전략

### 6.1 PR별 롤백

**Type A: 스키마/DB 변경 (search_schema.py)**
- 롤백: `git revert <PR-hash>`
- DB: 기존 `.ai/cb.db` 유지 (호환성 보장)
- 테스트: `ai search query` 동작 확인

**Type B: 함수 이동 (recommend_util.py, hooks_cooldown.py 등)**
- 롤백: `git revert <PR-hash>` + 불필요한 import 정리
- 테스트: unit test 통과 확인
- 위험도: 낮음 (순수 함수)

**Type C: 진입점 변경 (cli_core.py, hooks_core.py)**
- 롤백: `git revert <PR-hash>`
- 테스트: `ai version`, `ai hook SessionStart`
- 위험도: 높음 (모든 명령 영향)
- **회피 전략**: 즉시 revert 대신 hotfix branch 사용

### 6.2 대규모 회귀 시 전략

**시나리오 1: SessionStart 응답 시간 +50ms 이상**
```bash
# 최신 develop에서 분할 전 상태로 복구
git log --oneline | grep -E "PR#8|PR#9"
git revert <그 중 가장 최신 PR>
ai memory session --json | jq '.metadata.latency_ms'
```

**시나리오 2: 검색 결과 garbage (search_chunking.py 오류)**
```bash
# DB 복구
rm .ai/cb.db
git revert <PR#11>
ai search rebuild --json
ai search query --json "def main"
```

**시나리오 3: 추천 중복도 증가 (hooks_recommendation.py or recommend_signals.py)**
```bash
# 메모리 감사
.ai/bin/ai memory audit --json | grep "duplicates"
git log -p --follow -- .ai/notes/research/GOD_MODULE_SPLIT_PROPOSAL.md \
  | grep -A5 "cooldown_weights\|_cached_recommend_invoke"
# (회귀 원인 파악 후 targeted fix)
```

---

## 7. 주요 의존성 맵

### 7.1 모듈 간 호출 그래프

```
cli.py
├── hooks.py (read_payload, handle_hook, codex_wire_output)
├── search.py (rebuild, query, context_pack)
├── memory.py (append_audit, append_event, rebuild_audit_index)
├── recommend.py (없음, 추천은 hooks 경유)
└── 기타 (doctor, render, inbox, trust, ...)

hooks.py
├── memory.py (append_event, append_audit, ...)
├── recommend.py (recommend, _spawn_bash_head_cache_rebuild)
├── search.py (없음, 직접 호출 없음)
├── config.py (load_config)
└── policy.py (is_ci)

recommend.py
├── memory.py (read_jsonl_*, append_jsonl, append_audit)
├── redact.py (redact_value)
└── portable.py (hyphen_encode_path)

search.py
├── config.py (load_config)
├── redact.py (redact_value)
└── (외부: sqlite3, subprocess, pathlib)
```

### 7.2 분할 후 의존성 (import 대상 변경 필요)

| 모듈 | 현재 import | 분할 후 import |
|------|-----------|----------------|
| cli_core.py | hooks.handle_hook | hooks_core.handle_hook |
| hooks_core.py | recommend.recommend (간접) | hooks_recommendation._cached_recommend_invoke |
| hooks_recommendation.py | recommend.recommend | recommend_core.recommend (새 import) |
| recommend_core.py | (변경 없음) | recommend_signals, recommend_candidates, recommend_scoring |
| search_core.py | (변경 없음) | search_rebuild, search_query, search_util |

---

## 8. 요약

### 8.1 분할 제안 (총 모듈 수)

**cli.py**: 1042줄 → 6개 모듈 (cli_core + 5개 도메인)
**hooks.py**: 1683줄 → 8개 모듈 (hooks_core + 7개 책임별)
**recommend.py**: 1258줄 → 6개 모듈 (recommend_core + 5개 책임별)
**search.py**: 1191줄 → 6개 모듈 (search_core + 5개 책임별)

**합계: 26개 신규 모듈**, 기존 4개 god module 유지 (deprecated)

### 8.2 마이그레이션 일정

- **총 PR 수**: 12개
- **총 기간**: 8–10주 (주당 1–2개 PR)
- **가장 위험한 PR**: PR#9 (hooks_recommendation), PR#11 (search_chunking)
- **가장 안전한 첫 단계**: PR#1 (search_schema), PR#2 (search_util), PR#7 (hooks_injection)

### 8.3 최종 목표

- 각 모듈 200–400줄 (읽기 용이)
- hot path (handle_hook, build_context, query, recommend) 성능 ±5% 이내
- 회귀 위험 최소화: 단위 테스트 + smoke test + 성능 측정
- 점진적 배포: 한 번에 한 PR (parallel가능한 부분만)

---

## 부록 A: 함수 이동 체크리스트 (PR#9 예시)

```
hooks.py (1683줄) → 분할
├── hooks_core.py (300줄)
│  ├── handle_hook() [200줄, 라인 1126–1337]
│  ├── read_payload() [라인 1119–1123]
│  ├── codex_wire_output() [라인 1349–1397]
│  └── 상수 (HOT_PATH_TARGET_MS, INJECTION_HOOKS, ...)
├── hooks_recommendation.py (300줄)
│  ├── _recommendation_section() [라인 541–640]
│  ├── _cand_importance() [라인 593–604, nested]
│  ├── _cached_recommend_invoke() [라인 686–725]
│  ├── _skill_recommendation_context() [라인 729–763]
│  ├── .invoke() [라인 733–750, nested]
│  ├── _agent_recommendation_context() [라인 854–884]
│  ├── .invoke() [라인 856–871, nested]
│  ├── _precall_recommendation_context() [라인 887–918]
│  └── .invoke() [라인 889–905, nested]
├── hooks_context.py (250줄)
│  ├── build_context() [라인 1546–1683]
│  ├── _audit_dependency_paths() [라인 646–662]
│  ├── _codex_global_memory_path() [라인 664–666]
│  ├── _recommend_memory_deps() [라인 668–683]
│  ├── _federated_summary_context() [라인 921–959]
│  ├── _satisfaction_summary_context() [라인 960–1034]
│  └── _compact_meta_line() [라인 1036–1116]
├── hooks_cooldown.py (150줄)
│  ├── _recently_surfaced_ids() [라인 241–287]
│  ├── _cooldown_score() [라인 289–306]
│  ├── _cooldown_weights() [라인 309–380]
│  ├── _adaptive_half_life() [라인 383–427]
│  └── _adaptive_min_signal_from_satisfaction() [라인 483–523]
├── hooks_scoring.py (100줄)
│  ├── _candidate_raw_strength() [라인 429–449]
│  ├── _importance_from_strength() [라인 451–464]
│  ├── _candidate_summary_line() [라인 466–481]
│  ├── _is_compact_mode() [라인 525–526]
│  └── _compact_section_line() [라인 529–539]
├── hooks_injection.py (80줄)
│  ├── _maybe_apply_delta() [라인 73–99]
│  ├── _max_injection_bytes_for() [라인 61–64]
│  ├── _target_ms_for() [라인 67–70]
│  ├── _injection_marker_path() [라인 57–58]
│  └── MAX_INJECTION_BYTES, SESSION_START_MAX_INJECTION_BYTES
├── hooks_rebuild.py (150줄)
│  ├── _spawn_background_rebuild() [라인 102–135]
│  └── _spawn_sleep_time_jobs() [라인 137–240]
└── hooks_autonomy.py (200줄)
   ├── _try_autonomous_accept() [라인 766–852]
   └── _handle_lifecycle_event() [라인 1399–1543]
```

---

**문서 생성 완료**: `/Users/ezbuilder/workspace/code-brain/.ai/notes/research/GOD_MODULE_SPLIT_PROPOSAL.md`  
**라인 수**: 1247줄 (마크다운)  
**분할 제안 모듈 수**: 26개  
**가장 위험한 분할**: PR#9 (hooks.py → hooks_recommendation.py, 300줄), hooks 핫패스 (SessionStart <250ms) 성능 회귀 위험  
**가장 안전한 첫 단계**: PR#1 (search.py → search_schema.py, 스키마 격리, 100줄 미만)
