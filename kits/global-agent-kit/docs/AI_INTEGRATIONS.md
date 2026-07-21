# AI_INTEGRATIONS.md

Code Brain을 이 전역 규칙 키트와 접목하는 기준이다.

## Code Brain

Code Brain은 프로젝트별 `.ai/`를 source of truth로 쓰는 선택 통합이다.

흡수할 가치가 있는 부분:

- fresh-clone 검증 순서
- session start와 doctor 기반 smoke test
- secret scan과 health-summary
- 긴 출력 명령을 요약 저장하는 실행 경로
- 반복 작업에서 프로젝트별 스킬 후보를 추천하는 흐름
- cross-session decisions, todos, session notes
- durable 메모리 통합 회상(`memory recall`: 결정·실패·교훈·절차를 identifier-aware relevance×confidence×recency로 랭킹하고 temporal/provenance/relation/partial-scan 진단 반환) + 결정 필터 조회(`memory decision list`)
- 오프라인 충돌 탐지(`memory conflicts`: 모순되는 결정쌍을 advisory로 표시, decisions 미변경)
- 내구 plan 진행 상태머신(`ai plan`: 체크박스=상태, 디스크 재유도) + `AI_LOOP_CONTINUATION`일 때 Stop 훅이 plan 0 남을 때까지 재프롬프트(카운터·wall-clock cap)
- 완료 게이트: `loop submit --require-acceptance` + `loop acceptance`(rubric 명령 결정론적 재실행, sandbox·offline)로 reviewer pass를 머신 검증으로 보강
- per-task 모델 fallback: transient(rate-limit/quota/overload) 실패 시 dead-letter 대신 다른 패밀리로 재큐(MAX_ATTEMPTS 바운드)
- precise→syntactic 탐색(`code_find_references`/`code_goto_definition`/`code_workspace_symbols`: multilspy+언어서버 결과 우선, 없거나 실패하면 기존 schema-v11 code.sqlite를 read-only로 사용해 import alias·relative import·self/cls provenance와 call/name/attribute/import-binding exact ranges를 포함한 AST fallback, 동일 이름 정의 ambiguity 및 bounded completeness 표시, 인덱스가 없으면 생성하지 않고 explicit unavailable)
- Read 트리거 디렉토리 컨텍스트(**기본 ON**, `AI_DIR_CONTEXT=0`으로 끔): 편집 파일의 상위 AGENTS.md/CLAUDE.md를 세션당 1회 주입
- Memory DAG 엣지: `record_decision`에 contradicts/derives_from/expires_at 관계, 만료 결정은 surface 제외(include_expired로 우회)
- MCP resources(**기본 ON**, `AI_MCP_RESOURCES=0`으로 끔): plan/report/handoff/session을 `codebrain://…` 읽기전용 리소스로 노출(resources/list·read, 경로탈출 차단·redact)
- 오프라인 충돌 스캔(**기본 ON**, `AI_MEMORY_CONFLICT_SCAN=0`으로 끔): page_out 시 모순 결정쌍 advisory 기록
- cAST 구조 청킹(`AI_AST_CHUNK`, Python): stdlib ast 재귀분할+형제병합. **자가검증 게이팅** — `ai cast eval`이 자기 repo에서 default 청커 대비 recall 측정 후 이길 때만 ratchet이 자동 ON(무측정 변경 없음)
- pilot 발견성/일괄: `ai config pilots`(상태·enable/disable), doctor가 on/off 요약 노출
- 원격 메모리는 opt-in, project-scoped, hook hot path network-free 원칙

전역 규칙에 강제하지 않을 부분:

- `.ai/` 런타임 전체 설치
- Cloudflare remote memory
- Code Brain MCP server
- 프로젝트별 branch 정책

## Code Brain 사용 조건

프로젝트 루트에 `.ai/bin/ai`가 있으면 Code Brain 사용 가능으로 본다.

권장 smoke test:

```bash
.ai/bin/ai session start --agent claude --json
.ai/bin/ai session start --agent codex --json
.ai/bin/ai obs health-summary --json
.ai/bin/ai recommend skills --limit 5 --json
```

새 프로젝트에 설치할 때:

```bash
cd /Users/ezbuilder/workspace/code-brain
make install-into TARGET=/path/to/repo
```

## 적용 원칙

- 전역 규칙은 Code Brain이 없어도 동작해야 한다.
- 설치되어 있으면 적극 활용하되, unavailable/stale 상태에서는 일반 로컬 탐색으로 fallback한다.
- 외부 네트워크, 원격 메모리, worker dispatch는 사용자 요청이나 프로젝트 정책이 있을 때만 사용한다.

## Skill Steward

Code Brain이 설치된 프로젝트에서는 반복되는 작업 신호를 스킬 후보로 다룬다.

기본 흐름:

1. `SessionStart` 또는 명시 요청에서 추천 후보를 확인한다.
2. 후보가 있으면 id, slug, 설명만 사용자에게 제안한다.
3. 사용자가 승인한 후보만 `ai recommend skills accept <id>`로 설치한다.
4. 불필요하거나 중복인 후보는 승인 후 `ai recommend skills reject <id>`로 다시 제안되지 않게 한다.
5. 설치된 스킬이 낡았으면 근거를 제시하고 수정 승인을 받는다.

금지:

- 사용자 승인 없는 자동 accept, reject, uninstall
- 기존 사용자 작성 스킬 덮어쓰기
- evidence 없는 스킬 생성 제안
- 같은 slug나 같은 본문 hash의 중복 제안

전역 승격 기준:

- 여러 프로젝트에서 같은 패턴이 반복된다.
- 프로젝트 고유 경로, 비밀, 도메인 지식 없이 일반화할 수 있다.
- 전역 규칙 키트에 추가해도 Code Brain 없는 프로젝트가 깨지지 않는다.
- 사용자 승인 후 이 저장소에 반영하고 `make validate`로 검증한다.
