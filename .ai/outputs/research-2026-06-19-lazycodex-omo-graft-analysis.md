# lazycodex(oh-my-openagent) → Code Brain 접목 분석 (2026-06-19)

> 출처: `https://github.com/code-yeongyu/lazycodex` (thin distribution) + 본체 submodule `https://github.com/code-yeongyu/oh-my-openagent` (b5b9058, MIT/CLA-gated) 임시 클론 후 다단계 워크플로 분석.
> 통계: OmO 8개 서브시스템 + Code Brain 3개 영역 병렬 매핑 → 15개 후보 합성 → 15개 후보별 적대적 검증 → 합성. 에이전트 28, 도구호출 774, 토큰 2.9M.
> 대상: Code Brain 메인테이너 · 모든 판정/근거는 OmO·CB 소스 직접 확인 기준.

---

## 1. lazycodex / oh-my-openagent란, 그리고 왜 핫한가

OmO(oh-my-openagent, CLI alias `lazycodex`)는 Codex CLI와 OpenCode를 같은 후크 계약 위에서 구동하는 멀티-하네스 에이전트 배포물로, 핵심은 **"끝났다"를 모델 자기보고로 인정하지 않는 자기참조 ULTRAWORK 루프**다. 진짜 차별점은 (a) `<promise>DONE</promise>` 자기선언이 루프를 끝내지 않고 종료 토큰을 `VERIFIED`로 바꿔 **편집 권한이 없는 read-only Oracle 서브에이전트의 독립 승인**을 강제하는 2단계 verified-completion, (b) 진행상태를 JSON이 아니라 **사람이 읽는 plan의 `- [x]` 체크박스(Boulder)** 에 두어 크래시·compaction 후 디스크에서 그대로 복원하는 설계, (c) LazyVim식 "LLM이 알아서 설치하는" zero-config 배포 + 5개 npm bin alias 팬아웃이다 — 코드 품질도 실측상 높다(TS strict, factory, co-located given/when/then 테스트, 200-LOC soft cap, compare-and-set 상태 커밋, idempotent scaffolding).

**실체 vs 과장**: 그래프트 가치는 *메커니즘*에 있고 *배선*에는 없다. 루프 두 구현(OpenCode `session.idle` 후크 / Codex `Stop` 후크 + goal CLI)은 각 하네스 라이프사이클과 `multi_agent_v1.spawn_agent`에 강결합돼 그대로는 포터블하지 않다. Oracle 검증도 암호학적·테스트기반 증명이 아니라 "Oracle이 read-only이고 attribution이 검증된다"는 구조적 가드 위에서 **모델이 VERIFIED를 정직하게 낸다고 신뢰**하는 것이라, 오정렬된 Oracle은 도장만 찍어줄 수 있다(Codex측 `--quality-gate-json` artifact 게이트가 그보다 강함). "60K stars·토큰버너 lore·AI가 공개적으로 짓는 레포" 류 서사는 *성장 메커니즘*이지 *기술 검증*이 아니며, 벤치마크는 미검증이다.

---

## 2. 우선순위 접목 후보 (adopt > pilot > watch > reject)

