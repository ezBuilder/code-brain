# Code Brain Architecture

Architecture snapshot for the Code Brain runtime. ai_core source, scripts, and GitHub Actions workflow were checked directly; round mappings are updated as hardening rounds land.

## 1. 전체 프로세스 맵 (Process-level)

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                          USER (operator / dev)                              │
└─────────────────────────────────────────────────────────────────────────────┘
        │                      │                          │
        │ runs                 │ runs                     │ launches
        ▼                      ▼                          ▼
 ┌────────────────┐    ┌────────────────┐        ┌────────────────────┐
 │  Claude Code   │    │   Codex CLI    │        │  Operator Shell    │
 │  (long-lived)  │    │  (long-lived)  │        │  (ai CLI invokes)  │
 └────────┬───────┘    └────────┬───────┘        └─────────┬──────────┘
          │                     │                          │
          │ hook events         │ hook events              │ ai <subcmd>
          │ (SessionStart,      │ (SessionStart,           │
          │  UserPromptSubmit,  │  UserPromptSubmit,       │ uv run ai ...
          │  PostToolUse,...)   │  PostToolUse,            │
          │                     │  Stop)                   │
          │ MCP JSON-RPC        │ MCP JSON-RPC             │
          │ (stdio)             │ (stdio)                  │
          ▼                     ▼                          ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                     .ai/bin/ shim layer (bash/ps1)                          │
│  ai-hook  →  uv run ai hook              (sync, hot path, ≤200ms target)    │
│  ai-mcp   →  uv run ai-mcp serve-stdio   (long-lived JSON-RPC)              │
│  ai      →  uv run ai <cmd>              (operator CLI)                     │
└─────────────────────────────────────────────────────────────────────────────┘
                          │
                          │ (always uv-sandboxed:
                          │  UV_PROJECT_ENVIRONMENT=.ai/runtime/.venv
                          │  UV_CACHE_DIR=.ai/cache/uv  no $HOME mutation)
                          ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                     ai_core runtime (Python 3.11)                           │
│                                                                             │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐    │
│  │  hooks.py    │  │ mcp_server.py│  │   cli.py     │  │  doctor.py   │    │
│  │ handle_hook  │  │ handle_req   │  │ argparse     │  │ run_checks() │    │
│  │ ≤200ms SLO   │  │ JSON-RPC 2.0 │  │ +reject_ci   │  │ 34 checks    │    │
│  └──────┬───────┘  └──────┬───────┘  └──────┬───────┘  └──────┬───────┘    │
│         │                 │                 │                 │            │
│  ┌──────┴─────────────────┴─────────────────┴─────────────────┴───────┐    │
│  │           POLICY GATE  (policy.py: is_ci, reject_ci_write)         │    │
│  │   WRITE_COMMANDS = {render,trust,upgrade,migrate,index,queue,      │    │
│  │     inbox,notify,obs_write,diagnostics_write,memory,audit,worker}  │    │
│  │   CI 환경에서 write 호출 → exit 16 (PERMISSION_DENIED)             │    │
│  └────────────────────────────────────────────────────────────────────┘    │
│         │                 │                 │                 │            │
│  ┌──────┴───────┐  ┌──────┴───────┐  ┌──────┴───────┐  ┌──────┴───────┐    │
│  │  worker/     │  │  redact.py   │  │  memory.py   │  │  report.py   │    │
│  │  scheduler   │  │ secret patts │  │ append_event │  │ status_report│    │
│  │  +lock+ipc   │  │ +path masks  │  │ append_audit │  │ release_gate │    │
│  │              │  │              │  │ (hash chain) │  │ summary v2   │    │
│  └──────────────┘  └──────────────┘  └──────────────┘  └──────────────┘    │
└─────────────────────────────────────────────────────────────────────────────┘
```

<!-- code-brain-contract: doctor-check-count=34 -->

## 2. Hook hot-path (Claude/Codex 공통)

```
Claude/Codex agent fires event (e.g. UserPromptSubmit)
         │
         │ JSON payload → STDIN
         ▼
.ai/bin/ai-hook                                   ← bash shim
         │
         │ exec uv run ai hook
         ▼
cli.py "hook" handler
         │
         ▼
