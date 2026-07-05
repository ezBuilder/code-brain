# memanto → Code Brain 접목 분석 (2026-06-18)

> 출처: `https://github.com/moorcheh-ai/memanto` (c39af7a, MIT) 임시 클론 후 다단계 워크플로 분석.
> 통계: memanto 7개 서브시스템 + Code Brain 3개 영역 병렬 매핑 → 14개 후보 합성 → 14개 후보별 적대적 검증 → 합성. 에이전트 26, 도구호출 633, 토큰 2.2M.

## 1. 결론

**14개 후보 중 adopt 0, pilot 4(G2·G3·G4·G10), watch 5, reject 5 — memanto의 클라우드/Moorcheh RAG 자산은 거의 전이되지 않고, 가치 있는 부분은 "아이디어와 ~50줄 포맷 헬퍼"뿐이다. 핵심 도입 가치는 CB가 이미 가진 로컬 JSONL 메모리 위에 "온디맨드 질의 표면"을 여는 것(G2/G3)과 오프라인 충돌 판정 파일럿(G4), MIT 워크플로 스킬 2종(G10)에 한정된다.**

memanto는 Moorcheh 벡터 클라우드(또는 로컬 Docker+Ollama)에 13종 타입 카드를 무임베딩으로 업로드하고 `#key:value` 인-쿼리 필터·서버측 RAG `answer`로 회상/합성하는 개인 비서형 장기메모리다. 핵심 베팅은 "쓰기 시점 LLM 추출을 의도적으로 생략 → 저장 즉시 검색 가능·제로 인입 비용"이며, 신뢰도(provenance×검증×나이감쇠)·충돌판정·시계열 질의 모델을 갖췄으나 그중 신뢰도/검증 로직은 성능 사유로 비활성("Skipped for speed")인 채 출하됐다.

## 2. 우선순위 접목 후보표

| # | 후보 | memanto 출처 | CB 접목 대상 | 판정 | 노력 | CB 기존중복 |
|---|---|---|---|---|---|---|
| G2 | answer_over_memory: 로컬 회상+인용 | answer RAG triad + format_context_block | mcp_server.py 신규 read 도구 (lessons/decisions/procedures 위) | **pilot** | medium | 부분(lessons_recall만, decisions 질의 불가) |
| G3 | list/search_decisions 필터 도구 | recall #type/#status/#confidence 토큰 | mcp_server.py + memory.read_jsonl_all/retention_score | **pilot** | medium | 없음(decisions 질의 도구 부재) |
| G4 | LLM-judge 충돌 탐지(오프라인 사이드카) | DailyAnalysisService.generate_conflict_report | 신규 memory_conflicts.py + self_improve 워커 패턴 | **pilot** | large | 부분(distill-time Jaccard 게이트만, 의미판정 없음) |
| G10 | diagnose/tdd-with-memory 스킬 | 두 SKILL.md 템플릿(MIT) | .agents/skills/&lt;slug&gt;/SKILL.md (직접 작성) | **pilot** | small | 없음(고정 스킬 라이브러리 부재) |
| G5 | 메모리 ASCII 분석 스냅샷 | SummaryVisualizationService | obs.py 신규 렌더러 | watch | small | 부분(집계는 이미 obs/retention_report에 존재) |
| G6 | decisions provenance 속성 | provenance enum + evidence 보강 | memory.append_decision + evidence.py 패턴 | watch | medium | 부분(evidence.jsonl만 dict 보유) |
| G8 | recall-before/store-after 스킬 매크로 | memory sandwich + 13타입 우선순위 사다리 | hooks.build_context 헤더 문구 | watch | small | 대부분(build_context가 이미 우월) |
| G11 | as-of/changed-since 시계열 질의 | search_as_of/changed_since/recent | memory.py read 헬퍼 | watch | medium | 부분(타임스탬프·fold는 있음, 질의표면 없음) |
| G13 | provisional 안티-포이즈닝 정책 | core.ValidationPolicy.validate_memory | memory.append_decision status | watch | large | 부분(status 머신은 failure 전용) |
| G1 | 쓰기 핫패스 타입 분류기 | MemoryParsingService 가중정규식 | memory.append_decision + RETENTION_TYPE_WEIGHTS | reject | large | 부분(가중표 존재, 단 provenance로 이미 발화) |
| G7 | provenance-가중 신뢰도 재구성 | core.compute_confidence(휴면) | memory_tier.retention_score | reject | medium | 대부분(CB 모델이 더 강함, provenance만 결여) |
| G9 | LLM-free 휴리스틱 추출기 | extractor.py 문장분할+키워드맵 | session_resume/lessons | reject | medium | 없음(단 G1 의존·타겟 blob 부재) |
| G12 | Markdown-as-event-log | SessionService 세션 로그 | session-current.md 스키마 | reject | small | 부분(JSONL+audit-index가 이미 더 강함) |
| G14 | LangGraph BaseStore 어댑터 | MemantoStore | 신규 외부 어댑터 패키지 | reject | large | 없음(미션 역전, 수요 없음) |

