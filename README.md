# Code Brain

에이전트가 새 repo에 바로 붙일 때:
```bash
git clone https://github.com/ezBuilder/code-brain.git && cd code-brain
./scripts/install-into.sh install /path/to/project
make install-into TARGET=/path/to/repo
make upgrade-in TARGET=/path/to/repo
make uninstall-from TARGET=/path/to/repo
cd /path/to/project && .ai/bin/ai session start --agent codex --query "current task" --json && .ai/bin/ai doctor --strict --json
```

**Claude Code · Codex CLI · Google Antigravity** 세 코딩 에이전트가 **한 워크스페이스에서 같은 메모리·검색·정책·감사 인프라**를 공유하도록 만드는 repo-local 인프라.

`grep`/`rg`을 버리지 않고 위에 `BM25 + hashline 무결성 + MCP + hooks + cross-session memory`를 얹는 lexical-first 설계. **로컬, 오프라인, 네트워크 없이 hot-path < 200ms.**

---

## 무엇이 가능한가

| 시나리오 | 한 줄 효과 |
|---|---|
| `code_query "auth 흐름"` | BM25 FTS5 + 트리시터 함수/클래스 chunk → 랭킹 스니펫 5개 |
| `code_read_hashline file.py --start 10 --end 80` | `line:hash12\|content` 앵커로 stale 편집 자동 차단 |
| `record_decision`/`record_todo` | 결정·할 일이 SHA-256 prev_sha 체인으로 audit log에 append-only 기록 |
| `sandbox_execute "rg ..."` | 긴 출력은 디스크에 저장하고 30 lines summary만 토큰 컨텍스트에 |
| `agy -p "..."` / Codex / Claude Code | 어느 에이전트로 켜도 같은 `.ai/memory/` · 같은 hook · 같은 MCP 서버 |
| `obs trajectory` | 최근 세션의 loop / shallow exploration / over-exploration 진단 |
| `speculative_mine_patterns` | 과거 tool 호출 2-gram 패턴 마이닝 (PASTE 식 추측 실행 토대) |
| `lsp_find_references` | multilspy 설치 시 정확한 cross-file reference graph (없으면 graceful fallback) |

---

## 지원 에이전트

세 에이전트 모두 **동일 hook 스펙 + 동일 MCP 서버**를 본다. install-into.sh가 각 에이전트의 설정 파일을 자동 생성/머지.

| 에이전트 | 설정 위치 | hook 이벤트 |
|---|---|---|
| **Claude Code** | `.claude/settings.json` | PreToolUse · PostToolUse · SessionStart · SessionEnd · UserPromptSubmit · Stop · SubagentStart · SubagentStop · PreCompact · PostCompact · Notification · CwdChanged · ConfigChange · PermissionDenied · InstructionsLoaded · TaskCreated · TaskCompleted · FileChanged · PostToolUseFailure |
| **Codex CLI (OpenAI)** | `.codex/hooks.json` + `.codex/config.toml` | PreToolUse · PostToolUse · SessionStart · UserPromptSubmit · Stop · SubagentStart · SubagentStop · PreCompact · PostCompact · PermissionRequest |
| **Google Antigravity** | `.agents/hooks.json` + `.agents/mcp_config.json` + `.agents/skills/` | PreToolUse · PostToolUse · PreInvocation · PostInvocation · Stop |

> **Antigravity 주의**: Antigravity 1.0.x는 위 **5개 이벤트만** 지원한다(SessionStart/UserPromptSubmit 없음). hooks.json은 `{"<name>": {이벤트: [{matcher, hooks:[{type,command,timeout}]}]}}` 형태의 정규 스키마를 쓴다. 또한 command-hook의 stdout으로는 모델 컨텍스트를 주입할 수 없으므로, Claude/Codex가 SessionStart로 받는 메모리를 Antigravity는 **자동 로드되는 `AGENTS.md`의 관리 블록**(`ai_core/agents_md.py`)으로 동일하게 받는다.

Claude / Codex hook handler는 `.ai/bin/ai-hook` (Unix) / `.ai/bin/ai-hook.ps1` (Windows, `commandWindows` 필드로 자동 등록).