| # | 후보 | OmO 출처 | CB 접목 대상 | 판정 | 노력 | CB 기존중복 |
|---|------|----------|--------------|:----:|:----:|------|
| G1 | 독립 Oracle / acceptance-evidence 완료 게이트 | ulw-loop 2단계 완료 + DoneClaim→AdversarialVerify | `loop_engineering.py`(record_verdict/`_finish`) + `route_floor.py` | **pilot** | medium | Partial — 리뷰어 pass게이트·독립패밀리 soft점수·anti-gaming win 有; typed evidence·결정론적 재실행 無 |
| G2 | 내구 per-plan 진행 상태머신(체크박스=상태) | Boulder `- [ ]`/`- [x]` 파싱 + JSON 사이드카 | `session_resume.py` + 신규 `plan_state.py` | **pilot** | medium | Partial — 큐 inbox/processing/done/dead+lease=request단위 크래시복원 有; per-step 진행 無 |
| G3 | Stop-후크 continuation 루프 드라이버 | start-work-continuation `decision:block` + context-pressure 차단 | `hooks.py`(Stop) + `loop_continuation.py` | **pilot** | medium | No(plan-gated 재주입); 단 advisory `autonomous_harness` 자매품 有 |
| G4 | per-task 순서형 모델 fallback 체인 + 에러분류 | category-scoped fallbackChain + retryable/fatal 분류 | `route_floor.py`/`loop_engineering.py`/`loopd.select_worker` | **pilot** | medium | Partial — 선호패밀리 리스트·학습 floor·quota de-prioritize(dormant) 有; per-task 즉시 fallback·재시도분류 無 |
| G5 | 실제 LSP-as-MCP(스텁 배선) + diagnostics | 2-프로세스 LSP(thin MCP + warm daemon) | `lsp.py`(multilspy 배선) + `mcp_server.py` | **pilot** | large | No — 계약/탐지/캐시 스캐폴드만, 백엔드 미배선; codegraph는 Python-only |
| G9 | Read 트리거 walk-up 디렉토리 컨텍스트 주입 | agents-md-core `tool.execute.after`(Read) | `hooks.py` PostToolUse(Read) | **pilot** | medium | Partial — SessionStart 최근접파일 읽기 有; read트리거 walk-up·per-dir dedup·compaction재주입 無 |
| G11 | behavior-lock-before-edit 정리 디시플린 | remove-ai-slops(회귀테스트 GREEN 선행) | `kits/.../skills/safe-refactor`+`lean-review` 보강 | **pilot** | small | Partial — safe-refactor/lean-review/lean-debt 有; behavior-lock 불변식·KEEP/REFACTOR 룰셋 無 |
| G12 | LIGHT/HEAVY tier triage → evidence regime 스케일 | ultrawork tier triage 프롬프트 | `autonomous_harness.py`(directive) + `loopd` 분류 재사용 | **pilot** | small | Partial — 승인게이트용 risk분류 有; evidence깊이를 tier로 스케일 無 |
| G7 | 단일 plan에서 병렬 서브에이전트 fan-out/join | start-work/ultraresearch 병렬 burst+reduce | `loop_engineering.submit_wave`+barrier | **watch** | medium | Partial — `survey_plan`(decompose+bound+gate)·warm pool·큐 有; barrier-join 無 |
| G8 | 디렉토리 복잡도 점수 기반 계층 AGENTS.md *생성* | $init-deep 8-factor 점수 | `codebase_map.py`+신규 generator | **watch** | large | Partial — 루트 생성·flat depth-1 map 有; 점수·재귀·per-dir 생성 無 (OmO는 알고리즘 코드 無, 프롬프트표만) |
| G10 | review-work N-lane 병렬 리뷰 스킬 | review-work(all-must-pass/INCONCLUSIVE) | `kits/.../skills/code-review` 확장 | **watch** | small | Partial — 코드레벨 pass게이트·독립리뷰어 선택 有; 멀티레인 패키징 無 |
| G14 | reset-vs-continue + CAS 반복 커밋 | ralph-loop reset/continue + incrementIteration | `loopd`/`loop_engineering` | **watch** | small | Partial — rename-CAS·lease_id가드·MAX_ATTEMPTS 有; reset-vs-continue 노브만 無, G3 의존 |
| G15 | hashline stale-edit auto-remap 피드백 | hashline-core remaps Map + 컨텍스트창 | `hashline.py`(verify_anchors) | **watch** | small | Partial — `actual` 필드로 동일라인 보정앵커 이미 반환; 컨텍스트창·moved-line만 無, 소비자 無 |
| G13 | ultraresearch 포화 연구 EXPAND 루프 | ultraresearch 재귀 수렴 | (신규 스킬) | **reject** | medium | Mostly — `autoresearch/`(sandbox 실행·citation 검증) 有; EXPAND 수렴만 신규 |
| G6 | post-edit 주석 staleness 체커 후크 | comment-checker PostToolUse | `hooks.py`+`ast_verify`/`astgrep` | **reject** | large | No — 주석 staleness 無; 단 OmO 탐지는 *닫힌 다운로드 Go 바이너리*, offline AST 아님 |

