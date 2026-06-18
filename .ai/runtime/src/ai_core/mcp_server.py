from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

from . import __version__
from .doctor import as_payload, run_checks
from .memory import (
    append_decision,
    append_event,
    append_session_note,
    append_todo,
    close_todo,
)
from .obs import health_summary, search_report, usage_report
from .policy import is_ci, reject_ci_write
from .redact import redact_value
from .sandbox import execute as sandbox_execute, fetch as sandbox_fetch, list_executions as sandbox_list
from .search import context_pack, query, rebuild
from .worker.ipc import health

MCP_PROTOCOL_VERSION = "2024-11-05"
MCP_SERVER_NAME = "code-brain"

# Tool catalog. Each entry is exposed via tools/list and dispatched via tools/call.
# Description text is short; the inputSchema follows JSON Schema (draft 2020-12 compatible).
TOOLS: tuple[dict[str, Any], ...] = (
    {
        "name": "memory_query",
        "description": "인덱싱된 소스를 BM25로 검색. 출처가 붙은 상위 K개 스니펫 반환.",
        "inputSchema": {
            "type": "object",
            "properties": {"query": {"type": "string"}, "limit": {"type": "integer", "default": 5}},
            "required": ["query"],
        },
    },
    {
        "name": "code_query",
        "description": "memory_query 별칭 — BM25 코드 검색.",
        "inputSchema": {
            "type": "object",
            "properties": {"query": {"type": "string"}, "limit": {"type": "integer", "default": 5}},
            "required": ["query"],
        },
    },
    {
        "name": "context_pack",
        "description": "BM25 검색 결과에 훅 주입에 적합한 additionalContext 문자열을 더해 반환.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer", "default": 5},
                "mode": {
                    "type": "string",
                    "enum": ["high_fidelity", "balanced", "aggressive"],
                    "default": "balanced",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "code_graph_callers",
        "description": "호출 그래프 역방향 조회: 이 qualname을 누가 호출하나? 읽기전용.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "qualname": {"type": "string", "description": "Function/method qualname (e.g. 'append_audit' or 'C.method')"},
                "limit": {"type": "integer", "default": 20},
            },
            "required": ["qualname"],
        },
    },
    {
        "name": "code_graph_callees",
        "description": "호출 그래프 정방향 조회: 이 qualname이 무엇을 호출하나? 읽기전용.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "qualname": {"type": "string"},
                "limit": {"type": "integer", "default": 20},
            },
            "required": ["qualname"],
        },
    },
    {
        "name": "code_graph_symbol",
        "description": "qualname 일부로 함수/클래스 정의를 찾는다. 읽기전용.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Substring to match against qualname (LIKE %name%)"},
                "limit": {"type": "integer", "default": 20},
            },
            "required": ["name"],
        },
    },
    {
        "name": "code_read_hashline",
        "description": "기존 파일을 편집하기 전 대상 줄 범위를 읽어 stale-edit를 막는 기본 읽기 도구. 줄+해시 앵커 반환; 자격증명류 경로는 거부한다.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "start": {"type": "integer"},
                "end": {"type": "integer"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "stream_guard_scan",
        "description": "Code Brain stream-guard 규칙으로 텍스트를 스캔. 읽기전용.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "scope": {"type": "string", "enum": ["tool", "prompt", "output"], "default": "tool"},
            },
            "required": ["text"],
        },
    },
    {
        "name": "ai_request_rebuild",
        "description": "SQLite FTS5 코드 인덱스를 강제 재빌드. 쓰기성.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "obs_usage",
        "description": "토큰 사용량 + Code Brain 효과 바이트. 읽기전용.",
        "inputSchema": {
            "type": "object",
            "properties": {"include_sessions": {"type": "boolean", "default": False}},
        },
    },
    {
        "name": "obs_health_summary",
        "description": "doctor + 큐 + 워커 + 인덱스 종합 요약. 읽기전용.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "obs_search",
        "description": "stale 감지 리포트가 붙은 BM25 검색.",
        "inputSchema": {
            "type": "object",
            "properties": {"query": {"type": "string"}, "limit": {"type": "integer", "default": 5}},
            "required": ["query"],
        },
    },
    {
        "name": "doctor_strict",
        "description": "모든 doctor 체크를 실행하고 전체 페이로드를 반환. 읽기전용.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "sandbox_execute",
        "description": "샌드박스에서 셸을 실행; 요약+exec_id를 반환하고 전체 출력은 디스크에 저장. 쓰기성. command는 argv 배열(예: [\"git\", \"log\"]) 또는 단일 셸 문자열(`bash -lc`로 실행) 모두 허용해 heredoc/파이프를 JSON 이스케이프 없이 쓸 수 있다. 출력이 작으면(≤20줄/≤1KB) first_lines/last_lines 대신 단일 `output` 필드로 반환.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "command": {
                    "oneOf": [
                        {"type": "string", "minLength": 1},
                        {"type": "array", "items": {"type": "string"}, "minItems": 1},
                    ]
                },
                "cwd": {"type": "string"},
                "timeout": {"type": "integer", "default": 30},
            },
            "required": ["command"],
        },
    },
    {
        "name": "record_decision",
        "description": "결정(또는 재검증 가능한 실패)을 .ai/memory/decisions.jsonl에 기록. 다음 세션에 자동 주입. 실패/부정 결과는 kind='failure'로 두고 observed_versions/environment/retest_after를 적어 '날짜가 있는 재검증 가능한 관측'으로 남긴다 — 영구 금지가 아님. 이후 성공하면 은퇴 처리: kind='failure', status='refuted', supersedes_id=<원래 id>. 쓰기성.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "tags": {"type": "array", "items": {"type": "string"}},
                "source": {"type": "string", "default": "agent"},
                "kind": {"type": "string", "enum": ["decision", "failure"]},
                "observed_at": {"type": "string", "description": "ISO-8601; when the failure was reproduced"},
                "observed_versions": {"type": "object", "description": "versions seen under, e.g. {torch: 2.4.0}"},
                "environment": {"type": "string", "description": "scope: host/preset/codepath"},
                "retest_after": {"type": "string", "description": "ISO date; re-test backstop"},
                "status": {"type": "string", "enum": ["observed", "confirmed", "stale", "refuted"]},
                "supersedes_id": {"type": "string", "description": "retire this prior failure id"},
            },
            "required": ["text"],
        },
    },
    {
        "name": "code_graph_trace",
        "description": "두 심볼 간 최단 caller→callee 사슬을 추적(멀티홉). 방향 잡기용; 결과는 code_query에 넘겨 쓴다.",
        "inputSchema": {"type": "object", "properties": {
            "src": {"type": "string"}, "dst": {"type": "string"}, "max_depth": {"type": "integer", "default": 6}},
            "required": ["src", "dst"]},
    },
    {
        "name": "code_graph_impact",
        "description": "파일/심볼 변경의 영향 범위: 전이적으로 영향받는 호출자들. 변경된 repo 상대경로를 넘기면 리뷰 범위를 좁힌다.",
        "inputSchema": {"type": "object", "properties": {
            "paths": {"type": "array", "items": {"type": "string"}},
            "symbols": {"type": "array", "items": {"type": "string"}},
            "max_depth": {"type": "integer", "default": 4}}},
    },
    {
        "name": "code_graph_architecture",
        "description": "repo 전체 조망: 심볼 수와 호출 중심성 기준 상위 모듈. supervisor용 저비용 지도.",
        "inputSchema": {"type": "object", "properties": {"limit": {"type": "integer", "default": 8}}},
    },
    {
        "name": "ast_grep_search",
        "description": "구조적(AST) 코드 검색: 언어별 구문 패턴에 맞는 코드를 찾는다(예: 패턴 'except: $$$' lang python, 'fetch($URL)' lang ts). BM25가 못 하는 정밀 리팩터/감사 검색.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "ast-grep pattern; $VAR / $$$ metavars"},
                "lang": {"type": "string", "description": "python|javascript|typescript|tsx|go|rust|java|..."},
                "path": {"type": "string", "description": "optional repo-relative subpath to scope"},
                "max_results": {"type": "integer", "default": 40},
            },
            "required": ["pattern", "lang"],
        },
    },
    {
        "name": "loopd_status",
        "description": "Code Brain loopd 상태: 큐 카운트와 warm 워커 상태. 읽기전용; llm_idle_polls는 항상 0.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "loop_submit",
        "description": "멀티에이전트 워커 풀에 작업을 큐잉. 오케스트레이터가 이후 가장 싸고 적합한 워커/모델로 라우팅한다. 사용자가 codex/claude/agy 워커에 작업을 위임하고 싶을 때 사용. 이어서 loopd_dispatch_once를 호출. 쓰기성.",
        "inputSchema": {"type": "object", "properties": {
            "instruction": {"type": "string", "description": "the full task to perform"},
            "goal": {"type": "string", "description": "one-line goal"},
            "model_tier": {"type": "string", "enum": ["cheap", "balanced", "best"], "description": "force a tier (else auto by complexity)"},
            "reviewer_required": {"type": "boolean", "default": False},
            "priority": {"type": "string", "enum": ["P0", "P1", "P2", "P3"], "default": "P1"}},
            "required": ["instruction"]},
    },
    {
        "name": "loopd_up",
        "description": "warm 워커 풀을 띄운다(등록 프로필당 tmux 워커 1개). dry_run은 계획만 표시. autonomous는 명령별 프롬프트를 건너뛴다(안전장치=디스패치 게이트). tier로 모델 비용 등급을 설정. 풀을 시작할 때 사용.",
        "inputSchema": {"type": "object", "properties": {
            "autonomous": {"type": "boolean", "default": False},
            "tier": {"type": "string", "enum": ["cheap", "balanced", "best"]},
            "dry_run": {"type": "boolean", "default": False}}},
    },
    {
        "name": "loopd_recover",
        "description": "loopd 유지보수 틱: 완료 워커를 idle로 해제, stale 표시, 양성 인터럽트 넛지. LLM 없음.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "selfimprove_run",
        "description": "닫힌 루프 자가개선 1사이클 트리거: 저렴한 비자기 judge 워커에 자가검토 작업을 큐잉(이어서 loopd_dispatch_once 호출). judge가 프롬프트 규칙 하나를 제안하고, M_core 안전 게이트를 통과하면 래칫으로 검증해 적용/롤백된다.",
        "inputSchema": {"type": "object", "properties": {
            "tier": {"type": "string", "enum": ["cheap", "balanced", "best"], "default": "cheap"}}},
    },
    {
        "name": "loopd_agents",
        "description": "여기에 어떤 에이전트 CLI(codex/claude/agy)와 tmux가 설치돼 있는지 자동 감지. 풀을 띄우기 전 가장 먼저 호출 — 사용 가능한 에이전트만 실행되고 나머지는 건너뛴다.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "loopd_dispatch_once",
        "description": "결정론적 loopd 디스패치 틱 1회(대기 작업을 idle 워커에 배정; 고위험은 보류). LLM 없음. 쓰기성.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "tool_search",
        "description": "키워드로 Code Brain MCP 도구를 찾아 전체 스키마를 가져온다. 기본 도구 목록이 compact(AI_MCP_COMPACT_TOOLS)이고 필요한 도구가 로드되지 않았을 때 사용.",
        "inputSchema": {"type": "object", "properties": {
            "query": {"type": "string"}, "limit": {"type": "integer", "default": 8}}, "required": ["query"]},
    },
    {
        "name": "lessons_recall",
        "description": "질의에 관련된 정제된 교훈(과거 실행에서 캐낸 실패예방 전략)을 confidence*relevance*recency 순으로 회상. 읽기전용. 위험/반복 작업 전에 호출해 과거 경험을 재활용.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer", "default": 5},
            },
            "required": ["query"],
        },
    },
    {
        "name": "memory_recall",
        "description": "질의에 관련된 durable 메모리(결정·실패·교훈·절차)를 confidence*relevance*recency로 통합 회상하고 인용 블록을 반환. 읽기전용·로컬·LLM합성 없음. lessons_recall의 상위호환(교훈만이 아니라 결정/실패/절차까지). 위험/반복 작업 전 '이 주제로 뭘 알고있나' 조회.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer", "default": 8},
                "types": {"type": "array", "items": {"type": "string", "enum": ["decision", "failure", "lesson", "procedure"]},
                          "description": "optional subset of stores to search; default all"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "list_decisions",
        "description": "기록된 결정/실패(decisions.jsonl)를 필터로 온디맨드 조회. 읽기전용. SessionStart 주입 tail 너머의 과거 결정을 미드세션에 질의할 때 사용. 필터: kind(decision|failure)/status/tag/source/text. 실패는 id로 fold되고 stale/refuted는 기본 제외.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": ["decision", "failure"]},
                "status": {"type": "string", "enum": ["observed", "confirmed", "stale", "refuted"]},
                "tag": {"type": "string", "description": "match any tag (substring)"},
                "source": {"type": "string", "description": "substring match on source"},
                "text": {"type": "string", "description": "substring match on decision text"},
                "limit": {"type": "integer", "default": 20},
                "include_retired": {"type": "boolean", "default": False},
            },
        },
    },
    {
        "name": "record_todo",
        "description": "열린 todo를 .ai/memory/todos.jsonl에 기록. 다음 세션에 자동 주입. 쓰기성.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "owner": {"type": "string", "default": ""},
                "tags": {"type": "array", "items": {"type": "string"}},
                "source": {"type": "string", "default": "agent"},
            },
            "required": ["title"],
        },
    },
    {
        "name": "close_todo",
        "description": "id 또는 제목 일부로 todo를 닫는다. 쓰기성.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "match": {"type": "string"},
                "status": {"type": "string", "enum": ["done", "closed", "cancelled", "canceled"], "default": "done"},
                "reason": {"type": "string", "default": ""},
            },
            "required": ["match"],
        },
    },
    {
        "name": "append_session_note",
        "description": ".ai/memory/session-current.md에 마일스톤 한 줄을 추가. 쓰기성.",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    },
    {
        "name": "evidence_list",
        "description": ".ai/memory/evidence.jsonl의 최신 repo-local 증거 레코드 목록. 읽기전용.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "status": {"type": "string", "enum": ["candidate", "curated", "verified", "rejected"]},
                "limit": {"type": "integer", "default": 20},
            },
        },
    },
    {
        "name": "evidence_record",
        "description": "명시적 repo-local 증거 항목을 기록. 쓰기성.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "path": {"type": "string"},
                "status": {"type": "string", "enum": ["candidate", "curated", "verified", "rejected"], "default": "candidate"},
                "snippet": {"type": "string", "default": ""},
                "source": {"type": "string", "default": "agent"},
                "note": {"type": "string", "default": ""},
            },
            "required": ["query", "path"],
        },
    },
    {
        "name": "evidence_set_status",
        "description": "증거 레코드를 승격하거나 거부. 쓰기성.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "status": {"type": "string", "enum": ["candidate", "curated", "verified", "rejected"]},
                "note": {"type": "string", "default": ""},
                "source": {"type": "string", "default": "agent"},
            },
            "required": ["id", "status"],
        },
    },
    {
        "name": "security_finding_list",
        "description": ".ai/memory/security-findings.jsonl의 최신 repo-local 보안 발견 목록. 읽기전용.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "status": {"type": "string", "enum": ["open", "verified_fixed", "accepted_risk", "false_positive"]},
                "limit": {"type": "integer", "default": 50},
            },
        },
    },
    {
        "name": "security_finding_record",
        "description": "요약/해시 증거가 붙은 redacted 보안 발견을 기록. 쓰기성.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "affected_path": {"type": "string"},
                "finding_type": {"type": "string"},
                "detail_summary": {"type": "string"},
                "evidence_hash": {"type": "string", "default": ""},
                "repro_command": {"type": "string"},
                "verification_command": {"type": "string"},
                "status": {"type": "string", "enum": ["open", "verified_fixed", "accepted_risk", "false_positive"], "default": "open"},
                "source": {"type": "string", "default": "agent"},
            },
            "required": ["affected_path", "finding_type", "detail_summary", "repro_command", "verification_command"],
        },
    },
    {
        "name": "security_finding_update",
        "description": "검증 후 보안 발견 상태를 갱신. 쓰기성.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "status": {"type": "string", "enum": ["open", "verified_fixed", "accepted_risk", "false_positive"]},
                "verification_command": {"type": "string"},
                "source": {"type": "string", "default": "agent"},
            },
            "required": ["id", "status", "verification_command"],
        },
    },
    {
        "name": "append_handoff",
        "description": "정지 지점에서 resume HANDOFF(goal/plan/next_step/open_questions/blockers)를 설정/갱신. Git 추적이라 머신 간(Mac↔VPS) 이동 — 다음 세션은 어떤 에이전트·어느 머신이든 SessionStart 컨텍스트 맨 앞에 이걸 둔다. 부분 갱신: 준 필드만 변경. 쓰기성. 작업을 멈추기 전에 호출.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "goal": {"type": "string", "description": "What we are ultimately trying to do"},
                "next_step": {"type": "string", "description": "The very next action to take on resume"},
                "plan": {"type": "array", "items": {"type": "string"}},
                "open_questions": {"type": "array", "items": {"type": "string"}},
                "blockers": {"type": "array", "items": {"type": "string"}},
                "agent": {"type": "string", "default": "agent"},
                "clear": {"type": "boolean", "default": False},
            },
        },
    },
    # remote_memory_* tools removed (T37) — .ai/ git sync replaces Cloudflare round-trip.
    # ---- Innovation modules (PoC; safe — no hot-path mutation) ----
    {
        "name": "speculative_mine_patterns",
        "description": "audit/2026.jsonl에서 투기실행용 2-gram 도구호출 패턴을 캔다.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "min_support": {"type": "integer", "default": 3},
                "min_confidence": {"type": "number", "default": 0.5},
                "limit": {"type": "integer", "default": 100},
            },
        },
    },
    {
        "name": "speculative_hit_rate",
        "description": ".ai/cache/speculative.jsonl 기반 투기실행 적중/실패 요약.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "trajectory_summarize",
        "description": "최근 세션들에 대한 TRAJEVAL식 궤적 진단(효율성 + 실패모드).",
        "inputSchema": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "default": 10}},
        },
    },
    {
        "name": "autoresearch_search",
        "description": "AutoResearch 지식위키 FTS5 BM25 검색(Stage 0). 읽기전용.",
        "inputSchema": {
            "type": "object",
            "properties": {"q": {"type": "string"}, "k": {"type": "integer", "default": 10}},
            "required": ["q"],
        },
    },
    {
        "name": "autoresearch_ingest_stage",
        "description": "AutoResearch ingest 1단계: 불변 raw + manifest를 보존(sha256 멱등), 에이전트가 요약하도록 nonce로 감싼 데이터를 반환. `content`(로컬) 또는 `url`(Stage 3, SSRF 가드 HTTPS 페치) 제공. 웹 콘텐츠는 신뢰불가(플래그 시 격리). 쓰기성.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "content": {"type": "string"},
                "url": {"type": "string"},
                "source_url": {"type": "string"},
                "title": {"type": "string"},
                "trust_tier": {"type": "string"},
            },
        },
    },
    {
        "name": "autoresearch_ingest_commit",
        "description": "AutoResearch ingest 2단계: verify-det 게이트 후 에이전트가 작성한 위키 페이지 + FTS + 로그를 쓴다. 인용 실패는 status:draft로 격리. 쓰기성.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "source_id": {"type": "string"},
                "pages": {"type": "array", "items": {"type": "object"}},
            },
            "required": ["source_id", "pages"],
        },
    },
    {
        "name": "autoresearch_lint",
        "description": "AutoResearch 위키 건강 lint(Stage 0): orphan/draft/taint/stale 페이지. 읽기전용, 자동수정 없음.",
        "inputSchema": {
            "type": "object",
            "properties": {"stale_before": {"type": "string"}},
        },
    },
    {
        "name": "autoresearch_query",
        "description": "AutoResearch 지식 질의(Stage 0): 페이지별 신뢰 신호가 붙은 FTS5 검색. draft/taint 페이지는 후보에서 격리(세탁 방어); 인용된 답변은 호출 에이전트가 작성한다.",
        "inputSchema": {
            "type": "object",
            "properties": {"question": {"type": "string"}, "k": {"type": "integer", "default": 10}},
            "required": ["question"],
        },
    },
    {
        "name": "autoresearch_verify",
        "description": "AutoResearch 결정론적 인용 검증(Stage 3): 각 주장의 인용문을 출처 텍스트와 대조 채점(faithfulness 0~1, LLM 없음). 점수로 에이전트가 수용/완화/거부를 판단; 사실성 판단은 에이전트 몫.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "claims": {"type": "array", "items": {"type": "object"}},
                "long_tail_ids": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["claims"],
        },
    },
    {
        "name": "autoresearch_deepresearch_start",
        "description": "Stage 3: 딥리서치 세션 시작. 런타임은 상태만 추적; 에이전트가 plan→fetch(autoresearch_ingest_stage url)→synthesize→commit. 세션 반환.",
        "inputSchema": {"type": "object", "properties": {"question": {"type": "string"}}, "required": ["question"]},
    },
    {
        "name": "autoresearch_deepresearch_update",
        "description": "Stage 3: 딥리서치 세션 갱신(subquestions/add_source/status). 크기 제한.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "subquestions": {"type": "array", "items": {"type": "string"}},
                "add_source": {"type": "string"},
                "status": {"type": "string"},
            },
            "required": ["session_id"],
        },
    },
    {
        "name": "autoresearch_deepresearch_status",
        "description": "Stage 3: session_id로 딥리서치 세션 상태 조회.",
        "inputSchema": {"type": "object", "properties": {"session_id": {"type": "string"}}, "required": ["session_id"]},
    },
    {
        "name": "autoresearch_route",
        "description": "Stage 4: 결정론적 복잡도 휴리스틱(RouteLLM식)으로 질의의 모델 티어(local/frontier)를 제안. 최종 모델 선택은 에이전트가. LLM 없음.",
        "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
    },
    {
        "name": "autoresearch_survey_plan",
        "description": "Stage 4: 너비우선 멀티에이전트 팬아웃(orchestrator-worker) 게이트. single/multi 권고, 제한된 워커 목록, ~15배 비용 경고를 반환. 결정론적 정책이며 실행기는 아님. LLM 없음.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "subtopics": {"type": "array", "items": {"type": "string"}},
                "independent": {"type": "boolean"},
                "max_workers": {"type": "integer"},
            },
            "required": ["subtopics"],
        },
    },
    {
        "name": "autoresearch_loop_start",
        "description": "Stage 2(기본 OFF; autoresearch.loop.enable): 메트릭 래칫 루프 시작. 런타임은 상태+예산 추적; git(worktree/commit/reset)과 편집은 에이전트가. metric_cmd는 사용자 신뢰 명령이어야 함.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "workspace": {"type": "string"},
                "metric_cmd": {"type": ["string", "array"], "items": {"type": "string"}},
                "metric_grep": {"type": "string"},
                "direction": {"type": "string", "enum": ["minimize", "maximize"]},
                "edit_surface": {"type": "array", "items": {"type": "string"}},
                "max_iters": {"type": "integer"},
                "max_cost_usd": {"type": "number"},
                "per_run_timeout_s": {"type": "integer"},
            },
            "required": ["workspace", "metric_cmd", "metric_grep", "direction"],
        },
    },
    {
        "name": "autoresearch_loop_record",
        "description": "Stage 2: 래칫 평가 1회 실행(하드닝된 샌드박스—네트워크+env 격리—에서 메트릭). 결정 keep|discard|crash + best + should_continue 반환. discard/crash 시 에이전트가 git-reset.",
        "inputSchema": {
            "type": "object",
            "properties": {"session_id": {"type": "string"}, "cost_spent": {"type": "number"}},
            "required": ["session_id"],
        },
    },
    {
        "name": "autoresearch_loop_status",
        "description": "Stage 2: session_id로 래칫 루프 세션 상태 조회.",
        "inputSchema": {"type": "object", "properties": {"session_id": {"type": "string"}}, "required": ["session_id"]},
    },
    {
        "name": "autoresearch_loop_stop",
        "description": "Stage 2: 래칫 루프 정지(자동 머지 없음; 최선 커밋은 사람이 검토).",
        "inputSchema": {"type": "object", "properties": {"session_id": {"type": "string"}}, "required": ["session_id"]},
    },
)


