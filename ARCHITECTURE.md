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
│  │ ≤200ms SLO   │  │ JSON-RPC 2.0 │  │ +reject_ci   │  │ 17 checks    │    │
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
   ├─ [retention-sweep.sh]          backlog: stale dist 차단
   ├─ package.sh                    (Python tarfile deterministic, Round 83)
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
