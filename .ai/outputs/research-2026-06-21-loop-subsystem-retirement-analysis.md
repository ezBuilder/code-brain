# loop/worker-pool 서브시스템 폐기 — 영향 분석 (2026-06-21)

> 요청: "loop/worker-pool 서브시스템 자체를 CB에서 폐기할지 별도 분석" (사용자가 옛 loop 명령을 쓰레기로 폐기했다고 밝힘 — 명령 자체는 v0.6.1 prune으로 청소됨).
> 결론(요약): **전체 폐기는 비권장.** 사용자가 싫어한 "worker-pool/오케스트레이터(tmux로 Codex/Claude 띄우기)"와, 이번 세션에 원하셨던 "완료규율(acceptance 게이트·plan continuation)"이 같은 모듈에 얽혀 있음. 명령(쓰레기)은 이미 사라졌으니, 추가 조치는 "worker-pool 표면만 숨김/축소"가 합리적.

## 1. footprint

- 코드 ~**2,496 LOC** / 11 모듈: `loop_engineering`, `loopd`, `loop_continuation`, `worker_registry|launch|profiles|models`, `route_floor`, `acceptance`, `error_classifier`, `tmux_adapter`.
- 의존자 6: `autonomous_harness`, `cli`, `hooks`, `mcp_server`, `memory_tier`, `self_improve`.
- MCP 도구 7: `loopd_status/up/recover/agents/dispatch_once`, `loop_submit`, `selfimprove_run`.
- 테스트 9파일.

## 2. 핵심 — "쓰레기"와 "원하던 것"이 한 모듈에 섞임

| 분류 | 구성요소 | 사용자 평가 |
|---|---|---|
| **worker-pool/오케스트레이터** (사용자가 폐기) | `loopd`, `worker_*`, `tmux_adapter`, `loop_submit`/`loopd_*` MCP, `loop`/`queue`/`worker` CLI | 쓰레기(tmux로 Codex/Claude 띄우는 멀티에이전트) |
| **완료규율/검증** (이번 세션에 원함) | `loop_engineering`의 큐+verdict, `acceptance`(G1), `plan_state`(G2), `loop_continuation`(G3) | 원함(early-stop 해결·검증 게이트) |
| **모델 라우팅 학습** | `route_floor`, `error_classifier`(G4) | 중립(loopd가 소비) |

문제: `loop_engineering.py` 하나에 **큐·verdict·acceptance·worker submit이 전부** 들어있어, "worker-pool만 도려내기"가 깔끔히 안 됨. self_improve는 loop에 작업을 enqueue하고, autonomous_harness(G12)는 loopd의 `infer_risk`/`assess_tier`를 재사용.

## 3. 옵션별 영향

**A. 전체 폐기 (비권장)**
- 제거: ~2,496 LOC + 6 의존자 수정 + 7 MCP 도구 + 9 테스트.
- **부작용**: 이번 세션의 G1(acceptance)·G3(continuation, plan 의존이라 일부)·G4(fallback)·G12(evidence tier)가 **같이 죽음**. self_improve 자동 enqueue·route_floor 학습도 제거. 즉 "early-stop 해결"의 일부 기반이 무너짐.
- 비용 large·위험 high. 사용자가 명시적으로 원한 기능을 되돌리는 셈.

**B. worker-pool 표면만 제거 (중간 — 실효적)**
- 제거: `loopd`/`worker_*`/`tmux_adapter` + `loop_submit`/`loopd_*` MCP 7종 + `loop submit`/`queue`/`worker` CLI + 관련 테스트(~1,200 LOC).
- 유지: `loop_engineering`의 큐/verdict 코어 + `acceptance`(G1) + `plan_state`/`loop_continuation`(G2/G3) + `route_floor`.
- 손봐야 할 결합: `autonomous_harness.evidence_tier`가 `loopd.infer_risk/assess_tier` 사용 → 그 분류기를 작은 모듈로 이전. `self_improve.enqueue_review`가 loop submit 사용 → enqueue 경로 제거 또는 no-op.
- 비용 medium·위험 medium. "tmux 멀티에이전트(쓰레기)"는 사라지고 완료규율은 보존.

**C. 표면 숨김만 (최소 — 권장 1순위)**
- 코드 0삭제. `loopd_*`/`loop_submit` MCP 도구를 기본 tool 프로필에서 **숨김**(tool_search로만 노출), `loop`/`worker` CLI는 두되 문서에서 비노출.
- 사용자 체감: 명령/도구 팔레트에서 worker-pool이 안 보임(이미 명령은 prune됨). 코드·기능은 살아있어 G1~G4 무손상.
- 비용 small·위험 low.

## 4. 권고

1. **명령(진짜 쓰레기)은 v0.6.1 prune으로 이미 해결** — 더 안 되살아남.
2. **전체 폐기(A)는 비권장** — 원하신 완료규율 기능을 같이 파괴.
3. tmux worker-pool을 "다시는 안 쓴다"가 확실하면 **B(표면 제거)**, 그냥 "안 보이게"면 **C(숨김)**. 기본 추천은 **C → (확신 시) B**.
4. 어느 쪽이든 G1(acceptance)·G2(plan)·G3(continuation)는 보존 권장 — early-stop 해결의 핵심.

*footprint·의존자·MCP/CLI 매핑은 2026-06-21 소스 기준. 실행은 별도 승인 시.*