hooks.handle_hook(root, hook_name, payload)
         │
         ├── is_ci() OR payload["dry"] is True ───┐
         │                                        │
         │ NO                                     │ YES
         ▼                                        ▼
   memory.append_event(root, event)        mode = ci-fast-path /
         │                                        local-dry-fast-path
         │ writes to .ai/memory/events/blob       persisted = False
         │ + appends events.jsonl
         ▼
   redact_value(response)
         │
         │ {ok, hook, mode, persisted, elapsed_ms,
         │  target_ms=200, additionalContext}
         ▼
   STDOUT JSON  →  agent receives additionalContext
                   (injected into model context for SessionStart /
                    UserPromptSubmit; logged-only for PostToolUse/Stop)

INVARIANTS:
  • elapsed_ms ≤ 200ms (hot_path_slo doctor check)
  • redact_value 적용 (secret 패턴 + 절대 경로 마스킹)
  • 네트워크 호출 0 (AGENTS.md hard constraint)
  • CI 환경: write 0 (CI fast-path 분기)
```

### 2.1 턴 요약 넛지 (turn_report)

Claude Stop은 `additionalContext`와 block으로, Antigravity는 `decision:"continue"`로 모델을
재진입시킬 수 있다. 그러나 둘 다 보안·미완료 차단과 같은 제한 예산을 소비하고 Codex 호스트 간
이식성도 일정하지 않다. 완료된 작업의 문장 형식만 고치려고 이 채널을 쓰면 불필요한 모델 턴과
토큰을 만든다. 그래서 상시 Response 규칙으로 같은 턴의 간결 요약을 요구하고, 정량 넛지는 다음
`UserPromptSubmit`에 한 번만 넣는다.

```
Stop / SessionEnd
         │
         ▼
_spawn_turn_report(root, agent)          ← DETACHED (git 비용을 훅 예산에서 분리)
         │                                  측정: git diff --shortstat
         │                                  code-brain 36ms / navio 92ms / blurivo 687ms
         ▼
turn_report.write_snapshot()
         │  .ai/ 는 pathspec 으로 제외 (CB 자신의 audit/cache 쓰기가 오염원)
         │  이전 측정 대비 절대 델타만 기록 → 최초 1회는 baseline (넛지 없음)
         ▼
.ai/cache/turn_report.json

UserPromptSubmit (다음 턴 시작)
         │
         ▼
turn_report.nudge_line()                 ← 임계 초과일 때만 한 줄, 1회 소비
         │  기본 임계: 파일 8개 또는 churn 200줄
         ▼
build_context() 앞부분에 삽입            ← tail 절단에 대비해 의도적으로 앞쪽

INVARIANTS:
  • 요약 문구 때문에 Stop 을 block 하지 않는다 (보안·미완료 채널 예산 보존)
  • 훅 인라인에서 git 을 호출하지 않는다 (detached only)
  • LLM 요약을 저장하지 않는다 — git 사실만 (_auto_milestone_on_stale 와 동일 원칙)
  • 넛지 1줄 ≤ 120B (UserPromptSubmit 예산 2048B, tail 절단이므로 예산 경쟁)
  • AI_TURN_REPORT=0 → 측정·주입 모두 완전 정지
  • 비-git 디렉터리 / 오류 → 스냅샷 없음, 넛지 없음 (fail-soft)
```


### 2.2 주입 예산 구성 (`_fit_sections`)

예산 초과 시 결합 문자열의 tail을 자르던 방식은 뒤쪽 섹션을 통째로 삭제했다. 실측
폐기율은 code-brain 57%, navio 76%였고, 모든 프로젝트에서 `learned_prompt`(prompt
growth 산출물)와 `session tail`이 완전히 누락됐다. 즉 자동 성장한 규칙이 모델에
도달한 적이 없었다.

```
sections = [fast_path, Response, Search, Read, cb-turn, cb-stale, decisions,
            todos, session tail, learned rules, lessons]
         │
         ▼
_fit_sections(sections, max_bytes)
         │
         ├── 1) 보호 섹션(지시문) 예산 우선 확보
         │      fast_path / Response / Search / Read / cb-turn / cb-stale / learned header
         │      단, 보호분이 예산을 넘으면 보호를 포기(기아 방지)
         │
         └── 2) 나머지(증거)를 순서대로 채우고,
                첫 초과 섹션만 줄 경계로 clip 후 중단

INVARIANTS:
  • 결과 <= max_bytes (호스트 계약)
  • 예산은 상한이며 정확히 채우는 quota가 아니다 (잘린 지시문 금지)
  • 섹션 순서 보존, 동일 입력 → 동일 출력