## 3. ADOPT/PILOT 상세

### G2 — answer_over_memory (pilot, medium)
**무엇**: `lessons_recall`의 confidence×relevance×recency 스코어러를 decisions·folded-failures·todos·session-notes로 일반화하고, 인용 블록을 조립해 반환하는 **오프라인 read 표면**. memanto의 네트워크 `answer`(Moorcheh+호스티드 LLM)는 이식 불가 — 아이디어와 `format_context_block` 마크다운 레이아웃(~50줄)만 전이.
**어디**: 신규 `.ai/runtime/src/ai_core/memory_recall.py`; MCP 등록은 `mcp_server.py` `lessons_recall`(L283 카탈로그 / L926 디스패치) 옆.
**첫 단계**: `recall_memory(root, *, query, limit, types=None)` 작성 — `lessons.score_lessons`(lessons.py:136)·`memory.read_decisions_for_surface`(memory.py:144, 일반 decision+live failure)·`procedural_memory.search_procedures`(:148)에서 후보 수집, **타입별 confidence prior** 부여(explicit decision=high, failure=status 의존, lesson=score_lessons, procedure=검색점수), recall_lessons의 relevance×recency 공식(:227-235) 재사용.
**주의**: (1) **LLM 합성 절반은 파일럿에서 제외** — CB 워커는 비동기(enqueue→loopd→worker)라 MCP 한 호출로 합성 응답을 await 못 하고 job id만 반환됨. (2) 도구명은 `answer`가 아닌 **`memory_recall`** (합성 함의 회피). (3) 조립 블록 전 필드를 `redact.redact_value` 통과시켜 observed_versions/environment 누출 방지. (4) DECISIONS_TAIL을 그대로 재방출하면 가치 미미 — relevance 필터가 본질.

### G3 — list/search_decisions 필터 도구 (pilot, medium)
**무엇**: decisions/todos에 대한 온디맨드 필터 read. 에이전트는 현재 SessionStart 주입 3개 tail(hooks.py:41 DECISIONS_TAIL=3, :2251)만 받고 미드세션 질의가 불가.
**어디**: `mcp_server.py`에 read 전용 `list_decisions` 등록(record_decision 스키마 L178 옆, 디스패치 L854 옆); `cli.py` `memory decision list` 서브파서(L343 부근, `memory evidence list` L333 플래그 미러).
**첫 단계**: memory.py에 `read_decisions_filtered(root, *, kind, status, tags, source, text, limit)` — **반드시 read_decisions_for_surface의 fold-by-id + `_RETIRED_STATUSES` 제외 로직(memory.py:144-168)을 재현**. (failure는 id 재사용 supersession이라 naive 스캔 시 중복·retired 누출, memory.py:136-137.)
**주의**: `type=architecture`·`min_confidence=0.7` 필터는 **백킹 필드가 없음**(스키마는 kind/status/tags/source뿐) — G1 미착륙 시 no-op이므로 스키마에서 **생략하거나 문서화된 no-op으로 명시**. 출력은 외부 채널이므로 redact 필수. recall_recent/as_of/changed_since는 후속(저비용이나 비핵심).