---

## 3. ADOPT / PILOT 후보 상세 (무엇을 · 어디에 · 첫 단계)

> adopt 등급 없음. OmO 메커니즘은 모두 일부 결합도/재작성 비용·CB 기존중복이 있어 단계적 pilot이 정직한 상한.

### G1 — 독립 evidence 완료 게이트 (pilot, medium)
**핵심 정정**: OmO Oracle은 read-only LLM이 `VERIFIED` 토큰을 내는 것이지 *결정론적 재실행이 아니다*. 후보가 제안한 "rubric 명령 재실행"은 OmO 그래프트가 아니라 후보의 *자체 발명*이며, 그게 오히려 진짜 가치다.
- **무엇을**: (a) `reviewer_verdict` 스키마에 옵트인 typed evidence(`{command, observed, artifact_path}`, `repro`, `confidence`) 추가, (b) empty-evidence/tests-only pass 거부, (c) checklist/rubric를 기존 sandbox로 재실행하는 결정론적 acceptance runner.
- **두 함정**: typed-evidence 스키마 단독은 anti-gaming 연극이다(boolean 위조나 array 위조나 동일) — **머신 검증과 짝지어야** 가치 발생. cross-family **hard** 게이트는 거부: `route_floor.py:36`이 단일 에이전트 호스트(solo light user)를 명시 타깃 → 2번째 패밀리 없는 최소설치에서 루프가 완료 불가가 됨(회귀). family는 soft 유지(`loopd.py:180` 이미 그러함).
- **첫 단계**: `loop_engineering.py` `record_verdict()`(L215-250)에 옵트인 evidence 파라미터 + `_verdict_has_evidence()` 헬퍼(L474 옆), `_finish()`(L364-366) 기존 `LoopPhaseError` 유지하며 `acceptance_required` 플래그시 evidence·non-tests-only 요구를 `_phase_issues`로 표면화. `reviewer_required=False`인 `self_improve.py`는 영향 없게 옵트인. 신규 `ai_core/acceptance.py`는 기존 sandbox_execute(offline·approval-gated)로 명령 재실행 후 `eval_loop.record_case`에 합류. 테스트: `test_route_floor.py` 갱신 + 신규 `test_acceptance.py`(empty 거부 / tests-only 거부 / 재실행 pass→complete / 최소설치 여전히 complete).

### G2 — 내구 per-plan 진행 상태머신 (pilot, medium)
**정정**: 후보의 헤드라인("CB는 멀티스텝 체크포인트/재개 불가")은 과장. CB는 이미 request 단위로 inbox/processing/done/dead + lease + `_recover_expired_locked` + atomic tmp→rename로 "메모리 불신, 디스크에서 진실 재유도"를 갖췄다. **진짜 빈칸은 좁다**: 한 goal 내부의 *순서형 per-step 진행*이 없음(`checklist`는 binary present/absent 계약일 뿐).
- **무엇을**: Boulder의 작은 커널만 — 체크박스 파서 + 파일/사이드카 분리 — 를 **기존 큐의 자식(per request_id)** 으로 추가. 큐를 대체/경쟁시키지 말 것(이중 진실원이 최악).
- **금지**: Boulder의 `## TODOs`/`## Final Verification Wave` + `N.`/`FN.` 넘버링은 Prometheus/Sisyphus 저작 컨벤션 — import하면 죽은 요구사항 상속. CB 자체 최소 규약(`## Steps`)을 정의.
- **첫 단계**: 신규 `ai_core/plan_state.py`(순수 파이썬 체크박스 파서, 매 읽기마다 디스크 재유도). plan markdown은 `.ai/memory/sessions/<request_id>/plan.md`(큐 lease/recovery가 단일 복원 권위 유지), step 타이머는 gitignore된 `.ai/cache/` 사이드카(`machine_id` 패턴, `session_resume.py:182-210`). 모든 step label·evidence path에 `redact_value`(handoff.json은 git추적+Mac↔VPS 이동). read-only 우선 배선: `session_resume._handoff_for_snapshot`(L303-316)가 `next_step_label` fallback, `autonomous_harness.context_line`(L84-92)에 `{completed}/{total}` 표면화. `_expected_phase` 결합은 검증 후로 연기. 검증: `test_self_improve.py` 미러 + `make doctor`.