```

### 2.3 저장 한도는 회수 가능 바이트에 적용

`pinned`(git 추적 / 추적 소스가 참조 / 명시적 `.keep`)는 삭제 거부권이며 사용자
결정이다. 이 바이트를 cap에 산정하면 집행기가 영구 실패한다 — blurivo `.ai/tmp`
546MB 중 475MB가 `.keep` 픽스처 3개여서 모든 sweep이 지울 수 있는 것을 다 지우고도
`ok=False`, doctor가 영구 적색이었다.

`pinned`와 `undetermined`는 분리한다. `tracked_known=False`(git 조회 실패)는 "확인
불가라 삭제 보류"일 뿐 사용자 결정이 아니므로, 이를 pin으로 묶으면 비-git
워크스페이스에서 한도 검사가 통째로 무력화된다.

```
                    삭제 보류   cap 산정
pinned              O          제외
undetermined        O          포함
그 외               X          포함
```

### 2.4 Stop 훅은 트랜스크립트를 인라인 스캔하지 않는다

`prompt_growth._output_tokens`가 `obs.usage_report`를 인라인 호출했다. 이는 호스트의
모든 codex/claude 세션 파일을 재파싱한다 — blurivo 실측 8.06s(507 codex + 125 claude
세션, JSON 623,836행), code-brain 6.5s. `tick`이 growth cooldown(기본 5턴)마다 도달하므로
**5턴 중 1턴의 턴 종료가 6~8초 정지**했다. 오류는 하나도 나지 않았다. 그냥 멈췄다.

이 값은 규칙에 기록되는 **출처 정보**(`baseline_tokens`)일 뿐이며 어떤 판정에도
쓰이지 않는다. ratchet은 `baseline_obs_avg`/`_recent_output_avg`(작은 로컬 jsonl)로
판정한다. 따라서 인라인 경로는 TTL 캐시 읽기로 바꾸고, 스캔은 분리 자식이 채운다.

```
Stop ──▶ prompt_growth.tick()
           └─ _output_tokens()  ← 캐시 히트 or 0. 절대 스캔 안 함
Stop ──▶ _spawn_tokens_cache_refresh()   ← DETACHED, 쿨다운 3600s
           └─ refresh_output_tokens_cache()
                └─ compute_output_tokens()   ← 느린 집계는 여기만
                     └─ .ai/cache/prompt_growth_tokens.json  (TTL 3600s)