### G4 — LLM-judge 충돌 탐지 (pilot, large — **advisory-only**)
**무엇**: 비인접 메모리 간 "use X vs never-use X" 의미 충돌을 주기적/page_out 시 cheap non-self LLM이 JSON으로 판정. **CB는 이미 distill-time 결정론적 게이트**(`_conflicting_decisions`, loop_engineering.py:614-665, Jaccard 0.45, scan 800)로 최고위험(신규 durable 규칙이 기존과 충돌)을 차단하고 의미판정을 in-loop 에이전트에 위임(:619-620) — 신규 델타는 "전체 코퍼스 배치 판정"뿐.
**어디**: 신규 `memory_conflicts.py`; 워커는 `self_improve.py`(이미 cheap non-self·핫패스 외·M_core 게이트·"no LLM/network in this module") 패턴 정확히 미러; 트리거는 `memory_tier.page_out`(:265)에 audit_fold(:323)처럼 piggyback(non-fatal).
**첫 단계**: `memory_conflicts.py`에 결정론적 prefilter(loop_engineering의 token-overlap 재사용, scan 800 바운드로 고중첩 쌍만 LLM에 투입) + `conflicts.jsonl`(resolved=false) writer 작성 — **모듈 자체는 stdlib·무LLM·무네트워크**, test는 test_self_improve.py 모델. 전체를 env 플래그 뒤 dark 출하.
**주의**: **해소(resolution)는 수동** — `supersedes_id`는 kind==failure만 fold(memory.py:122-137)하므로 일반 decision 충돌엔 fold 경로가 없음. 자율 LLM이 durable 메모리에 쓰는 것은 CB 최고위험 표면 → 파일럿은 사이드카 advisory만, decisions.jsonl 미접촉. 먼저 "distill 게이트가 놓치는 실제 충돌을 잡는지" 측정 후에만 해소 검토. `remove_both` 권고는 안전상 제외.

### G10 — diagnose/tdd-with-memory 스킬 (pilot, small)
**무엇**: MIT 프롬프트 자산 2종(6단계 디버그+에러패턴 회상/근본원인 저장; RED→GREEN+컨벤션 decision 저장). CB의 failure-as-dated-observation(record_decision kind=failure + observed_versions/retest_after)·lessons가 깔끔한 로컬 백엔드.
**어디**: **`recommend.py` accept()/catalog 경유 금지** — 그 경로는 자동 마이닝 후보 전용(seed-from-file 없음, ID는 content-hash+T42 dedup). **직접 작성**: `.agents/skills/diagnose-with-memory/SKILL.md`, `.agents/skills/tdd-with-memory/SKILL.md`(추적·사용자 작성 가능·write-guard 없음).
**첫 단계**: memanto 본문 복사 후 메모리 콜 리타게팅 — `memanto-skills recall` → `.ai/bin/ai lessons recall --query "<패턴>"`; `store --type learning` → `.ai/bin/ai memory decision add --kind failure --observed-versions ... --retest-after <date>` + lessons add_lesson(failure/cause/fix 필수 3필드). 원하면 `.claude/commands/*.md`(description-only frontmatter)도 추가. MIT LICENSE/NOTICE 귀속 유지. `make lint` + `.ai/bin/ai skills list` 검증.
**주의**: tdd의 decision/preference/instruction 회상은 CB에 직접 등가 없음(decisions는 질의표면 부재 — G3에 의존). 직접 작성 시 managed-by 마커·카탈로그 라이프사이클 밖이라 `skills list`/uninstall과 분리됨. 가치는 modest(Opus급 에이전트가 이미 적용하는 규율) — durable 이득은 회상/저장 배선.

## 4. 기각 / 이미보유 (간략)