---

## 핵심 모듈 (모두 구현됨)

### 검색 / 코드 이해

- **BM25 FTS5 인덱스** (`.ai/cache/code.sqlite`) — contentless FTS5, 스니펫은 트래킹 소스에서 lazy fetch (캐시에 파일 본문 중복 저장 안 함).
- **트리시터 hybrid chunking** — Python/JS/TS/Go/Rust/C/C++/Java 등 함수·클래스 단위 chunk + 파일 단위 chunk.
- **Code graph** (`codegraph.py`) — callers/callees/symbol/hotspots MCP tool. 휴리스틱 기반.
- **LSP integration (PoC, `lsp.py`)** — multilspy 설치 시 pyright/gopls/tsserver 등을 sub-process로 띄워 `find_references`/`goto_definition`/`workspace_symbols` 노출. 없으면 `ok=false` graceful.
- **Chunk filter (PoC, `chunk_filter.py`)** — CODEFILTER 식 polarity 분류 (pos/neu/neg). 식별자 overlap, 코멘트-only, 길이 페널티로 음성 chunk 자동 제거.
- **Sandbox execute** (`sandbox.py`) — 긴 출력 명령은 sandbox에서 실행, 디스크에 저장 후 first/last lines만 응답. 토큰 컨텍스트 보호.

### 편집 무결성

- **hashline** (`hashline.py`) — 파일 슬라이스를 `<line>:<sha256[:12]>|<content>` 형식으로 읽기. 편집 직전 hash 검증으로 stale 편집 차단.
- **stream_guard** (`stream_guard.py`) — destructive_git, secret_leak, compound_pipeline 등 위험 패턴을 hook payload에서 스캔, 조건부 차단.
- **AST verify** (`ast_verify.py`) — 모델이 생성한 Python 소스에 forbidden import/call/sandbox escape 있는지 정책 게이트.

### 메모리 (cross-session)

- **append-only JSONL + SHA-256 prev_sha 해시 체인** — 모든 결정·todo·세션 노트가 변조 감지 가능한 audit chain에 들어감.
- **`record_decision` / `record_todo` / `close_todo` / `append_session_note` / `append_handoff`** — MCP tool · CLI(`ai memory handoff`) 모두 노출.
- **memory_tier (MemGPT-style)** — hot/warm/cold 분류 + page-out 신호. `cb-mem: hot=N warm=N cold=N`로 SessionStart에 자동 inject.
- **Session resume** — SessionStart hook이 **handoff(goal/next_step/plan/open_questions/blockers)** 를 최상단에 inject하고, 이전 세션의 최근 결정 5개·미완 todo 5개·`session-current.md` tail 8줄을 이어 inject.
- **Handoff (`ai memory handoff` / `append_handoff`)** — 멈추기 전 "무엇을/다음에 뭘"을 기록. `.ai/memory/handoff.json`(git-tracked)으로 머신·에이전트 간 이동.
- **Cross-machine 연속성** — `machine_id`(불투명 opaque id, gitignored 캐시, 호스트명 PII 미포함; opt-in `AI_MACHINE_LABEL`) provenance + 다른 머신 thread 재개 힌트. `cb-behind` 배너: 이미-fetch된 upstream ref만 읽어(핫패스 네트워크 0) 원격이 앞서면 경고. opt-in 자동 동기 `ai memory sync`(코드는 안 건드리고 `.ai/memory`만 커밋→pull --rebase[클린 트리]→push; `--loop` 데몬). `memory_sync.enabled` + `AI_REMOTE_FETCH=1`로 활성.
- **Audit chain repair** (`ai audit repair-chain`) — stash/머지로 prev_sha가 깨졌을 때 체인 재계산. (인덱스 재구성은 `ai audit rebuild-index`.)

### Hook 시스템