### G3 — Stop-후크 continuation 루프 (pilot, medium · **G2 선행 필수**)
- **무엇을**: turn-end에 plan(G2) remaining>0 AND context-pressure 마커 없음 AND continuation 진행중 아님이면 다음-step directive 재주입. 루프 조건을 모델 자기평가가 아니라 파싱된 plan으로 외부화.
- **세 가지 정직한 제약**: (1) G2 미존재 → 지금 그래프트는 무의미. (2) **3개 타깃에 일반화 안 됨**: Antigravity는 Stop 후크를 작업 전에 죽임(`hooks.py:180-185`) → agy에선 구조적으로 불가, 정직하게 2/3로 스코프. (3) 무인 재주입 = 런어웨이/토큰소진 리스크 → CB 자체 바운드 필요(세션당 max-continuations 카운터, wall-clock cap, 기본 OFF 옵트인).
- **첫 단계**: 신규 `ai_core/loop_continuation.py`의 순수함수 `continuation_directive(payload, root) -> str|None`: `stop_hook_active`·context-pressure 마커·`antigravity`·`remaining==0`이면 None, else CB-네이티브 directive(OmO Prometheus/boulder/multi_agent_v1 텍스트 금지, 새로 작성). `hooks.py` Stop 분기(L1807-1829, 현재 `{"continue":True}`)에서 **보안 block을 덮지 않는** 조건 + 옵트인 env flag일 때만 `decision='block'`+`reason` 설정(L1808 기존 wire 재사용). 테스트: remaining>0→block / ==0→continue / stop_hook_active→no block / agy→no block / context-pressure→no block / 카운터초과→no block / 보안block 비덮어쓰기.

### G4 — per-task 순서형 fallback 체인 + 에러분류 (pilot, medium)
**실제 빈칸 확인**: transient fault(rate-limit/quota/overload)시 워커가 `loop fail`→즉시 dead-letter(`loop_engineering.py:211-212`)이거나 `_TASK_FAULT` 정규식에 걸려 `neutral` 점수만 받고 **재시도/에스컬레이트 안 됨**. quota_exhausted 상태는 선언만 되고 **쓰는 코드가 없음**(dormant).
- **무엇을(포터블 코어, offline)**: OmO `runtime-fallback-error-classifier.ts:26-52`의 정규식 셋(rate.?limit, quota, overloaded, 429/503/529, CJK 변형)과 retryable-vs-fatal 분류. 순수 문자열 매칭(network 無, 3 하네스 일반화).
- **두 설계 위험**: (1) `route_floor._TASK_FAULT`(L50-56)가 이미 rate-limit/429/503 매칭 → **단일 진실원** 필요(classify_outcome에서 한 번 산출). (2) 무한재시도 방지 — 재시도 re-queue도 `MAX_ATTEMPTS=5` 증가·준수, `tried_agents`/`tried_tiers` 기록해 `select_worker`가 다음 패밀리로 전진.
- **첫 단계**: `route_floor.is_transient_fault(reason)`(또는 신규 `error_classifier.py`, ~40 LOC, 무동작변화) → `_finish`(L352-396)에서 transient+attempts<MAX면 inbox/ 재큐(`_recover_expired_locked` L445-447 미러, attempts++ + tried 리스트) → `select_worker`(loopd.py:148-189) tried 디랭크 → 소진/cap시 dead-letter → transient 재큐시 `worker_registry.update_worker`로 `quota_state='quota_exhausted'` 기록(dormant `loopd.py:184` 활성화). 테스트: transient→재큐, fatal→즉시dead, 체인소진→dead, tried 디랭크.