```

캐시가 차갑거나 낡거나 깨졌으면 0을 반환한다 — 기존 fail-soft 경로와 동일하다.
규칙이 졸업하려면 `RATCHET_WINDOW` 턴이 필요하므로 시간 단위 staleness는 어떤 결과도
바꾸지 못한다.

### 2.5 조기 종료 하드 차단 (`completion_guard`)

모델은 할 일이 남아 있어도 턴을 끝낸다. 기존 방어선 `loop_continuation`은 설치된 모든
프로젝트에서 **죽어 있었다**. 원인 세 개, 전부 실측:

1. 트리거가 `plan_state.active_summary(root)`의 `remaining>0` 하나뿐인데 그런 plan을
   유지하는 사람이 없다 — blurivo 140개 중 1개, navio 32개 중 0개, actraflow 1개 중 0개,
   fluxwright 0개, vera-harness 0개, code-brain `active_summary()` → `None`.
2. `AI_LOOP_CONTINUATION=1`은 소스 kit의 `.claude/settings.json`에만 있었고
   `install-into.sh`의 `merge_claude_settings`는 `hooks`만 병합하고 `env`는 병합하지
   않았다. 그래서 소비 프로젝트는 Stop 훅은 등록됐는데 `env`가 `None`이었다(blurivo,
   navio 확인). 플래그와 훅이 **한 번도 같은 곳에 존재하지 않았다.**
3. 결과: `ai hook Stop --json`이 `AI_LOOP_CONTINUATION` 유무와 무관하게
   `decision=None, continuation=None`.

해결은 "다른 자기보고를 믿는다"가 아니다. 모델이 말로 우회할 수 없는 신호만 읽는다.
근거: Evidence-Carrying Termination(arXiv:2608.23623, 2026-08-22) — 모든 주장을 트레이스
증거에 결박하는 typed certificate가 있을 때만 COMPLETE를 허용하고, 근거 없는 조기 종료
40/66 → 0/66, 정당한 완료는 92/132 → 97/132로 비열등. CB의 오프라인 대응물은 트리와
영속 저장소에서 완료를 **파생**한다.

신호 우선순위(첫 히트만 사용 — 이유 문구가 다음 행동 하나를 명시해야 한다):

| # | 신호 | 읽는 것 | 게임 가능성 |
| --- | --- | --- | --- |
| 1 | `plan` | 이번 요청 중 생성·변경된 plan의 미체크 스텝 | 체크박스 조작 가능(사용자 가시) |
| 2 | `conflict` | 요청 baseline 뒤 새로 생기거나 내용이 바뀐 unmerged/`^<{7}` | 없음 — git이 쓴다 |
| 3 | `syntax` | 요청 baseline 뒤 새로 생기거나 내용이 바뀐 `.py` 구문 오류 | 없음 — 파서 판정 |
| 4 | `marker` | 요청 baseline 뒤 추가된 TODO/FIXME/XXX/HACK | 마커를 안 쓰면 회피 |
| 5 | `verification` | PostToolUse 원장에 host call-id·성공 exit·편집 content hash로 결박된 관련 check | 약한 명령 위조·검증 뒤 재편집은 거부 |
| 6 | `acceptance` | 요청 시작 offset 뒤 append된 `kind=="acceptance"` 실패 행 | 재기록 가능(감사 남음) |

의도적으로 **신호가 아닌 것**: dirty worktree(blurivo는 평시 913 modified), 미커밋 변경
(커밋은 사용자 판단), 메모리 open todo(정의상 이월 백로그 — 블록하면 영구 해제 불가).

요청 귀속이 핵심이다. `UserPromptSubmit` 또는 Antigravity의 첫 `PreInvocation`
(`invocationNum==0`)에서 기존 conflict/syntax/marker/plan/acceptance를 작은 hash/offset baseline으로
잡는다. 이후 Stop은 baseline과 달라진 증거만 현재 요구의 부채로 본다. 따라서 평소 dirty 파일이
수백 개인 소비 프로젝트에서도 이전 작업을 새 요구 탓으로 돌리지 않는다. baseline이 없거나 손상됐거나
30분 TTL을 넘으면 차단 근거가 없으므로 양보한다. marker는 최대 40개
후보 path로 제한한 `git diff HEAD --unified=0` **한 번**으로 추가된 줄만 본다. 삭제 전용 tracked
파일을 untracked처럼 전체 스캔하던 오탐도 제거했다. 40개를 넘으면 부분 marker 결과로 차단하지 않는다.
파일당 diff는 6개 변경 파일에서 68ms였고 선형 증가한다 —
§2.4가 만든 것과 정확히 같은 종류의 hot-path 비용이다. 이 최적화로 `_signal_marker`는
68ms → 15.5ms, `detect()`는 175ms → 99.5ms가 됐다.

PostToolUse 활동 ledger는 git·network 없이 편집/검증의 요청별 순서를 private atomic sidecar에
기록한다. 최대 32개 편집 path의 현재 content hash를 누적하고, host가 발급한 `tool_use_id`(Antigravity는
`conversationId+stepIdx`)와 성공 exit를 결박한다. 코드·미상 편집은 명시 allowlist의
static/build/test 수준(강도 2 이상), 문서·설정 편집은 docs check 또는 `git diff --check`(강도 1 이상)를
마지막 편집 **뒤에 성공**해야 한다. 실패 exit, `echo pytest` 같은 문구 위조, 검증 뒤 재편집, content
hash 불일치는 완료 증거가 아니다. call-id/path/hash/lock/state를 완전하게 읽지 못하면 원장 신호만
fail-open한다.

**기본 ON**(`AI_COMPLETION_GUARD=0`이 킬 스위치). opt-in이 위 2번 원인 그 자체였다.
세 가지 호스트 설정 형식(`.claude/settings.json`, `.codex/hooks.json`,
`.agents/hooks.json`)에 걸친 env 배관에 활성화가 의존하면 대부분의 설치에서 죽는다.

기본 ON이 안전한 이유는 **모든 레일이 양보(yield) 쪽**에 있기 때문이다:

- `stop_hook_active`는 무조건 양보하지 않는다. Claude가 재진입한 Stop임을 뜻할 뿐 완료 증거가
  아니다. 증거 fingerprint가 바뀌는 동안은 계속하고, 같은 fingerprint가 2회 반복되거나 공유
  상한 8회에 닿으면 양보한다. 이 방식이 공식 8회 host cap 안에서 실제 진척은 허용하고 자기
  루프는 유한하게 만든다.
- 컨텍스트 압박(`context_pressure`/`compact_pending`/`near_compaction`) → 양보.
- Antigravity의 system/error/max-step/non-idle stop → 양보. 정상 `model_stop`은 공식
  `decision:"continue"` 계약으로 재진입한다.
- host/harness가 인증한 `user_input_required`/`awaiting_user`/`approval_required` → 양보.
  물음표는 증거가 아니다. `?` 하나로 양보하던 구현은 모델의 불필요한 질문을 범용
  우회로로 만들었다.
- 무진척 에스컬레이션: 같은 fingerprint(신호 JSON + 해당 증거 파일/plan의 정확한 내용)가
  `MAX_STALL_REPEATS`(2)회 반복되면 양보. 막힌 모델을 계속 찌르는 건 토큰 화재다.
- `git diff --stat`은 쓰지 않는다. 내용이 바뀌어도 행 증감이 같으면 동일해서 실제 진척을
  stall로 오판했다. 증거 하나만 해시하므로 더 정확하고 더 싸다.
- `loop_continuation`과 **공유**하는 repository/worktree + host-session 키의 요청별 예산
  (`_bump_counter`, 최대 8회/30분) 소진 → 양보. Claude에는 모델을 재진입시키지 않는
  `systemMessage`로 상한 소진을 한 번 알린다.
  `UserPromptSubmit`에서 카운터/무진척 상태를 리셋해 긴 세션의 이전 작업이 새 요구를
  무력화하지 못하게 한다.
- 레포 밖, 플래그 off, 임의 예외 → 양보.

```
Stop / SubagentStop
   │  decision == "block"?  ── yes ─▶ 보안 결정 유지. guard 미조회.
   ▼ no