- **PreToolUse 자동 라우팅** — 광범위 grep/rg/find 같은 compound pipeline / long-output 명령을 자동 차단하고 `sandbox_execute` 또는 `code_query`로 대체 경로 안내.
- **PostToolUse 결과 가드** — 도구 출력에서 secret 패턴 탐지 후 `updatedToolOutput`으로 redact.
- **PreCompact / PostCompact** — context compaction 직전 session-resume snapshot 강제 저장 (`/compact`, `/clear` 후에도 메모리 유지).
- **SubagentStart context injection** — subagent에도 메인 세션과 같은 decisions/todos/session-tail 주입.
- **TaskCreated / TaskCompleted** — Claude Code의 `TaskCreate` API와 Code Brain `record_todo` 자동 매핑.
- **FileChanged** → incremental index rebuild trigger.

### Precall 정책 (Bash 라우팅)

- **자동 마이닝된 룰** — `precall_recommend`가 누적 Bash 호출에서 자주 등장하는 long-output / compound / destructive 패턴을 마이닝해 룰 후보 제안.
- **active 룰** — 매칭 시 PreToolUse hook에서 차단 + 안내 메시지 + audit 기록.
- **dry-run 룰** — 차단 없이 관찰만, 100회 이상 매칭되면 `stuck_dry_run` 신호.
- **사용자 override** — 차단된 명령을 사용자가 그대로 실행하면 `record_user_override`로 학습.

### Speculative tool execution (PoC, `speculative.py`)

PASTE (arXiv 2603.18897) 식 패턴 마이닝. `mine_patterns`는 audit log에서 within-session 2-gram tool 호출 (`Read → Edit` support=5 등) 추출. 향후 PreToolUse hook이 predicted_next_tool을 미리 stage하면 -48.5% latency 보고치 적용 가능. **현재 PoC 상태 — 마이닝/예측/hit-rate 추적만, hot-path inject는 미적용.**

### Trajectory 진단 (PoC, `trajectory.py`)

TRAJEVAL 식 fine-grained 진단. `obs trajectory --json [--session-id X]`로:
- **efficiency**: total_events, unique_tools, tool_repeat_rate, tools_per_minute, dominant_tool
- **failures**: loop_suspected, shallow_exploration, backtrack_evidence, over_exploration
- 16,758 trajectory 연구 (모든 에이전트가 필요 함수의 22× 검토)와 같은 over-exploration 신호도 자동 탐지.

### MCP 서버

`.ai/bin/ai-mcp` (stdio JSON-RPC). 노출 도구 (40+):

```
code_query, context_pack, memory_query, code_read_hashline, code_verify,
code_graph_callers, code_graph_callees, code_graph_symbol, code_graph_hotspots,
stream_guard_scan, sandbox_execute, sandbox_fetch, sandbox_list,
memory_tier, obs_search, obs_health_summary, obs_usage, doctor_strict,
record_decision, record_todo, close_todo, append_session_note, append_handoff,
recommend_skills, recommend_skills_accept, recommend_skills_reject,
skills_list, skills_uninstall, agents_recommend, agents_accept, agents_reject,
precall_recommend, precall_accept, precall_activate, precall_disable, precall_list,
ai_status, ai_request_rebuild, federated_summary,
lsp_available, lsp_find_references, lsp_goto_definition, lsp_workspace_symbols,
speculative_mine_patterns, speculative_hit_rate,
trajectory_summarize
```

### Antigravity (Google) 통합

- **workspace `.agents/mcp_config.json`** — `serverUrl` (Claude의 `url`이 아닌) 키 사용, 우리가 dialect 자동 변환.
- **글로벌 셋업 (opt-in)** — `scripts/setup-antigravity-global.sh`가 `~/.local/bin/code-brain-mcp` wrapper 설치 + `~/.gemini/antigravity/mcp_config.json`에 등록. **유일하게 글로벌을 건드리는 부분이라 기본 설치에선 실행되지 않고, `AI_INSTALL_GLOBAL_ANTIGRAVITY=1`로 명시 opt-in해야 동작**(Antigravity 1.0.x가 MCP를 워크스페이스가 아닌 글로벌에서 읽기 때문). wrapper는 spawning cwd에서 위로 walk-up하며 `.ai/bin/ai-mcp` 자동 발견 → multi-project 한 줄 설정.
- **agy 전용 도구 매처** — `run_command` (실행), `replace_file_content`/`multi_replace_file_content`/`view_file` (편집/탐색) 자동 인식.
- **CommandLine 리라이트** — Antigravity의 `CommandLine` 파라미터를 우리 `updatedInput` 메커니즘으로 sandbox 명령으로 자동 교체.