MCP_METHODS = tuple(tool["name"] for tool in TOOLS)
TOOL_NAMES = frozenset(MCP_METHODS)

# tools/list payload is static within a process lifetime (TOOLS is a module-level
# constant). Profiling on real projects showed tools/list being called 100+ times
# per session with a ~8KB response — pure waste. Cache once and reuse.
#
# Safety:
#  - The cached value is the inner "result" payload (a dict {"tools": [...]}).
#  - handle_request wraps it in _ok(request_id, cached) — request_id is fresh.
#  - redact_value walks the response and returns a fresh copy, so the cached
#    payload itself is never mutated by downstream callers.
_TOOLS_LIST_CACHE: dict[str, Any] | None = None


# Hot tools surfaced by default in compact mode; the rest load on demand via tool_search.
# Opt-in (AI_MCP_COMPACT_TOOLS=1) — cuts the fixed per-session tool-schema token cost for
# clients without their own deferred-loading (landscape P1). Default OFF = no behavior change.
_USAGE_TOOLS = frozenset({
    "obs_usage",
    "code_query",
    "context_pack",
    "code_read_hashline",
    "tool_search",
})
_CORE_TOOLS = frozenset({
    *_USAGE_TOOLS,
    "obs_health_summary",
    "obs_search",
    "doctor_strict",
})