### G5 — 실제 LSP-as-MCP (pilot, large)
**갭 정정**: codegraph는 "Phase 1: Python only"(`codegraph.py:1`) — JS/TS/Go/Rust가 아예 없음. `lsp.py`는 전부 `reason='lsp_backend_not_wired'` 반환하는 고아 스텁(cli/mcp/doctor 0 참조), `multilspy`는 미선언 의존.
- **무엇을(스코프 한정)**: 기존 Python 계약 뒤에 multilspy `SyncLanguageServer`를 **per-call**(daemon 없음), **명시 호출 전용**(후크 없음)으로 배선. **포터블 코드 0** — OmO는 TS/Node 손수짠 JSON-RPC + unix-socket daemon이라 *설계 아이디어만* 이식, 재구현이지 port 아님. **daemon·PostToolUse diagnostics-injection 후크는 거부**(후크는 매 턴 다발, pyright/tsserver cold-start 초단위 → 반복 안정화한 `hot_path_slo` doctor 체크 플레이크, 커밋 168dff0·8e46f59).
- **첫 단계**: `lsp.py`에서 1개 언어(Python/pyright-langserver) `find_references`/`goto_definition`만 context-manager per-call, LSP Location→기존 `{path,line,column,preview}`(L160-168)·5s TTL캐시 재사용, `_unavailable()` fallback 유지(`test_lsp.py` 계약 보존). `pyproject.toml`에 multilspy를 **옵셔널 extra**(하드의존 금지). `mcp_server.py`에 `code_find_references`/`code_goto_definition` 등록(code_graph_* 옆 L67-110, dispatch L822-840). `doctor.py`에 `lsp_available()` INFO 프로브(절대 fail 금지). `test_lsp.py`에 `shutil.which('pyright-langserver')` 가드 실테스트. **OUT**: daemon·refcount·idle-reaper·rename·diagnostics 후크 전부 연기.

### G9 — Read 트리거 walk-up 디렉토리 컨텍스트 주입 (pilot, medium)
**진짜 빈칸**: 손으로 쓴 중첩 AGENTS.md/CLAUDE.md는 SessionStart 코스맵의 CWD-버킷 최근접 파일로만 보일 뿐, 깊은 서브트리에서 *편집중인 파일의 실제 디렉토리*는 안 보임. OmO 코어는 ~6 순수함수(~145 LOC, realpath 봉인·set dedup) — language-agnostic·offline·demand-driven.
- **세 정직한 이유로 adopt→pilot**: (a) **load-bearing 미검증** — PostToolUse `additionalContext`가 Claude/Codex에서 실제로 모델에 소비되는지 미확인(make-or-break). (b) **Antigravity no-op** — `agents_md.py`가 존재하는 이유 자체가 agy는 command 후크로 컨텍스트 주입 불가 → 3중 2만 충족. (c) compaction 재주입은 가장 비포터블·고배선(PreCompact만 있음). `context_budget.py`는 ranked-line fitter지 prose truncator 아님 → 작은 char-cap은 신규.
- **첫 단계**: **Phase 0(프로덕션 코드 0)**: 실제 Claude/Codex 세션에서 PostToolUse `additionalContext` 소비 경험적 확인. 미소비면 **REJECT**. Phase 1(기본 OFF): 신규 `ai_core/dir_context.py`(`findAgentsMdUp`+realpath 봉인 port, skipRoot=True, process-level lru + MAX_DEPTH). `CONTEXT_INJECTION_HOOKS`(L27) **확장 금지** — PostToolUse 별도 분기(redact 분기 L1763 앵커), `AI_DIR_CONTEXT=1`일 때 file_path 추출(L1474/1979/2026 패턴)·session_id 키 in-memory Set dedup·char-cap·`[Directory Context: <path>]` 블록을 `additionalContext`로. SessionStart 이미 표면화한 파일은 skip. compaction-invalidation은 Phase 2 연기.