### Skill / Agent 추천

- **`recommend skills`** — 누적 메모리에서 슬래시 명령 후보 마이닝. Claude `commands/`, Codex `prompts/`, Antigravity `.agents/skills/<slug>/SKILL.md` 세 곳에 동시 publish.
- **`agents_recommend`** — transcripts에서 sub-agent 정의 후보 마이닝.
- **`recommendation_satisfaction`** — 추천 수락률 추적 (100% / 18-of-18 등 SessionStart에 자동 표시).
- **Danger pattern 자동 거부** — `<system-reminder>`, "ignore previous instructions" 같은 prompt-injection 패턴 자동 reject.

### Federated patterns (multi-project)

- **`federated_summary`** — 같은 머신의 다른 Code Brain 설치 (manifest 기반 자동 발견)에서 결정 태그, todo bigram, precall 룰 카테고리, 설치된 skill 슬러그를 cross-project 집계.
- **Antigravity coverage** — `.agents/mcp_config.json` 설치율 cross-project 리포트.
- SessionStart context에 "Federated patterns from N projects" 자동 inject.

### Observability

- **`ai obs`** subcommand: log / metrics / usage / search / slo / health-summary / **trajectory** (new) / **speculative** (new).
- **`obs usage`** — 실제 Claude transcript token 사용량 (사용 가능 시), Code Brain hook injection 바이트 수, search/context 바이트 수.
- **`obs health-summary`** — doctor + queue + worker + index roll-up.
- **`obs slo`** — hot-path latency p95 vs target (200ms / SessionStart 1500ms).

### 운영 인프라

- **`doctor --strict`** — 23+ checks: layout / config / sqlite features / index_freshness / manifest / trust / jsonl / audit_index / audit_chain / hot_path_slo / secret_scan / no_token_estimates / mcp_methods_registered / redaction_self_test / bootstrap_preflight / worker_singleton_lock / queue_lease_recovery / queue_age / diagnostics / skills_catalog / precall_rules / **antigravity_artifacts** (new).
- **Worker singleton** — 동시 실행 잠금 + recovery.
- **Queue P0-P3** — file-based lease + dead-letter.
- **Trust** — age-like 로컬 identity + tracked machine 기록.
- **Inbox** — 5-gate approval (auth/permission, billing, deploy/prod, data deletion, policy).
- **Diagnostics bundle** — `.ai/cache/diagnostics`에 redacted local bundle.
- **Release gate** — bootstrap + env-check + preflight + lint + smoke + docs-check + package + verify-artifacts + install-check + reproducibility + tamper-check + release-gate.

---

## 설치

### 한 줄 설치 (zero-config, 권장)

`uv`를 자동 설치(없을 때)하고, repo-local로 설치 + 런타임 부트스트랩까지 한 번에. **글로벌/머신 설정은 건드리지 않는다.** git 미연동 프로젝트도 동작(크로스머신 동기만 비활성).

```bash
# macOS / Linux
/path/to/code-brain/scripts/install.sh /path/to/your/project      # 인자 생략 시 현재 디렉토리

# Windows (PowerShell)
powershell -NoProfile -ExecutionPolicy Bypass -File C:\path\to\code-brain\scripts\install.ps1 C:\path\to\your\project
```

설치된 CLI 종류와 무관하게 claude/codex/agy 설정을 모두 작성한다(없는 CLI엔 무해). 크로스머신 동기는 옵트인: `.ai/config.yaml`의 `memory_sync.enabled: true` + (원격-앞섬 감지용) `AI_REMOTE_FETCH=1`.

### 새 프로젝트에 attach (수동/세부 제어)

```bash
cd /path/to/your/project
/path/to/code-brain/scripts/install-into.sh install $(pwd)
```

