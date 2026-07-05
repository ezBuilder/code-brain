# Code Brain 개선안 — 3소스 통합 비판 분석 (2026-06-21)

> 소스: ① 내 딥리서치(net-new, 적대검증, CB v0.4.0 실제 코드 기준) ② 외부 보고 A(codebrain-deep-research-report) ③ 외부 보고 B(Code Brain 개선 딥리서치, v0.5.0 비전).
> 목적: 합치고, **틀린 것 / 이미 보유 / 제약 위반**을 가려내고, 진짜 증분만 우선순위화.

## 0. 메타 — 출처 신뢰도 (먼저 알아야 할 것)

- **외부 A**: 본문에 "공개 웹에서 CB 저장소를 식별 못 해 내부 기준선으로 가정"이라 명시 → **CB 실제 코드를 못 봄**. 인용이 전부 `citeturn…` 미해소 토큰이라 **개별 검증 불가**. 방향성 참고용.
- **외부 B**: arXiv URL 인용은 있으나 일부 future-dated·검증불가(예: SMART 2605.24938), 벤치 수치 다수 미검증. CB 코드 미열람(도시에 기반).
- **내 딥리서치**: 소스 21·주장 99·적대검증 25 → 통과 2(cAST, snapcompact 반쪽). CB v0.4.0 **실제 코드** 기준.

→ 결론: 외부 둘은 "도시에만 보고 추론"이라 **CB가 이미 구현한 걸 신규로 오인**하는 오류가 핵심 문제다.

## 1. 외부 보고의 잘못된 점

### 1a. 이미 CB가 보유 → "신규"로 잘못 제안 (가장 흔한 오류)
- **LSP-as-MCP (B §7.2)** → **이미 v0.4.0 출하**(`code_find_references`/`code_goto_definition`, multilspy). B가 모름.
- **OmO 오케스트레이션/tmux 워커풀 (B §7.2)** → 이미 loopd 워커풀+파일큐+route_floor 보유.
- **GEPA 리플렉티브 ratchet (B §6)** → 이미 prompt_growth/eval_loop/self_improve가 동등 개념(2026-06-17 채택).
- **코드 임베더 SFR/CodeXEmbed (B §3.1)** → 2026-06-17에 이미 평가(어댑터 채택보류, 재벤치 전제). 신규 아님.
- **memanto 타입메모리/recall/conflict (B §4.1)** → **v0.3.0 이미 접목**(memory_recall/list_decisions/memory_conflicts). B가 모름.
- **A-MEM / Zep temporal (B §4.2-4.3, A Memory DAG)** → 2026-06-17에 watch로 평가됨(이득 미미/저비용 대체).
- **요구사항-코드 그래프 / CodeRAG (A §1)** → 2026-06-17 "CodeRAG bigraph pilot"과 중복.
- **sleep-time compute (B §5)** → 2026-06-17 pilot/watch로 평가됨.

### 1b. 제약 위반 / 사실 오류
- **"memanto ITS를 로컬 통합"(B §4.1)** → **틀림**. ITS=Moorcheh **클라우드** 의존(내 memanto 분석서 확정). 90ms 로컬 ITS 엔진은 오픈소스로 존재하지 않음 → 이식 불가. B의 핵심 전제 오류.
- **FastCoder 투기적 디코딩(B §7.1)** → CB는 하네스라 모델 forward-pass를 소유하지 않음 → draft-verify 디코딩 **적용 불가**. (SpecAgent식 사전 컨텍스트 투기는 CB `speculative.py`+sleep-time과 중복.)
- **Anthropic Context Editing API(B §2.2)** → 실재 기능이나 **Anthropic API 전용** → CB 오프라인·Codex 핫패스엔 직접 적용 불가. B도 regex fallback로 인정 → **부분만 유효**.
- **버전 주장(A §외부비교)** → "OmO/lazycodex v4.12.1·teammode v4.12.0(6/20)"은 내가 6/19 클론한 lazycodex=v0.2.2/marketplace v4.11.1과 불일치 → **미검증/의심**.
- **A 인용 전부 `citeturn` 미해소** → 벤치·날짜 주장 전반 검증 불가.