### G11 — behavior-lock-before-edit 디시플린 (pilot, small)
**정정**: cb_target `.agents/skills/`는 틀림(거기엔 source-command-* + ssh.md). CB 스킬은 `kits/global-agent-kit/.claude/skills/`. **배포 갭**: `install.sh:454`는 스킬을 `~/.claude/skills/`에만 설치 → Codex는 rules/AGENTS.md, Antigravity 스킬 타깃 없음. CB 스킬은 15-26줄 terse 한국어 vs OmO 318줄 영어 → 충실한 port는 이물질.
- **무엇을(디시플린만)**: 회귀테스트 GREEN을 삭제 전 선행 불변식, SKIP-not-GUESS, KEEP/REFACTOR(경계검증·I/O예외·WHY주석 보존), safest→riskiest, 3회실패 revert+escalate.
- **첫 단계**: 신규 스킬보다 **기존 보강 선호**. `safe-refactor/SKILL.md` step 2-3을 진짜 behavior-lock 게이트로: "동작 고정 회귀테스트 GREEN 선확인, 커버리지 없으면 최소 회귀테스트 추가 후 편집, GREEN 베이스라인 불가면 중단·보고"; 금지 목록에 "확신 없으면 GUESS 말고 SKIP"; "같은 파일 3회 실패시 중단·보고". `lean-review` KEEP에 "경계 입력검증·I/O 예외처리·WHY 주석은 과설계로 보지 않는다" 1줄. **DROP**: Phase4 mailbox·check-no-excuse-rules.py·250-LOC 하드맨데이트. `scripts/validate.sh` 실행.

### G12 — LIGHT/HEAVY tier triage → evidence 스케일 (pilot, small)
**빈칸**: `loopd.py`엔 `infer_risk`(L78-91)·`assess_tier`(L105-121) 두 분류기가 있으나 **워커 디스패치+승인파킹에만** 도달, 완료/evidence 게이트엔 미도달. `autonomous_harness`는 별개의 프로젝트레벨 `_mode()`와 flat `COMPLETION_TARGET=0.95`만.
- **무엇을(작은 커널만)**: tier 라벨을 기존 harness directive에 주입해 위험변경에 더 무거운 evidence bar를 명명. OmO 3번째 트리거 리스트 import 금지 — `_GATED`/`_COMPLEX` 정규식 **재사용**.
- **정직한 한계**: 미강제 prose(모델이 무시 가능) — CB의 결정론·측정 에토스와 충돌, 측정신호 없어 `prompt_growth` ratchet 밖. 그래서 작은 directive 클로즈에만 한정.
- **첫 단계**: `autonomous_harness.py`에 순수 `evidence_tier(payload)` → `from .loopd import infer_risk, assess_tier`, `high` OR `best`면 heavy(신규 정규식 없음). `directive()`(L100-107)에 tier별 1-2줄 클로즈 추가(OmO tmux/curl/Playwright 채널 리스트 금지). payload는 `hooks.py:2174-2176`(UserPromptSubmit) 경유. 테스트: "add oauth"→heavy, "fix typo"→light, tier 헬퍼가 loopd 위임(정규식 미중복).

---

## 4. 기각 / 이미보유 (짧게 왜)