loop_continuation.continuation_directive()   ← plan 기반(기존)
   │  None?
   ▼
completion_guard.guard_directive()
   │  레일 통과 + 신호 있음
   ▼
{"decision": "block", "reason": "cb-guard[...]: ...", "completion_guard": true}
   └─ 내부 decision은 항상 block; host-aware wire가 각 host 규격으로 투영
```

호스트별 와이어는 `hook_wire_output(response, request_payload)`가 **원본 입력**으로
호스트를 판별해 만든다. 이전 CLI는 모든 host에 `codex_wire_output`을 무조건 적용했다.
이는 Antigravity에서 조용히 극성을 뒤집은 P0였다.

| host | 미완료 Stop | 완료 Stop |
| --- | --- | --- |
| Claude / Codex | `{"decision":"block","reason":...}` | `{"continue":true}` |
| Antigravity 2.0 / CLI 1.1.x | `{"decision":"continue","reason":...}` | `{"decision":"stop"}` |

Antigravity Stop 설정 역시 matcher-group이 아닌 **direct handler list**여야 한다. `doctor`가
이 shape과 `.ai/bin/ai-hook` command를 강제한다. `_STOP_LIKE_HOOKS`로 Stop/SubagentStop을
묶는 이유는 Claude/Codex의 SubagentStop이 Stop과 같은 decision 계약을 쓰고,
서브에이전트의 조기 종료도 같은 결함이기 때문이다.

보장 범위는 정직하게 제한한다. **정의된 증거 클래스에서는 동일 입력이 host별로 동일한
continue/stop 결정을 내리는 결정 등가성을 golden test로 100% 고정**한다. 탐지 재현율은 관측 가능한
PostToolUse와 bounded tree scan 범위에 한정되며, 미지원/구형 host가 훅을 호출하지 않거나 증거가
불완전하면 차단하지 않는다. 표현되지 않은 요구의 의미적 완료를 100% 판정하는 oracle이라고 주장하지
않는다.

`doctor`의 `completion_guard` 체크가 세 축으로 생존을 증명한다: 모듈 임포트 + 킬 스위치
상태, 실제 트리에서 `detect()` 무예외 실행, 그리고 존재하는 각 호스트 설정에
Stop/SubagentStop이 실제로 등록되어 있는지. 이전 guard가 doctor를 전부 통과하면서 죽어
있었기 때문에 관측 가능성 자체를 계약으로 못박았다.

## 3. MCP stdio surface (Claude/Codex 공통)

```
agent ─── JSON-RPC 2.0 line ──▶ .ai/bin/ai-mcp ──▶ uv run ai-mcp serve-stdio
                                                        │
                                                        ▼
                                          mcp_server.serve_stdio(root)
                                                        │
                                          for each line: handle_request
                                                        │
        ┌───────────────────────┬──────────────┼──────────────┬──────────────┐
        ▼                       ▼              ▼              ▼              ▼
  memory_query              code_query     context_pack    ai_status   ai_request_rebuild
  search.query              search.query   search.context  worker.ipc.health  search.rebuild
        │                       │              │              │              │
        │ READ-ONLY              │              │              │              │ ← 유일 write 경로
        └───────────────────────┴──────────────┴──────────────┘              │
                                                                              ▼
                                                                    enqueue rebuild job
                                                                    (worker queue)

  모든 응답 → redact_value 통과 → STDOUT (jsonrpc=2.0, id, result|error)
  STDERR 로그 별도 (stdout은 JSON-RPC 전용, batching 없음)