생성/머지되는 것:
- `.ai/` (전체 — runtime, memory, cache 디렉토리 구조)
- `.mcp.json` (Claude Code MCP 서버 등록)
- `.codex/config.toml` + `.codex/hooks.json` (Codex CLI)
- `.claude/settings.json` (Claude Code hooks)
- `.agents/mcp_config.json` + `.agents/hooks.json` (Antigravity)
- 루트 `AGENTS.md` — 사용자 정의 파일이 있으면 절대 덮어쓰지 않음 (seed-only)
- `.githooks/post-merge` + `post-checkout` (인덱스 자동 refresh)

### 글로벌 Antigravity 셋업 (opt-in — 유일한 글로벌 쓰기)

```bash
AI_INSTALL_GLOBAL_ANTIGRAVITY=1 bash /path/to/code-brain/scripts/setup-antigravity-global.sh
```

`~/.local/bin/code-brain-mcp` wrapper + `~/.gemini/antigravity/mcp_config.json` 등록. **기본 설치는 이 스크립트를 호출하지 않는다**(repo-local 전용). `AI_INSTALL_GLOBAL_ANTIGRAVITY=1`을 명시해야만 실행되며, 그 외엔 글로벌/머신 설정을 일절 건드리지 않는다.

### 업그레이드

```bash
bash scripts/install-into.sh upgrade /path/to/your/project
```

- managed 파일만 갱신. user-owned 파일 (root `AGENTS.md` 등)은 손대지 않음.
- root 권한으로 실행 시 `.ai/` 전체 트리 owner 자동 복원.
- Windows: `.claude/settings.json` + `.codex/hooks.json` hook에 `commandWindows` 자동 등록 (PowerShell `ai-hook.ps1` 호출). MCP 서버는 OS 감지로 `powershell -File .ai/bin/ai-mcp.ps1` 사용. (Antigravity hooks는 자체 5-이벤트 정규 스키마를 쓰며, agy의 Windows hook 패리티는 실제 Windows 환경 검증이 더 필요한 미완 항목.)

### 제거

```bash
bash scripts/install-into.sh uninstall /path/to/your/project
```

`.ai/generated/install-manifest.json`에 기록된 파일만 제거. 사용자 데이터 (`.ai/memory/*`, `AGENTS.md`) 보존.

---

## Quick Start (development)

```bash
cd code-brain
make env-check
make preflight
make lint
make quick
uv run --project .ai/runtime ai version
uv run --project .ai/runtime ai doctor --strict --json
uv run --project .ai/runtime ai obs trajectory --json --limit 5     # 최근 세션 진단
uv run --project .ai/runtime ai obs speculative --json --min-support 3   # 패턴 마이닝
uv run --project .ai/runtime ai code query "audit chain" --json
printf '{"agent":"codex"}' | uv run --project .ai/runtime ai hook SessionStart --json
```

---

## 운영 안전망

### Locked rules
- `.ai/`가 단일 repo-local 소스.
- Hook · MCP hot path 네트워크 호출 금지.
- CI는 read-only. write-class 명령은 worker 접촉 전 거부.
- 트래킹 소스에 plaintext secret 금지.
- 캐시는 단일 SQLite (`.ai/cache/code.sqlite`).
- Audit는 append-only + 연 단위 rotate.
- Default retriever는 `bm25`; vector/hybrid는 future opt-in (`AI_SEARCH_DENSE=1` + `[dense]` extra).

### 보안 게이트
- **Stream guard**: 파괴적 git, 외부 secret 누출, compound pipeline 자동 차단.
- **Precall routing**: 광범위 grep/find/rg 자동 sandbox로 리라우트.
- **Inbox 5-gate**: 인증/권한/결제/배포/데이터 삭제는 명시 승인 필요.
- **Redact**: 모든 hook payload + audit + obs는 storage 직전 redact 통과.

### 검증 명령
```bash
make doctor                  # 일반 doctor
ai doctor --strict --json    # 23+ checks
make lockfile-check          # scripts/lockfile-check.sh: uv lock --check --project .ai/runtime
make release-gate            # full release gate (package + tamper-check + reproducibility)
```