- **G6 (reject)**: OmO comment-checker 탐지는 *offline AST+diff가 아니라* 런타임에 GitHub Releases에서 받는 **닫힌 Go 바이너리**(`downloader.ts`). 빌릴 수 있는 건 ~20줄 가드뿐. CB `ast_verify.py`는 보안검증기, Python `ast`는 주석을 버림 → 제안한 재사용 베이스 부적합. 저정밀 net-new-comment 플래그는 매 편집 false-positive 생성기.
- **G13 (reject)**: 후보 전제 오류 — 실제 `autoresearch/`는 sandbox 실행(`loop.py`)·citation faithfulness(`verify.py`)를 이미 보유. 신규는 EXPAND 수렴 루프뿐 — 프롬프트 관용구지 인프라 아님. OmO SKILL은 460줄 하네스 결합, "탐색바운딩 무시·15워커" 권한 클로즈가 CB 게이팅 철학과 정면 충돌.
- **G7 (watch)**: decompose+bound+gate는 이미 `autoresearch/orchestration.py::survey_plan`(MIN/MAX/HARD fanout, MCP 노출). "결정론적 reducer로 의미적 요약 join"은 카테고리 오류 — 의미적 reduce는 LLM 몫. 빠진 작은 조각은 barrier-join뿐이고, 그걸 필요로 하는 CB 소비자가 아직 없음.
- **G8 (watch)**: OmO는 **점수 알고리즘 코드가 없음**(SKILL.md 표를 LLM이 눈대중). 진짜 재사용 코드(walk-up)는 CB가 이미 보유. 결정론적 per-dir 생성은 SKILL.md가 금하는 generic boilerplate를 양산. codegraph Python-only라 centrality factor가 TS/Go에서 미측정.
- **G10 (watch)**: INCONCLUSIVE-never-PASS의 load-bearing 절반(pass-only 게이트·blocked 구분·독립리뷰어 선택)이 이미 `loop_engineering.py`/`loopd.py`에 **코드로** 존재(프롬프트보다 강함). 남은 델타는 thin fan-out 래퍼. QA-executor/context-miner 레인은 CB offline 제약과 불일치.
- **G14 (watch)**: 3 프리미티브 중 2개 이미 동등 — inbox→processing rename(queue_lock 내)이 CAS, `_finish`/`record_verdict`의 lease_id 불일치 거부가 중복커밋 차단, `MAX_ATTEMPTS=5`가 cap. reset-vs-continue만 빈칸이나 G3(미착수) 의존 + OmO reset은 OpenCode `session.create({parentID})` 결합 → CB tmux+file-handoff 모델에 무대응.
- **G15 (watch)**: 후보 전제 오류 — CB는 mismatch에 거부하지 않음(`verify_anchors`는 어느 write 가드에도 미배선, 수동 리포터). 보정앵커는 이미 per-row `actual` 필드로 반환됨. MCP 노출도 없음(`code_read_hashline`만). 남은 델타(컨텍스트창·moved-line)는 **호출 소비자가 없음** → 실제 edit/PreToolUse hashline 가드가 생기기 전엔 무의미.

---

## 5. 정직성 caveats

1. **Codex/하네스 플랫폼 결합도**: 루프 본체·planner·start-work는 OpenCode `session.idle` + Codex app-server 후크(Stop/SubagentStop/UserPromptSubmit/PreToolUse) + `multi_agent_v1.spawn_agent`/`/goal`에 강결합. *메커니즘*은 포터블해도 *배선*은 아님. 모든 그래프트는 CB-네이티브 재작성.
2. **Antigravity 비대칭**: G3·G9는 Antigravity Stop/command-후크 제약으로 **3중 2만** 충족. "installs into all three" 프레이밍은 이 경로들에서 과장 — 정직하게 스코프해야.
3. **TS→Python 재작성 비용**: G5(LSP daemon)·G2(Boulder 파서)·G15(hashline)는 OmO가 TS, CB가 Python. **포터블 라인 0** — 설계/정규식만 이식하고 재구현. 노력 보정에서 G5는 large 유지, G2·G4·G9·G12는 통합표면(큐 결합·doctor·redaction·3-하네스 테스트)이 진짜 비용.
4. **토큰비용 철학 차이**: OmO는 토큰버너(100→500 반복, 3-15 워커 팬아웃, "탐색바운딩 무시"). CB는 smallest coherent change·offline hot-path·승인 후 fan-out. 무인 continuation(G3)·tier evidence(G12)는 CB 바운드(카운터·wall-clock cap·기본 OFF) 없이는 철학 위반.
5. **검증의 한계(가장 중요)**: OmO Oracle은 read-only LLM이 `VERIFIED`를 *정직하게* 낸다고 신뢰 — 암호학·테스트 증명 아님. G1의 진짜 가치는 OmO의 LLM Oracle이 아니라 그 위에 얹는 **결정론적 acceptance 재실행 + 머신검증 evidence**에 있으며, 이는 후보의 발명이지 OmO 그래프트가 아님(정직하게 라벨).
6. **벤치 미검증**: OmO의 "60K stars·AI-built-in-public" 서사는 성장 메커니즘이지 기술 검증이 아님. verified-completion·루프의 실제 작업 성공률 벤치마크는 본 분석에서 미검증.
7. **라이선스/도구 제약**: OmO는 CLA-gated(LICENSE.md), 진행중 multi-harness 리팩터 사본. MIT→Apache-2.0 *아이디어* 차용은 무방하나 워크플로/포맷(Prometheus N./FN. 넘버링 등)은 import 금지.