```

## 4. Worker / Queue / Lock layer

```
┌──────────────────────────────────────────────────────────────────────────┐
│                     worker process (singleton)                           │
│                                                                          │
│  worker/lock.py                                                          │
│  ┌────────────────────────────────────────────────────────────────────┐  │
│  │  acquire():  O_EXCL create .ai/cache/run/worker.pid                │  │
│  │              {pid, owner, hostname, acquired_at}                   │  │
│  │              if exists → lock_status() → stale auto-clear or       │  │
│  │                          WorkerAlreadyRunning(exit 75)             │  │
│  │  cross_host check: 다른 host pid → 절대 force-clear 금지            │  │
│  └────────────────────────────────────────────────────────────────────┘  │
│                                                                          │
│  worker/scheduler.py  (모든 mutation은 queue_lock 안)                    │
│  ┌────────────────────────────────────────────────────────────────────┐  │
│  │  enqueue(priority, kind, payload)                                  │  │
│  │    └─▶ .ai/memory/queue/{p0..p3}-<ts>-<hex>.json (atomic .tmp→repl)│  │
│  │                                                                    │  │
│  │  lease_next(worker_id, priority?):                                 │  │
│  │    1. _sweep_if_due → recover_expired (≥30s 간격)                  │  │
│  │    2. queue_lock acquired                                          │  │
│  │    3. pending → processing/ rename, attempts++                     │  │
│  │    4. lease_id = secrets.token_hex(16), TTL=300s                   │  │
│  │                                                                    │  │
│  │  complete/fail(job_id, lease_id) → 검증 후 unlink/dead 이동        │  │
│  │                                                                    │  │
│  │  recover_expired():                                                │  │
│  │    • lease 만료 + attempts < max_attempts(3) → pending 복귀        │  │
│  │    • lease 만료 + attempts ≥ max → dead/ + audit dead_letter_promote│  │
│  │    • state: .ai/cache/run/queue.recovery.json                      │  │
│  │                                                                    │  │
│  │  list_dead(limit, since) → operator inspection                     │  │
│  └────────────────────────────────────────────────────────────────────┘  │
│                                                                          │
│  worker/ipc.py  (envelope auth)                                          │
│  ┌────────────────────────────────────────────────────────────────────┐  │
│  │  envelope = {protocol_version=1, token, root_id, root_hash,        │  │
│  │              machine_id_hash, request_id}                          │  │
│  │  validate_envelope: token / root_hash 정합 검증                    │  │
│  │  CI 모드 token = "__ci_readonly_no_worker_token__" (write 차단)    │  │
│  │  health(envelope) = {ok, protocol_version, methods[…]}             │  │
│  └────────────────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────────────────┘

queue 디렉터리:
  .ai/memory/queue/             ← pending root (P0..P3 우선순위 prefix)
  .ai/memory/queue/processing/  ← lease 중인 job
  .ai/memory/queue/dead/        ← max_attempts 초과 또는 명시적 fail
  .ai/memory/queue/.tmp/        ← atomic write staging