- **G1 분류기 (reject)**: 전제 오류 — `RETENTION_TYPE_WEIGHTS`는 죽지 않았다. `scored_durable_items()`가 **provenance로 타입 매핑**(decisions→decision L475, lessons→lesson L490, procedures→kind L506-507). 게다가 memanto 13타입(CRM/스탠드업)과 CB 13타입(코드)은 4개만 이름 겹침 → "이식"이 아니라 "재작성". 부착 지점도 없음(append_decision은 이미 kind-typed).
- **G7 신뢰도 재구성 (reject)**: 이식 대상이 memanto에서 **죽은 코드**(compute_confidence→trust_score 미호출, 쓰기/읽기 양쪽 "Skipped for speed"). CB retention_score는 연속 감쇠+타입 salience+reinforcement+confidence override로 이미 **상위집합**. 유일한 신규 인자 provenance_weight는 G6(미증명 스키마 변경) 선행 필요.
- **G9 휴리스틱 추출기 (reject)**: G1 의존 + 타겟 "free-text blob" 부재(handoff는 이미 구조화 필드, lessons는 이미 atomic). 정규식 분할은 파일경로/버전(v1.2.3)/약어에서 오분할, 키워드맵은 영어전용 → CB의 KO/EN 이중언어와 충돌.
- **G12 MD-event-log (reject)**: 전제 오류 — memanto의 summary/conflict 잡은 헤딩을 **파싱하지 않고** 원문 MD를 네트워크 LLM에 투입; 헤딩 파싱은 ASCII viz만. CB는 이미 hash-chained JSONL audit + audit-index 미러로 **더 강한** 구조화 저장 보유. MD 스키마 격식화는 약한 두 번째 저장을 추가할 뿐.
- **G14 LangGraph 어댑터 (reject)**: 미션 역전(CB는 install-into로 inward 통합). store.py는 memanto 네트워크 SdkClient에 결박 — 재사용 가능 글루는 487줄 중 ~80줄. SearchOp.query가 갈 곳 없음(recall_lessons는 토큰중첩만). 수요 없음.

## 5. 정직성 caveats

- **클라우드/Moorcheh 의존**: memanto의 회상·합성·시계열·충돌의 실효 코드는 거의 전부 Moorcheh API + 호스티드 LLM 래퍼다(`direct_client.answer.generate`, similarity_search, namespaces). CB의 무네트워크 핫패스 제약상 **로직 이식이 아니라 아이디어·포맷 차용**이 전부다. "graft size"를 과대평가하지 말 것.
- **벤치/신뢰도 미검증**: provenance×검증×나이감쇠 신뢰도 모델과 anti-poisoning 검증은 memanto가 **성능 사유로 비활성 출하**("Skipped for speed", legacy/ 이동). 즉 originating 프로젝트에서 프로덕션 증거가 없는 기능들이다. 더 핫한 CB 컨텍스트로 옮기는 것은 입증 책임이 역전된다.
- **코드-에이전트 전이 미검증**: memanto 13타입 온톨로지·정규식 분류표·키워드 추출기는 회의/관계/커밋먼트/EOD 같은 **개인비서 의미론**에 튜닝됨. CB의 architecture/pattern/skill/precall/workflow/bug 같은 load-bearing 코드 타입엔 규칙이 0개. "타입 사다리를 우선순위로"라는 행동 효과(G8)도 측정 증거 없는 프롬프트 가설이다.
- **위협모델 불일치**: anti-poisoning(G13)은 멀티테넌트·에이전트 주장 채팅 fact 표면을 방어. CB는 단일사용자·로컬·의도적 결정 기록 → 포이즈닝 표면이 작다.
- **파일럿 게이팅**: G4는 advisory-only·env 플래그·dark 출하로 시작하고 "기존 distill 게이트 대비 marginal yield"를 먼저 측정해야 한다. G2/G3는 존재하지 않는 필드(type/confidence) 위 광고 필터를 출하하지 말 것 — 기존 필드(kind/status/tags/source)에 타이트하게 스코프.