### 1c. 외부 둘 다 놓친 것 (내 딥리서치만 발견)
- **cAST(구조인지 청킹, MIT astchunk)** → 두 보고 모두 누락. CB `search.py`에 가장 깔끔히 맞는 net-new.

## 2. 3소스 합의 (높은 신호 — 방향은 옳다)
1. **리트리벌을 "구조"로 한 단계 올려라**: cAST(나) + DraCo 데이터플로우(B) + 요구사항-코드 그래프(A). 셋 다 "임베더 교체보다 구조적 retrieval".
2. **메모리에 supersede/contradict/temporal 관계 추가**: Memory DAG(A) + Zep bi-temporal(B). CB는 이미 supersedes_id+fold+conflict 보유 → **엣지(contradicts/derives-from/expires-at)만 증분**.
3. **context-rot 방어**(이미 CB 방향: context_pack/budget).

## 3. 통합 후 진짜 검토가치 (skepticism 통과 · 우선순위)

| 후보 | 출처 | CB 접목 | 판정 | 근거/주의 |
|---|---|---|---|---|
| **cAST 구조 청킹** | 내 DR | `search.py` 청크경계 | **adopt-pilot** | MIT·오프라인·tree-sitter, GIST Recall@5 +4.3(상한), EM 무승부→자체 재측정 |
| **DraCo 데이터플로우 retrieval** | B | `codegraph` 확장(Assigns/Refers/As) | **pilot** | ACL2024·정적분석·오프라인. Python 위주→재측정 필요, 100x/F1+3.27%는 미검증 |
| **Memory DAG 엣지(supersede/contradict/expires-at)** | A+B | `memory.py`/`memory_conflicts` | **pilot(증분)** | CB가 절반 보유. 그래프DB 없이 SQLite 메타로. 회상 시 관계·최신성 제약 우선 |
| **MCP resources/prompts 노출** | A | `mcp_server` | **watch→pilot** | plan/context_pack/eval/handoff을 tool이 아닌 **resource**로. 표준 정합·저위험. auth/enterprise 주장은 검증요 |
| context-rot 오프라인 stripping | B | `context_budget` | **watch** | Anthropic-API판 말고 정규식 로컬판만. CB context_pack과 델타 작음 |
| OTel 관측성 | A | `obs`/audit | **watch** | 로컬-퍼-레포 도구엔 과설계 위험·의존성↑. 감사≠관측 분리 아이디어만 차용 |

## 4. 기각 (통합)
- memanto ITS 로컬화(클라우드 의존), FastCoder 디코딩(하네스 부적합), A2A control plane(미성숙·스코프), 멀티모달 Record&Replay(비전·오프라인 충돌), snapcompact 소비(비전모델 필요).

## 5. 최종 권고
- **외부 보고들의 방향(구조적 retrieval + 메모리 관계화)은 옳다.** 그러나 구체 항목 다수가 **CB가 이미 한 것(LSP-MCP/OmO/GEPA/memanto/임베더)** 이거나 **제약 위반(ITS 클라우드·FastCoder 디코딩·Context Editing API)**.
- **진짜 증분은 3개로 수렴**: ① cAST + ② DraCo(검색 구조화) ③ Memory DAG 엣지(증분). + ④ MCP resources(저위험 표준화)는 차순위.
- 핵심 한 줄: "더 많은 모델/모듈"이 아니라 **"검색 구조 + 메모리 관계" 증분** — 이는 보고 A의 결론("더 강한 구조")과도 일치.
- 정직성: 위 3개도 **CB 자체 코퍼스 재측정 전엔 pilot**. 외부 벤치 수치(100x, +18%, 2.53x 등)는 미검증으로 의사결정 근거에서 제외.

*근거: 내 보고 `.ai/outputs/research-2026-06-21-codebrain-net-new-candidates.md`, `research-2026-06-1{7,8,9}-*.md`, tech-dossier. 외부 2건은 Downloads 첨부.*