```

## 5. Persistence / 데이터 흐름

```
                     SOURCE OF TRUTH (tracked in git)
                     ─────────────────────────────────
  .ai/AGENTS.md ─────render.py──▶ AGENTS.md (shim)
                              ──▶ CLAUDE.md (shim)
                              ──▶ .ai/generated/manifest.json (sha 추적)

  .ai/config.yaml  →  config.load_config(root)  →  runtime 전역
  .ai/trust/machines/*.pub.toml  →  machine_id_hash 산정 입력
  .ai/secrets/*.enc.yaml         →  secrets_store (SOPS+age, ciphertext only)

                     WORKER WRITES (single-writer invariant)
                     ──────────────────────────────────────
  .ai/memory/audit/*.jsonl     ← memory.append_audit (hash-chain prev_sha)
  .ai/memory/audit-index.jsonl ← doctor.check_audit_index 검증
  .ai/memory/events/...        ← hooks.handle_hook (local 모드만)
  .ai/memory/queue/...         ← scheduler enqueue/lease/complete/fail
  .ai/memory/decisions.jsonl   ← 의사결정 로그
  .ai/memory/todos.jsonl       ← 작업 큐
  .ai/memory/session-current.md← 세션 narrative (worker single writer)
  .ai/memory/sessions/         ← 세션 archive
  .ai/memory/inbox/, outbox/   ← human-in-loop 승인 큐

                     CACHE (gitignored, rebuildable)
                     ────────────────────────────────
  .ai/cache/code.sqlite        ← FTS5 index (search)
  .ai/cache/run/worker.pid     ← singleton lock
  .ai/cache/run/worker.token   ← IPC 인증 토큰
  .ai/cache/run/queue.lock     ← fcntl flock (queue mutation)
  .ai/cache/run/queue.recovery.json ← lease recovery state
  .ai/cache/uv/                ← uv 의존성 캐시
  .ai/cache/diagnostics/       ← redacted bundle zip
  .ai/cache/upgrade/           ← rollback backup
  .ai/cache/remote-memory/     ← optional Cloudflare remote-memory pull cache

                     RELEASE ARTIFACTS (dist/, gitignored)
                     ────────────────────────────────────
  dist/code-brain-X.Y.Z.tar.gz          ← deterministic Python tarfile (Round 83)
  dist/code-brain-X.Y.Z.tar.gz.sha256
  dist/code-brain-X.Y.Z.manifest.json
  dist/code-brain-X.Y.Z.sbom.json
  dist/code-brain-X.Y.Z.provenance.json
  dist/code-brain-X.Y.Z.release-notes.md
  dist/release-gate.summary.json   ← schema v2 (Round 87, dep_advisory 포함)
  dist/dep-advisory.json           ← pip-audit advisory only

  package.sh는 현재 X.Y.Z family 생성이 끝난 뒤 retention을 dry-run→apply하고,
  다른 버전의 `code-brain-*` release family만 제거한다. unrelated dist 파일과
  현재 version family는 보존하며 release-gate가 stale family 0을 재확인한다.
```

## 6. Release-gate pipeline (현재 머지 상태 기준)

```
make release-gate  →  ./scripts/release-gate.sh
   │
   ├─ env-check.sh                  (uv/python/git/age/sops 존재)
   ├─ preflight.sh --check-only     (fresh clone 가능성 검증, R5)
   ├─ lint.sh                       (script + py compile)
   ├─ lockfile-check.sh             ★Round 85: uv lock --check (drift 차단)
   ├─ bootstrap.sh                  (uv sync → ai render → ai doctor → pytest)
   ├─ smoke.sh                      (CLI 표면 sanity)
   ├─ docs-check.sh                 (docs needles + CI write rejection 회귀)
   ├─ package.sh                    (deterministic tar + stale family retention)
   ├─ ai_core.report retention      (stale dist family 0 재확인)
   ├─ reproducibility-check.sh      ★Round 83: 두 번 build → sha256 동치
   ├─ verify-artifacts.sh           (checksum/manifest/SBOM/provenance/notes)
   ├─ install-check.sh              (extracted package 실행)
   ├─ artifact-tamper-check.sh     (변조 감지)
   ├─ rollback-drill.sh             ★Round 76: upgrade plan→apply→rollback round-trip
   ├─ dep-advisory.sh               ★Round 80: uvx pip-audit advisory only
   ├─ ai doctor --strict --json     (17 checks)
   ├─ ai report status --json       (release_ready & artifacts.all_current)
   └─ git status --short empty?     (tree clean invariant)
            │
            └─▶ "release gate ok"

doctor checks (현재):
  layout, config, gitattributes, sqlite_features, manifest, trust, jsonl,
  audit_index, hot_path_slo, secret_scan, redaction_self_test,
  bootstrap_preflight, diagnostics, worker_lock, queue_lease_recovery,
  queue_age, audit_chain   ← 17개

GitHub Actions (.github/workflows/release-gate.yml):
  jobs:
    parity:           matrix [ubuntu-latest, macos-latest]
      • permissions: contents: read
      • persist-credentials: false, fetch-depth: 0
      • Confirm CI write rejection (exit 16 probe)
      • ./scripts/release-gate.sh
      • upload release-gate.summary.json (14d)
      • upload release artifacts (main push, ubuntu only, 30d)
    summary-observe:  needs: parity
      • download both summaries
      • uv run python scripts/summary-parity.py UB MAC
        (schema_version=2 강제, canonical subset 동치 단언)
```

## 7. 보안/정책 enforcement points

```
┌───────────────────────────────────────────────────────────────────────────┐
│ Layer            │ Mechanism                       │ Failure mode         │
├───────────────────────────────────────────────────────────────────────────┤
│ CI write block   │ policy.reject_ci_write          │ exit 16              │
│ Hook hot path    │ HOT_PATH_TARGET_MS=200          │ doctor fail          │
│ No-network hot   │ AGENTS.md hard constraint       │ code review only     │
│ Secret in tree   │ doctor.check_secret_scan        │ exit 12 / strict fail│
│ Worker singleton │ worker/lock.py O_EXCL pidfile   │ exit 75              │
│ Cross-host lock  │ lock_status.cross_host          │ force-clear 거부 14  │
│ Queue mutation   │ queue_lock fcntl LOCK_EX        │ contention block     │
│ Lease auth       │ ipc.validate_envelope token/sha │ UNAUTHORIZED         │
│ Audit tampering  │ memory.append_audit prev_sha    │ doctor audit_chain   │
│ Summary schema   │ report.assert_summary_schema    │ ValueError → fail    │
│ Cross-OS parity  │ scripts/summary-parity.py       │ exit 1/2             │
│ Dep CVE          │ scripts/dep-advisory.sh         │ advisory only (0)    │
│ Lockfile drift   │ scripts/lockfile-check.sh       │ exit 1               │
│ Archive byte-eq  │ scripts/reproducibility-check.sh│ exit 1               │
│ All redaction    │ redact.redact_value (재귀)      │ secret_self_test     │
│ MCP outbound     │ mcp_server: redact_value 적용   │ schema-only response │
│ Diagnostics zip  │ obs.diagnostics + 화이트리스트  │ doctor diagnostics   │
└───────────────────────────────────────────────────────────────────────────┘

Exit code 표:
  0 OK / 1 GENERIC / 2 USAGE / 10 CONFIG_INVALID / 11 POLICY_DENIED
  12 SECRET_DETECTED / 13 MANIFEST_DRIFT / 14 WORKER_UNAVAILABLE
  15 INCOMPATIBLE_VERSION / 16 PERMISSION_DENIED / 75 WorkerAlreadyRunning
```

## 8. 라운드별 hardening 누적 매핑

```
Round 70  worker singleton + queue_lock         (lock.py, scheduler.py)
Round 72  queue lease recovery sweep            (scheduler._sweep_if_due)
Round 73  GitHub Actions release-gate parity    (release-gate.yml + summary)
Round 74  worker stop --force CLI               (cli.py worker stop)
Round 75  queue oldest-age metrics + doctor     (queue_age check)
Round 76  rollback drill                        (rollback-drill.sh)
Round 77  dead-letter inspection                (cli queue dead)
Round 78  cross-OS summary parity               (summary-parity.py)
Round 79  ai obs health-summary                 (cli obs health-summary)
Round 80  dep-advisory artifact                 (dep-advisory.sh)
Round 81  bootstrap idempotency drill           (bootstrap-idempotency.sh)
Round 82  summary schema lock v1                (assert_summary_schema)
Round 83  archive reproducibility               (Python tarfile + repro check)
Round 84  audit hash chain                      (memory.append_audit prev_sha)
Round 85  uv.lock drift gate                    (lockfile-check.sh)
Round 87  summary schema v2 + dep_advisory      (report.py + parity 갱신)
Round 88  session auto-start                    (session.py + cli session start)
```

MVP 미구현 영역(embeddings, vector search, L3 LSP precision adapters, daemon lifecycle)은 의도적으로 backlog gating — 다이어그램에서 제외.