def _compact_tools_enabled() -> bool:
    import os
    return str(os.environ.get("AI_MCP_COMPACT_TOOLS", "")).strip() not in ("", "0", "false", "no")


def _tool_profile() -> str:
    import os
    profile = str(os.environ.get("AI_CODE_BRAIN_PROFILE", "")).strip().lower()
    if profile in {"usage", "core", "compact", "full"}:
        return profile
    return "compact" if _compact_tools_enabled() else "full"


def _build_tools_list_payload() -> dict[str, Any]:
    """Build the tools/list result payload. Pure function over module constants."""
    profile = _tool_profile()
    if profile == "usage":
        return {"tools": [dict(t) for t in TOOLS if t["name"] in _USAGE_TOOLS]}
    if profile in {"core", "compact"}:
        return {"tools": [dict(t) for t in TOOLS if t["name"] in _CORE_TOOLS]}
    return {"tools": [dict(tool) for tool in TOOLS]}


def _get_tools_list_payload() -> dict[str, Any]:
    """Return the cached tools/list payload, building it on first call."""
    global _TOOLS_LIST_CACHE
    if _TOOLS_LIST_CACHE is None:
        _TOOLS_LIST_CACHE = _build_tools_list_payload()
    return _TOOLS_LIST_CACHE


def _invalidate_tools_list_cache() -> None:
    """Reset the tools/list cache. Reserved for hot-reload / test scenarios."""
    global _TOOLS_LIST_CACHE
    _TOOLS_LIST_CACHE = None