### 장기/대규모 운영
- **`ai exec prune --older-than-seconds 86400`** — `.ai/cache/sandbox/*` 누적 정리. Stop/SessionEnd 후 sleep-time 백그라운드 job으로 자동 실행됨. 대규모 워크로드에서 수동 호출 가능.
- **`ai audit repair-chain --json`** — `git stash`/머지로 `audit/*.jsonl`의 `prev_sha` 체인이 깨졌을 때 결정적 복구. content 손실 없음.
- **`ai audit rebuild-index --json`** — `audit-index.jsonl`을 연도 audit 로그에서 재구성 (restore/upgrade 후).

---

## 아키텍처 (실제 모듈 매핑)

```
.ai/
├── bin/                          # ai / ai-hook / ai-mcp (UNIX) + ai.ps1 / ai-hook.ps1 / ai-mcp.ps1 (Windows)
├── memory/
│   ├── audit/2026.jsonl          # append-only audit chain (SHA-256 prev_sha)
│   ├── audit-index.jsonl         # year-indexed audit summary
│   ├── decisions.jsonl           # 의사결정 로그
│   ├── todos.jsonl               # cross-session todo (append-only, latest-status semantics)
│   └── session-current.md        # 현재 세션 진행 노트
├── cache/
│   ├── code.sqlite               # FTS5 + embeddings_vec0 (dim-agnostic)
│   ├── sandbox/<exec_id>.txt     # sandbox_execute 전체 출력
│   └── speculative.jsonl         # PASTE 추측 실행 hit/miss 로그
├── runtime/src/ai_core/
│   ├── hooks.py                  # 19+ hook 이벤트 handler
│   ├── mcp_server.py             # 40+ MCP tools (stdio JSON-RPC)
│   ├── search.py                 # BM25 + chunking + dense extra slot
│   ├── hashline.py               # line+SHA-256[:12] anchor 편집 무결성
│   ├── stream_guard.py           # 위험 패턴 차단
│   ├── precall.py                # PreToolUse 정책 평가
│   ├── precall_recommend.py      # Bash 패턴 마이닝
│   ├── codegraph.py              # callers/callees/symbol/hotspots
│   ├── memory.py / memory_tier.py # append-only + MemGPT-style tier
│   ├── audit_fold.py             # 오래된 audit summary fold
│   ├── recommend.py              # skill 추천 (3-target publish)
│   ├── agent_recommend.py        # sub-agent 추천
│   ├── federated.py              # cross-project 패턴 집계
│   ├── mcp_config.py             # MCP dialect 변환 (claude/codex/antigravity)
│   ├── doctor.py                 # 23+ doctor checks
│   ├── obs.py                    # log / metrics / usage / search / slo
│   ├── trajectory.py             # TRAJEVAL 진단 (PoC)
│   ├── speculative.py            # PASTE 패턴 마이닝 (PoC)
│   ├── chunk_filter.py           # CODEFILTER 음성 chunk 제거 (PoC)
│   ├── lsp.py                    # multilspy wrap (PoC)
│   └── ... (50+ modules)
├── runtime/tests/                # 270+ pytest cases
├── generated/
│   ├── manifest.json             # render output 메타데이터
│   └── install-manifest.json     # install-into 추적 파일 목록
└── AGENTS.md                     # canonical 에이전트 계약
```

---

## 관련 연구

본 인프라가 따르거나 영감을 받은 SOTA 2026:

- **GrepRAG** (arXiv 2601.23254) — lexical retrieval이 graph 기반 SOTA와 비슷하거나 더 좋음 → BM25 default 노선 정당화.
- **Repoformer** (arXiv 2403.10059) — 항상 retrieval은 비효율. 선택적 retrieval로 -70% latency.
- **PASTE** (arXiv 2603.18897) — Pattern-Aware Speculative Tool Execution, -48.5% task time.
- **CODEFILTER** (arXiv 2508.05970) — Impact-driven context filtering, negative chunk 제거.
- **TRAJEVAL** (arXiv 2603.24631) — fine-grained agent trajectory 진단.
- **Continual Harness** (Karten et al. 2605.09998) — self-improving foundation agent.

## License

Apache-2.0.