def _dispatch_tool(root: Path, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Run the underlying handler for a tool by name. Raises KeyError if unknown."""
    args = arguments or {}
    if name == "autoresearch_search":
        from .autoresearch import storage as _ars, hybrid as _arh
        return {"results": _arh.search(_ars.data_root(root), str(args.get("q", "")), k=int(args.get("k", 10) or 10))}
    if name == "autoresearch_ingest_stage":
        from .autoresearch import storage as _ars, ingest as _ari
        content = args.get("content")
        url = args.get("url")
        has_content = isinstance(content, str) and content
        has_url = isinstance(url, str) and url
        if not has_content and not has_url:
            raise ValueError("autoresearch_ingest_stage requires non-empty content or url")
        return _ari.stage_source(
            _ars.data_root(root),
            content=content if has_content else None,
            url=url if has_url else None,
            source_url=str(args.get("source_url", "")), title=str(args.get("title", "")),
            trust_tier=str(args.get("trust_tier", "untrusted")),
        )
    if name == "autoresearch_ingest_commit":
        from .autoresearch import storage as _ars, ingest as _ari
        sid = args.get("source_id")
        if not isinstance(sid, str) or not sid:
            raise ValueError("autoresearch_ingest_commit requires source_id")
        pages = args.get("pages")
        if not isinstance(pages, list):
            raise ValueError("autoresearch_ingest_commit requires pages array")
        return _ari.commit_pages(_ars.data_root(root), source_id=sid, pages=pages)
    if name == "autoresearch_lint":
        from .autoresearch import storage as _ars, lint as _arl
        sb = args.get("stale_before")
        return _arl.lint(_ars.data_root(root), stale_before=str(sb) if isinstance(sb, str) and sb else None)
    if name == "autoresearch_query":
        from .autoresearch import storage as _ars, query as _arq
        return _arq.query(_ars.data_root(root), str(args.get("question", "")), k=int(args.get("k", 10) or 10))
    if name == "autoresearch_verify":
        from .autoresearch import storage as _ars, verify as _arv
        claims = args.get("claims")
        if not isinstance(claims, list):
            raise ValueError("autoresearch_verify requires claims array")
        lt = args.get("long_tail_ids")
        return _arv.verify_claims(_ars.data_root(root), claims, long_tail_ids=lt if isinstance(lt, list) else None)
    if name == "autoresearch_deepresearch_start":
        from .autoresearch import storage as _ars, deepresearch as _dr
        q = args.get("question")
        if not isinstance(q, str) or not q:
            raise ValueError("autoresearch_deepresearch_start requires question")
        return _dr.start(_ars.data_root(root), q)
    if name == "autoresearch_deepresearch_update":
        from .autoresearch import storage as _ars, deepresearch as _dr
        sid = args.get("session_id")
        if not isinstance(sid, str) or not sid:
            raise ValueError("autoresearch_deepresearch_update requires session_id")
        res = _dr.update(
            _ars.data_root(root), sid,
            subquestions=args.get("subquestions") if isinstance(args.get("subquestions"), list) else None,
            add_source=args.get("add_source") if isinstance(args.get("add_source"), str) else None,
            status=args.get("status") if isinstance(args.get("status"), str) else None,
        )
        return res if res is not None else {"error": "session_not_found_or_invalid"}
    if name == "autoresearch_deepresearch_status":
        from .autoresearch import storage as _ars, deepresearch as _dr
        sid = args.get("session_id")
        if not isinstance(sid, str) or not sid:
            raise ValueError("autoresearch_deepresearch_status requires session_id")
        res = _dr.get(_ars.data_root(root), sid)
        return res if res is not None else {"error": "session_not_found"}
    if name == "autoresearch_route":
        from .autoresearch import complexity_router as _cr
        return _cr.classify(str(args.get("query", "")))
    if name == "autoresearch_survey_plan":
        from .autoresearch import orchestration as _orch
        return _orch.survey_plan(
            args.get("subtopics", []),
            independent=bool(args.get("independent", False)),
            max_workers=args.get("max_workers", _orch.DEFAULT_MAX_WORKERS),
        )
    if name == "autoresearch_loop_start":
        from .autoresearch import loop as _loop
        return _loop.start(
            root,
            workspace=str(args.get("workspace", "")),
            metric_cmd=args.get("metric_cmd"),
            metric_grep=str(args.get("metric_grep", "")),
            direction=str(args.get("direction", "")),
            edit_surface=args.get("edit_surface"),
            max_iters=args.get("max_iters", 50),
            max_cost_usd=args.get("max_cost_usd", 0.0),
            per_run_timeout_s=args.get("per_run_timeout_s", 600),
        )
    if name == "autoresearch_loop_record":
        from .autoresearch import loop as _loop
        sid = args.get("session_id")
        if not isinstance(sid, str) or not sid:
            raise ValueError("autoresearch_loop_record requires session_id")
        return _loop.record(root, sid, cost_spent=args.get("cost_spent", 0.0))
    if name == "autoresearch_loop_status":
        from .autoresearch import loop as _loop
        sid = args.get("session_id")
        if not isinstance(sid, str) or not sid:
            raise ValueError("autoresearch_loop_status requires session_id")
        return _loop.status(root, sid)
    if name == "autoresearch_loop_stop":
        from .autoresearch import loop as _loop
        sid = args.get("session_id")
        if not isinstance(sid, str) or not sid:
            raise ValueError("autoresearch_loop_stop requires session_id")
        return _loop.stop(root, sid)
    if name in ("memory_query", "code_query"):
        return query(root, str(args.get("query", "")), limit=int(args.get("limit", 5) or 5), evidence_source="search")
    if name == "context_pack":
        return context_pack(
            root,
            str(args.get("query", "")),
            limit=int(args.get("limit", 5) or 5),
            mode=str(args.get("mode", "balanced") or "balanced"),
        )
    if name == "code_graph_callers":
        from .codegraph import query_callers
        return query_callers(root, str(args.get("qualname", "")), limit=int(args.get("limit", 20) or 20))
    if name == "code_graph_callees":
        from .codegraph import query_callees
        return query_callees(root, str(args.get("qualname", "")), limit=int(args.get("limit", 20) or 20))
    if name == "code_graph_symbol":
        from .codegraph import find_symbol
        return find_symbol(root, str(args.get("name", "")), limit=int(args.get("limit", 20) or 20))
    if name == "code_graph_trace":
        from .codegraph import trace_call_path
        return trace_call_path(root, src=str(args.get("src", "")), dst=str(args.get("dst", "")),
                               max_depth=int(args.get("max_depth", 6) or 6))
    if name == "code_graph_impact":
        from .codegraph import blast_radius, impacted_by_paths
        paths = args.get("paths") if isinstance(args.get("paths"), list) else None
        if paths:
            return impacted_by_paths(root, paths=paths, max_depth=int(args.get("max_depth", 4) or 4))
        return blast_radius(root, symbols=args.get("symbols") or [], max_depth=int(args.get("max_depth", 4) or 4))
    if name == "code_graph_architecture":
        from .codegraph import architecture_summary
        return architecture_summary(root, limit=int(args.get("limit", 8) or 8))
    if name == "code_read_hashline":
        from .hashline import read_hashline
        target = args.get("path")
        if not isinstance(target, str) or not target:
            raise ValueError("code_read_hashline requires path string")
        return read_hashline(
            root,
            target,
            start=(int(args["start"]) if isinstance(args.get("start"), int) else None),
            end=(int(args["end"]) if isinstance(args.get("end"), int) else None),
        )
    if name == "stream_guard_scan":
        from .stream_guard import scan_text
        return scan_text(str(args.get("text", "")), scope=str(args.get("scope", "tool") or "tool"))
    if name == "ai_request_rebuild":
        return rebuild(root)
    if name == "obs_usage":
        return usage_report(root, include_sessions=bool(args.get("include_sessions", False)))
    if name == "obs_health_summary":
        return health_summary(root)
    if name == "obs_search":
        return search_report(root, query_text=args.get("query"), limit=int(args.get("limit", 5) or 5))
    if name == "doctor_strict":
        return as_payload(run_checks(root))
    if name == "sandbox_execute":
        command = args.get("command")
        if isinstance(command, str):
            if not command.strip():
                raise ValueError("sandbox_execute requires non-empty command")
            command_payload: list[str] | str = command
        elif isinstance(command, list) and command:
            command_payload = [str(part) for part in command]
        else:
            raise ValueError("sandbox_execute requires command as non-empty string or array")
        return sandbox_execute(
            root,
            command=command_payload,
            cwd=str(args["cwd"]) if isinstance(args.get("cwd"), str) else None,
            timeout=int(args.get("timeout", 30) or 30),
        )
    if name == "record_decision":
        text = args.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("record_decision requires non-empty text")
        return append_decision(
            root,
            text=text,
            tags=args.get("tags") if isinstance(args.get("tags"), list) else None,
            source=str(args.get("source", "agent")),
            kind=args.get("kind") if isinstance(args.get("kind"), str) else None,
            observed_at=args.get("observed_at") if isinstance(args.get("observed_at"), str) else None,
            observed_versions=args.get("observed_versions") if isinstance(args.get("observed_versions"), dict) else None,
            environment=args.get("environment") if isinstance(args.get("environment"), str) else None,
            retest_after=args.get("retest_after") if isinstance(args.get("retest_after"), str) else None,
            status=args.get("status") if isinstance(args.get("status"), str) else None,
            supersedes_id=args.get("supersedes_id") if isinstance(args.get("supersedes_id"), str) else None,
        )
    if name == "ast_grep_search":
        pattern = args.get("pattern")
        lang = args.get("lang")
        if not isinstance(pattern, str) or not isinstance(lang, str):
            raise ValueError("ast_grep_search requires pattern and lang")
        from .astgrep_integration import ast_grep_search

        return ast_grep_search(
            root, pattern=pattern, lang=lang,
            path=args.get("path") if isinstance(args.get("path"), str) else None,
            max_results=int(args.get("max_results", 40) or 40),
        )
    if name == "loopd_status":
        from . import loopd as _ld
        return _ld.status(root)
    if name == "loopd_dispatch_once":
        from . import loopd as _ld
        return _ld.dispatch_once(root)
    if name == "loopd_recover":
        from . import loopd as _ld
        return _ld.recovery_tick(root)
    if name == "loopd_agents":
        from . import worker_launch as _wl
        return _wl.capabilities()
    if name == "selfimprove_run":
        from . import self_improve as _si
        return _si.enqueue_review(root, tier=args.get("tier") if args.get("tier") in ("cheap", "balanced", "best") else "cheap")
    if name == "loop_submit":
        instruction = args.get("instruction")
        if not isinstance(instruction, str) or not instruction.strip():
            raise ValueError("loop_submit requires non-empty instruction")
        from . import loop_engineering as _le
        dispatch = {"model_tier": args["model_tier"]} if args.get("model_tier") in ("cheap", "balanced", "best") else None
        goal = str(args.get("goal") or "").strip() or instruction.strip().splitlines()[0][:120]
        # tier pinned atomically at submit (no read-modify-write of the queued file → no TOCTOU)
        return _le.submit(root, instruction=instruction, goal=goal,
                          reviewer_required=bool(args.get("reviewer_required", False)),
                          priority=str(args.get("priority", "P1") or "P1"), dispatch=dispatch)
    if name == "loopd_up":
        from . import worker_launch as _wl
        return _wl.launch_pool(root, dry_run=bool(args.get("dry_run", False)),
                               autonomous=bool(args.get("autonomous", False)),
                               tier=args.get("tier") if args.get("tier") in ("cheap", "balanced", "best") else None)
    if name == "tool_search":
        q = str(args.get("query", "")).strip().lower()
        terms = [t for t in q.split() if t]
        scored: list[tuple[int, dict[str, Any]]] = []
        for tool in TOOLS:
            hay = (tool["name"] + " " + str(tool.get("description", ""))).lower()
            score = sum(1 for t in terms if t in hay)
            if score:
                scored.append((score, tool))
        scored.sort(key=lambda s: s[0], reverse=True)
        limit = int(args.get("limit", 8) or 8)
        return {"ok": True, "tools": [dict(t) for _, t in scored[:limit]]}
    if name == "lessons_recall":
        recall_query = args.get("query")
        if not isinstance(recall_query, str) or not recall_query.strip():
            raise ValueError("lessons_recall requires non-empty query")
        from .lessons import recall_lessons

        return recall_lessons(root, query=recall_query, limit=int(args.get("limit", 5) or 5))
    if name == "memory_recall":
        mr_query = args.get("query")
        if not isinstance(mr_query, str) or not mr_query.strip():
            raise ValueError("memory_recall requires non-empty query")
        from .memory_recall import recall_memory
        mr_types = args.get("types") if isinstance(args.get("types"), list) else None
        return recall_memory(
            root,
            query=mr_query,
            limit=int(args.get("limit", 8) or 8),
            types=[str(t) for t in mr_types] if mr_types else None,
        )
    if name == "list_decisions":
        from .memory import read_decisions_filtered
        return read_decisions_filtered(
            root,
            kind=args.get("kind") if isinstance(args.get("kind"), str) else None,
            status=args.get("status") if isinstance(args.get("status"), str) else None,
            tag=args.get("tag") if isinstance(args.get("tag"), str) else None,
            source=args.get("source") if isinstance(args.get("source"), str) else None,
            text=args.get("text") if isinstance(args.get("text"), str) else None,
            limit=int(args.get("limit", 20) or 20),
            include_retired=bool(args.get("include_retired", False)),
        )
    if name == "record_todo":
        title = args.get("title")
        if not isinstance(title, str) or not title.strip():
            raise ValueError("record_todo requires non-empty title")
        return append_todo(
            root,
            title=title,
            owner=str(args.get("owner", "")),
            tags=args.get("tags") if isinstance(args.get("tags"), list) else None,
            source=str(args.get("source", "agent")),
        )
    if name == "close_todo":
        match = args.get("match")
        if not isinstance(match, str) or not match.strip():
            raise ValueError("close_todo requires match string")
        return close_todo(
            root,
            match=match,
            status=str(args.get("status", "done")),
            reason=str(args.get("reason", "")),
        )
    if name == "append_session_note":
        text = args.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("append_session_note requires non-empty text")
        return append_session_note(root, text=text)
    if name == "evidence_list":
        from .evidence import list_evidence
        status = args.get("status")
        return list_evidence(
            root,
            status=status if isinstance(status, str) and status else None,
            limit=int(args.get("limit", 20) or 20),
        )
    if name == "evidence_record":
        from .evidence import record_evidence
        reject_ci_write("evidence")
        return record_evidence(
            root,
            query=str(args.get("query", "")),
            path=str(args.get("path", "")),
            status=str(args.get("status", "candidate") or "candidate"),
            snippet=str(args.get("snippet", "")),
            source=str(args.get("source", "agent") or "agent"),
            note=str(args.get("note", "")),
        )
    if name == "evidence_set_status":
        from .evidence import set_evidence_status
        reject_ci_write("evidence")
        eid = args.get("id")
        status = args.get("status")
        if not isinstance(eid, str) or not eid.strip():
            raise ValueError("evidence_set_status requires id string")
        if not isinstance(status, str) or not status.strip():
            raise ValueError("evidence_set_status requires status string")
        return set_evidence_status(
            root,
            evidence_id_value=eid,
            status=status,
            note=str(args.get("note", "")),
            source=str(args.get("source", "agent")),
        )
    if name == "security_finding_list":
        from .security_findings import list_records
        status = args.get("status")
        return list_records(
            root,
            status=status if isinstance(status, str) and status else None,
            limit=int(args.get("limit", 50) or 50),
        )
    if name == "security_finding_record":
        from .security_findings import record
        reject_ci_write("security_finding")
        return record(
            root,
            affected_path=str(args.get("affected_path", "")),
            finding_type=str(args.get("finding_type", "")),
            detail_summary=str(args.get("detail_summary", "")),
            evidence_hash=str(args.get("evidence_hash", "")),
            repro_command=str(args.get("repro_command", "")),
            verification_command=str(args.get("verification_command", "")),
            status=str(args.get("status", "open") or "open"),
            source=str(args.get("source", "agent") or "agent"),
        )
    if name == "security_finding_update":
        from .security_findings import update
        reject_ci_write("security_finding")
        fid = args.get("id")
        status = args.get("status")
        verification_command = args.get("verification_command")
        if not isinstance(fid, str) or not fid.strip():
            raise ValueError("security_finding_update requires id string")
        if not isinstance(status, str) or not status.strip():
            raise ValueError("security_finding_update requires status string")
        if not isinstance(verification_command, str) or not verification_command.strip():
            raise ValueError("security_finding_update requires verification_command string")
        return update(
            root,
            finding_id=fid,
            status=status,
            verification_command=verification_command,
            source=str(args.get("source", "agent") or "agent"),
        )
    if name == "append_handoff":
        from .session_resume import write_handoff

        def _as_list(v: Any) -> list[str] | None:
            return [str(x) for x in v] if isinstance(v, list) else None

        return write_handoff(
            root,
            goal=(args.get("goal") if isinstance(args.get("goal"), str) else None),
            next_step=(args.get("next_step") if isinstance(args.get("next_step"), str) else None),
            plan=_as_list(args.get("plan")),
            open_questions=_as_list(args.get("open_questions")),
            blockers=_as_list(args.get("blockers")),
            agent=str(args.get("agent") or "agent"),
            clear=bool(args.get("clear")),
        )
    # remote_memory_* dispatchers removed (T37)
    # ---- Innovation modules (PoC dispatch) ----
    if name == "speculative_mine_patterns":
        from .speculative import mine_patterns
        return mine_patterns(
            root,
            min_support=int(args.get("min_support", 3) or 3),
            min_confidence=float(args.get("min_confidence", 0.5) or 0.5),
            limit=int(args.get("limit", 100) or 100),
        )
    if name == "speculative_hit_rate":
        from .speculative import hit_rate
        return hit_rate(root)
    if name == "trajectory_summarize":
        from .trajectory import summarize
        return summarize(root, limit=int(args.get("limit", 10) or 10))
    raise KeyError(name)


def _parse_prompt_md(text: str) -> tuple[str, str | None, str]:
    """Parse `.claude/commands/*.md` frontmatter -> (description, argument_hint, body)."""
    lines = text.splitlines()
    desc = ""
    arg_hint: str | None = None
    in_fm = False
    body_start = 0
    seen_fm_open = False
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped == "---":
            if not seen_fm_open:
                seen_fm_open = True
                in_fm = True
                continue
            body_start = i + 1
            in_fm = False
            break
        if in_fm:
            if line.startswith("description:"):
                desc = line.split(":", 1)[1].strip().strip("\"").strip("'")
            elif line.startswith("argument-hint:"):
                arg_hint = line.split(":", 1)[1].strip().strip("\"").strip("'")
    body = "\n".join(lines[body_start:]).strip() if seen_fm_open else text.strip()
    return desc, arg_hint, body


def _list_prompts(root: Path) -> list[dict[str, Any]]:
    prompts: list[dict[str, Any]] = []
    cmd_dir = root / ".claude" / "commands"
    if not cmd_dir.exists():
        return prompts
    for md in sorted(cmd_dir.glob("cb-*.md")):
        try:
            text = md.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        desc, arg_hint, _ = _parse_prompt_md(text)
        entry: dict[str, Any] = {"name": md.stem, "description": desc or md.stem}
        if arg_hint:
            entry["arguments"] = [{"name": "input", "description": arg_hint, "required": False}]
        prompts.append(entry)
    return prompts


def _get_prompt(root: Path, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    md = root / ".claude" / "commands" / f"{name}.md"
    if not md.is_file():
        raise KeyError(name)
    text = md.read_text(encoding="utf-8")
    desc, _, body = _parse_prompt_md(text)
    args_value = ""
    if isinstance(arguments, dict):
        for key in ("input", "ARGUMENTS", "args"):
            value = arguments.get(key)
            if isinstance(value, str) and value:
                args_value = value
                break
    body = body.replace("$ARGUMENTS", args_value)
    return {
        "description": desc,
        "messages": [
            {"role": "user", "content": {"type": "text", "text": body}},
        ],
    }


def _ok(request_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _err(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def handle_request(root: Path, request: dict[str, Any]) -> dict[str, Any] | None:
    """Route a single JSON-RPC message. Returns None for notifications (no response).

    Supports both standard MCP protocol (initialize, tools/list, tools/call, etc.)
    and direct tool-name dispatch (legacy/internal callers like ai-mcp --once-json).
    """
    start = time.perf_counter()
    method = request.get("method")
    params = request.get("params") or {}
    request_id = request.get("id")
    is_notification = "id" not in request
    audit_tool_name: str | None = None

    try:
        # Notifications — no response per JSON-RPC 2.0.
        if isinstance(method, str) and method.startswith("notifications/"):
            return None
        if method == "initialize":
            result = {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {
                    "tools": {"listChanged": False},
                    "resources": {"subscribe": False, "listChanged": False},
                },
                "serverInfo": {"name": MCP_SERVER_NAME, "version": __version__},
            }
            response = _ok(request_id, result)
        elif method == "ping":
            response = _ok(request_id, {})
        elif method == "tools/list":
            response = _ok(request_id, _get_tools_list_payload())
        elif method == "tools/call":
            name = params.get("name")
            arguments = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
            audit_tool_name = name if isinstance(name, str) else None
            if not isinstance(name, str) or name not in TOOL_NAMES:
                response = _err(request_id, -32602, f"unknown tool: {name!r}")
            else:
                try:
                    tool_result = _dispatch_tool(root, name, arguments or {})
                    response = _ok(
                        request_id,
                        {
                            "content": [
                                {
                                    "type": "text",
                                    "text": json.dumps(tool_result, ensure_ascii=False, sort_keys=True),
                                }
                            ],
                            "isError": not bool(tool_result.get("ok", True)) if isinstance(tool_result, dict) else False,
                            "structuredContent": tool_result if isinstance(tool_result, dict) else None,
                        },
                    )
                except Exception as exc:
                    response = _ok(
                        request_id,
                        {
                            "content": [{"type": "text", "text": f"error: {exc}"}],
                            "isError": True,
                        },
                    )
        elif method == "prompts/list":
            response = _ok(request_id, {"prompts": []})
        elif method == "prompts/get":
            response = _err(request_id, -32601, "prompts disabled — use local .claude/commands or .codex/prompts directly")
        elif method == "resources/list":
            response = _ok(request_id, {"resources": []})
        elif method == "resources/templates/list":
            response = _ok(request_id, {"resourceTemplates": []})
        elif isinstance(method, str) and method in TOOL_NAMES:
            # Legacy direct dispatch: e.g. {"method": "obs_usage", ...}
            audit_tool_name = method
            result = _dispatch_tool(root, method, params if isinstance(params, dict) else {})
            response = _ok(request_id, result)
        else:
            response = _err(request_id, -32601, f"method not found: {method}")
    except Exception as exc:
        response = _err(request_id, -32000, str(exc))

    record_mcp_request(
        root,
        method,
        request,
        response,
        start,
        response.get("result") if isinstance(response, dict) else None,
        tool_name=audit_tool_name,
    )
    return None if is_notification else redact_value(response)


def record_mcp_request(
    root: Path,
    method: Any,
    request: dict[str, Any],
    response: dict[str, Any],
    start: float,
    result: Any,
    *,
    tool_name: str | None = None,
) -> None:
    if is_ci():
        return
    try:
        event = {
            "hook": "mcp.request",
            "method": method,
            "elapsed_ms": int((time.perf_counter() - start) * 1000),
            "request_bytes": len(json.dumps(request, ensure_ascii=False, sort_keys=True).encode("utf-8")),
            "response_bytes": len(json.dumps(response, ensure_ascii=False, sort_keys=True).encode("utf-8")),
            "results_count": len(result.get("results", [])) if isinstance(result, dict) else None,
        }
        if tool_name:
            event["tool_name"] = tool_name
        append_event(root, event)
    except Exception:
        # mcp.request audit is best-effort; never fail the JSON-RPC response on it.
        pass


def serve_stdio(root: Path) -> int:
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError as exc:
            print(json.dumps(_err(None, -32700, f"parse error: {exc}"), ensure_ascii=False, sort_keys=True), flush=True)
            continue
        response = handle_request(root, request)
        if response is None:
            continue
        print(json.dumps(response, ensure_ascii=False, sort_keys=True), flush=True)
    return 0
