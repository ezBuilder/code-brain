# Code Brain 세계 최고 수준 자율 업그레이드 운영안

상태: 실행 계약 및 우선순위 백로그  
기준일: 2026-07-30  
적용 범위: Code Brain 코어, `kits/global-agent-kit`, strict doctor, 평가·증거·설치 검증 표면

## 1. 목표와 완료의 의미

이 계획의 목표는 “에이전트가 오래 실행된다”가 아니라, 다음 폐루프를 재현 가능하고 안전하게 반복하는 것이다.

> 검증된 연구 → 추적 가능한 작업 → 최소 구현 → 독립적인 기계 검증 → 타입화된 증거 → 같은 기준으로 재분석

한 라운드는 아래 조건을 모두 만족해야 완료다.

1. 연구의 주장과 출처가 분리되어 있고, 채택 후보마다 로컬 재현 또는 측정 방법이 있다.
2. 후보가 하나 이상의 범위 제한 작업으로 변환되며, 소유 경로와 금지 경로가 명시된다.
3. 구현은 가장 작은 유효 변경으로 제한되고, 기존 dirty 변경을 흡수하거나 되돌리지 않는다.
4. 가장 가까운 테스트, 영향 범위 테스트, strict doctor, 관련 평가 게이트가 실제로 통과한다.
5. 완료 주장은 명령, 관찰 결과, 산출물 경로를 가진 증거로 남는다.
6. 변경 후 동일한 연구 질문과 평가 기준을 다시 적용해 다음 후보를 채택·보류·기각한다.

`95%`는 현재 `autonomous_harness`의 진행 목표이지, 실패한 필수 게이트를 덮는 점수가 아니다. 보안, 승인, acceptance, strict doctor 중 하나라도 필수 실패면 라운드는 완료가 아니다.

## 2. 현재 기준선

### 2.1 검증된 강점

<!-- code-brain-contract: doctor-check-count=30 -->
<!-- code-brain-contract: eval-axes=precall_routing,context_budget,tool_discovery,autoresearch_retrieval,code_retrieval,memory_retrieval -->

| 품질 표면 | 현재 근거 | 유지할 계약 |
| --- | --- | --- |
| Repo-local 탐색 | `code query`, `context pack`, call graph, hashline read/verify | 편집 전 좁은 탐색과 정확한 읽기 |
| 안전한 기본값 | 원격 LLM, 외부 알림, 원격 메모리, AutoResearch web ingest 및 코드 실행 루프가 기본 OFF | 네트워크·원격 mutation은 opt-in |
| Strict doctor | 현재 체크아웃에서 source-derived 30개 check를 강제 | 배포 상태와 `secret_scan`·`index_freshness` 같은 건강 상태를 분리 보고 |
| 결정론 평가 | `make eval`이 source-derived 6개 축을 `--require-complete`로 강제 | 검색·라우팅 변경은 자체 코퍼스 회귀로 판정 |
| 완료 게이트 | `loop submit --require-acceptance`, sandbox 재실행, typed verdict evidence | 모델의 “완료” 텍스트보다 exit code와 증거 우선 |
| 증거·진행 상태 | evidence ledger, memory todo, durable plan, loop request/lease | 모든 라운드를 ID로 연결 |
| 설치·릴리스 | smoke, docs check, package, 재현성, 변조, rollback, release gate | 로컬 통과와 릴리스 가능 상태를 분리 |
| Global kit | validate, installed doctor, Codex doctor, install dry-run, research/evolution scripts | 전역 설치는 명시 승인 후에만 수행 |

현재 `make eval`의 강제 축(6개)은 `precall_routing`, `context_budget`, `tool_discovery`, `autoresearch_retrieval`, `code_retrieval`, `memory_retrieval`이다. `decision_logging`은 실제 prompt-to-memory production path가 없어 명시적으로 미지원 상태다. 미지원 축을 성공으로 계산하지 않는다.

### 2.2 현재 dirty 상태

문서 작성 시점의 기준선은 `develop...origin/develop [ahead 3]`이며, `kits/global-agent-kit` 아래 기존 수정 14개가 있다. 주요 주제는 다음과 같다.

- Claude 공식 문서 URL과 command 용어 갱신
- Codex hook event/config 진단 현대화
- `block-secret-commit.sh` 설치·doctor·validate 연결
- 기존 Claude settings의 env/hook merge와 과거 broad deny 정리
- 전역 `CLAUDE.md`/`AGENTS.md` 규칙 단축 및 상세 문서 분리

이 변경들은 이 문서의 구현 결과가 아니다. 첫 자율 라운드는 아래 기준선을 저장하고, 소유권이 확인되기 전에는 해당 14개 파일을 편집하지 않는다.

```bash
git status --short --branch
git diff --stat
git diff --name-status
git diff --check
```

완료 시 “working tree clean”을 무조건 요구하지 않는다. 대신 시작 기준선과 비교해 작업 소유 경로 밖의 diff가 증가하지 않았음을 요구한다.

### 2.3 닫아야 할 핵심 간극

1. `autonomous_harness`는 95% 목표, 예산, 보호 경로, 증거 수준을 지시하지만 연구를 작업으로 컴파일하거나 구현을 실행하는 오케스트레이터는 아니다.
2. kit의 `dev-loop.sh`는 공식 URL freshness snapshot, 후보 capture/score, dry-run promotion, validate/doctor를 수행하지만 코드 변경은 active agent에 맡긴다.
3. typed round report와 `autonomous_round_completeness`가 round ID·기준선·연구·task·acceptance·review verdict·종료 상태를 묶어 검증한다. todo/plan/loop ledger의 producer-side round ID 강제는 후속 통합 과제다.
4. strict doctor는 프로젝트 건강, 자율 라운드 완결성, 주입 컨텍스트 예산 계약을 검증한다. global-kit 소스와 설치물의 통합 drift는 아직 별도 후속 과제다.
5. eval 기반이 생겼지만 code navigation, memory retrieval, 실제 decision logging 등 후속 품질축은 아직 빈칸이다.
6. `ARCHITECTURE.md` 같은 일부 설명은 현재 doctor check 수와 최신 표면을 따라가지 못할 수 있다. 문서 truth도 doctor 가능한 계약으로 바꿔야 한다.

## 3. 세계 최고 수준의 품질 모델

자율 업그레이드는 다음 여섯 계층을 독립적으로 통과해야 한다.

| 계층 | 질문 | 필수 증거 |
| --- | --- | --- |
| 연구 신뢰성 | 주장이 최신 1차 출처와 로컬 현상으로 뒷받침되는가? | 출처, 확인 시각, 인용/스냅샷, 로컬 재현 |
| 작업 품질 | 범위, 소유 경로, acceptance, 승인 경계가 명확한가? | round/task ID, plan, todo, 위험 등급 |
| 구현 품질 | 가장 작은 변경으로 기존 계약을 보존했는가? | diff, 가까운 테스트, 비목표 목록 |
| 런타임 건강 | 실제 Code Brain/kit/consumer에서 작동하는가? | strict doctor, kit doctor, canary 결과 |
| 증거 품질 | 다른 사람이 같은 명령으로 결과를 재현할 수 있는가? | command, exit code, observed, artifact path |
| 학습 품질 | 결과가 다음 선택을 개선하고 stale memory를 만들지 않는가? | before/after, 기각 근거, 다음 todo, working-tree 재확인 |

각 계층은 `green`, `red`, `not-applicable` 중 하나여야 한다. `unknown`, 실행하지 않은 테스트, 미확인 외부 보고는 green이 아니다.

## 4. 우선 과제

### P0. 자율 라운드 계약과 추적성

가장 먼저 research → task → acceptance → evidence를 하나의 round ID로 묶는다.

필수 필드:

- `round_id`, 시작 SHA, branch, 시작 dirty 파일 목록
- 연구 질문, 출처, freshness, 로컬 재현
- 후보별 `adopt`, `defer`, `reject`와 이유
- task ID, 소유 경로, 보호 경로, 위험 등급, 승인 요구
- acceptance 명령과 예상되는 관찰
- 실제 변경 파일, 테스트·doctor·eval 결과
- reviewer verdict와 typed evidence
- 종료 상태와 다음 라운드 트리거

canonical machine record는 `.ai/outputs/autonomous-round-<round_id>.json`이며, todo·plan·loop ledger에는 같은 `round_id`를 source/tag로 기록한다. report는 시작 SHA/branch/dirty, 연구 source/freshness/local repro, task owned/protected/changed path, acceptance의 command/exit code/observed/artifact path, reviewer verdict/evidence reference, 종료 status/next trigger를 포함한다. Markdown round report는 사람이 읽는 보조 뷰로만 사용한다.

성공 기준:

- ID 없는 자동 구현 금지
- acceptance 없는 `done` 금지
- 시작 dirty 기준선 밖의 비소유 변경 0
- 실패·보류·기각도 이유와 재검토 조건을 가짐

### P0. 연구 후보를 안전한 작업으로 컴파일

연구 보고서의 자유 서술을 바로 코드 변경으로 실행하지 않는다. 각 후보는 아래 순서로 task가 된다.

1. 기존 기능과 중복 여부를 Code Brain 검색과 graph로 확인한다.
2. 로컬 코퍼스에서 현재 gap을 재현한다.
3. 영향, 안전성, 구현 크기, 검증 강도를 각각 1–5로 평가한다.
4. 필수 승인 여부와 보호 경로 충돌을 판정한다.
5. 가장 작은 acceptance 가능한 task 하나로 축소한다.
6. 같은 문제를 문서·설정·테스트만으로 해결할 수 있는지 먼저 본다.

채택 문턱:

- 로컬 gap이 재현되거나 명확한 contract drift가 있어야 한다.
- 예상 효과를 현재 eval/doctor/benchmark로 판정할 수 있어야 한다.
- 보안·승인 경계를 약화하지 않아야 한다.
- 외부 벤치 수치만 있고 자체 측정이 없으면 `defer`다.
- dependency, global install, 배포, auth가 필요하면 자동 채택하지 않는다.

### P0. 완료 게이트를 모델 독립적으로 만들기

모든 구현 task는 기본적으로 `--require-acceptance`를 사용한다. 완료 순서는 다음과 같다.

1. worker가 결과와 자체 검증을 제출한다.
2. reviewer가 rubric과 diff를 검토한다.
3. Code Brain이 acceptance 명령을 sandbox에서 다시 실행한다.
4. verdict에는 `command`, `observed`, `artifact_path`가 있는 typed evidence를 넣는다.
5. 모든 필수 acceptance가 통과한 뒤에만 `loop complete`를 허용한다.

빈 acceptance 목록, 실행하지 않은 명령, 화면의 pass badge, 에이전트의 요약 문장은 완료 증거가 아니다.

### P1. 진행 중 global-kit 현대화 라운드 정리

현재 14개 dirty 파일은 별도 소유 lane으로 먼저 완결한다. 새 기능을 더 얹기 전에 다음을 검증한다.

- source asset 정적 검증
- 임시 HOME 설치 smoke와 기존 사용자 settings 보존
- secret commit hook의 설치·실행·doctor 연결
- Codex hook event와 config schema의 현재 공식 표면 정합
- rules 축약 뒤 필수 보안·승인 문구 보존
- 설치 전 dry-run과 설치 후 doctor의 의미 분리

필수 명령은 8절의 global-kit 검증 묶음을 따른다. 실제 `~/.claude`/`~/.codex` 설치는 별도 승인 전에는 실행하지 않는다.

### P1. Doctor를 계층형 readiness 판정기로 확장

현재 source-derived 29개 strict check를 유지하면서 다음 readiness check를 순차 확장한다.

1. ✅ `autonomous_round_completeness`: bounded typed round report의 round/task/acceptance/evidence 연결을 read-only로 검증
2. `injected_context_budget`: SessionStart + UserPromptSubmit의 합계와 규칙 수 상한
3. `global_kit_source_health`: kit validate 결과를 repo doctor에서 분리 표시
4. `global_kit_install_drift`: 설치물과 source의 drift를 read-only로 표시
5. `consumer_canary`: 승인된 임시/대표 consumer에서 설치 SHA, runtime, doctor 상태 확인
6. `docs_contract_freshness`: check 목록·명령·버전 설명이 source와 어긋나면 실패

Doctor 결과는 다음 세 상태를 합치지 않는다.

- 설치 일치: source SHA, runtime, manifest, MCP 등록
- 프로젝트 건강: secret scan, index freshness, audit, storage, SLO
- 릴리스 준비: clean release snapshot, artifacts, reproducibility, rollback

### P1. 자체 eval을 의사결정 기준으로 확대

현재 `code_retrieval` 축을 기준선으로 다음 순서만 허용한다.

1. query-side 정확 심볼 부스트와 camel↔snake 변형
2. skeleton/refs-only context mode와 hashline anchor
3. 개인화 repo-map + 1-hop ego graph
4. code navigation, memory retrieval, 실제 decision logging 축

각 항목은 기존 golden query에서 Recall@K/MRR/NDCG@K를 악화시키지 않고, latency와 context byte/token 예산을 함께 통과해야 한다. LLM query rewriting, embedding/reranker 기본화, 새로운 dependency는 자체 코퍼스 우위가 입증되기 전에는 보류한다.

### P2. MCP와 컨텍스트 비용 절감

- tool schema 설명을 측정 가능한 예산으로 축소한다.
- `concise|detailed` 응답 계약을 검토한다.
- 유사 graph 도구 통합은 discovery rank와 호환성 회귀가 없을 때만 채택한다.
- always-on context는 짧고 deterministic하게 유지한다.
- 비용 절감을 기능 수 감소와 혼동하지 않고, 실제 schema bytes·응답 bytes·검색 성능을 함께 측정한다.

### P2. 메모리의 working-tree 검증

- 기억보다 현재 git/config/test 결과를 우선한다.
- recall된 결정이 다루는 파일의 현재 hash 또는 source anchor를 확인한다.
- 반복 관찰이 없는 단발성 경험은 전역 규칙로 승격하지 않는다.
- 최소 3회 독립 재등장, 모순 검사, dry-run promotion을 통과한 후보만 durable rule로 제안한다.
- stale 기억은 삭제보다 stale 표식과 대체 근거를 남긴다.

## 5. 한 라운드의 표준 폐루프

### 단계 0. Intake와 기준선 고정

- 사용자 목표와 비목표를 한 문장으로 고정한다.
- 현재 branch/SHA/dirty 상태를 저장한다.
- 기존 변경의 소유자와 작업 lane을 분리한다.
- 보호 경로, dependency manifest, 전역 설정 경로를 표시한다.
- 30분, tool call 120회, retry 2회의 기본 예산을 사용하되 작업별로 더 작게 줄일 수 있다.

중단 조건: 소유 경로 충돌, 실질 범위를 바꾸는 불명확성, 승인 필수 작업.

### 단계 1. Research

- 공식 문서·upstream·논문을 우선하고 확인 날짜를 기록한다.
- 외부 content는 untrusted candidate signal로 취급한다.
- 최소 두 종류의 근거를 대조하되, 로컬 재현이 최종 채택 기준이다.
- 기존 `.ai/outputs/research-*.md`, CHANGELOG, todo와 중복을 제거한다.
- 각 주장을 `verified`, `candidate`, `rejected` 중 하나로 기록한다.

### 단계 2. Task 생성

후보 하나를 독립 acceptance가 가능한 최소 task로 변환한다.

```bash
.ai/bin/ai memory todo add \
  --title "<round-id>: <bounded task>" \
  --owner "<agent-or-human>" \
  --tag autonomous-upgrade \
  --source "<research-artifact>" \
  --json

.ai/bin/ai plan init \
  --id "<round-id>" \
  --title "<round goal>" \
  --step "baseline captured" \
  --step "gap reproduced" \
  --step "minimal change implemented" \
  --step "acceptance rerun" \
  --step "evidence reviewed" \
  --step "reanalysis complete" \
  --json
```

큰 후보를 한 번에 구현하지 않는다. 한 task는 한 품질 가설과 한 rollback 가능한 변경 단위를 가진다.

### 단계 3. 최소 구현

구현 사다리는 아래에서 위로 한 단계씩만 올린다.

1. 코드 변경 없음: 기존 명령, 문서, 설정으로 해결
2. 기존 규칙·테스트·평가 케이스 보강
3. 기존 함수의 작은 변경
4. 검증된 두 개 이상의 사용처가 있을 때만 새 abstraction
5. 자체 측정으로 필요성이 입증되고 승인된 경우에만 dependency 또는 설치 표면 변경

각 반복은 실패 재현 → 최소 diff → 가까운 테스트 순서다. task가 요구하지 않은 리팩터링, 명칭 정리, formatting sweep는 별도 후보로 돌린다.

### 단계 4. Strict doctor, 테스트, eval

검증은 가까운 것부터 넓힌다.

1. 변경 파일의 구문·정적 check
2. 가장 가까운 단위 테스트
3. 영향 모듈 테스트
4. `make lint`
5. `make eval` 또는 관련 축
6. `.ai/bin/ai doctor --strict --json`
7. global-kit 변경이면 kit validate/doctor
8. 설치·패키지 경계면 smoke/reproducibility/rollback
9. 릴리스 요청이 있는 경우에만 full release gate와 원격 CI

Doctor 실패를 자동 우회하거나 allowlist로 숨기지 않는다. 실패를 설치 drift, 프로젝트 건강, 릴리스 readiness로 분류하고 원인을 고친다.

### 단계 5. Evidence와 독립 판정

```bash
.ai/bin/ai loop submit \
  --goal "<bounded goal>" \
  --file "<task-or-round-artifact>" \
  --rubric-file "<rubric>" \
  --checklist "<must preserve unrelated dirty changes>" \
  --priority P1 \
  --require-acceptance \
  --json
```

worker lease 후 acceptance는 실제 request/lease ID로 다시 실행한다.

```bash
.ai/bin/ai loop acceptance \
  --request-id "<request-id>" \
  --lease-id "<lease-id>" \
  --command "<closest test>" \
  --command ".ai/bin/ai doctor --strict --json" \
  --json
```

Reviewer verdict에는 최소한 다음이 있어야 한다.

```json
[
  {
    "command": "exact runnable command",
    "observed": "exit 0; exact pass count or check result",
    "artifact_path": "durable report or bounded log path"
  }
]
```

제3자 agent transcript, UI badge, 과거 CI 결과는 보조 맥락일 뿐 현재 checkout의 실행 증거가 아니다.

### 단계 6. 재분석

- 단계 1의 질문과 로컬 재현을 같은 입력으로 다시 실행한다.
- before/after metric, latency, context size, doctor 결과를 비교한다.
- 개선이 없거나 비용이 더 크면 변경을 확대하지 않고 기각 또는 rollback 후보로 분류한다.
- 새로 드러난 문제는 현재 task에 끼워 넣지 않고 todo로 만든다.
- 완료된 todo/plan step을 닫고 다음 최고 신호 후보 하나만 선택한다.

## 6. Kiro 오케스트레이션·이벤트 기반 감독 계약

### 6.1 Top-level Kiro의 허용 범위

Top-level Kiro는 오케스트레이터이며 implementation worker가 아니다. 허용되는 동작은 다음뿐이다.

- repo와 task의 read-only 상태 수집
- native task 생성·상태 전이·우선순위 관리
- subagent dispatch, 이벤트 기반 상태 reconcile
- approval blocker 격리와 안전한 다른 task 선택
- analyst·writer·verifier 결과를 현재 repo/process truth와 대조

Top-level Kiro는 제품 코드나 테스트 코드를 생성·수정·삭제하지 않는다. 테스트, 빌드, formatter, package, install 명령도 직접 실행하지 않는다. 필요한 구현과 실행 검증은 해당 repo의 단일 implementation writer에게 위임한다. Kiro가 직접 확인하는 것은 status, diff, task state, process liveness, agent output 같은 read-only 감독 표면뿐이다.

### 6.2 Repo별 agent topology

각 repo의 한 wave는 다음 순서를 지킨다.

1. **병렬 read-only analysts**: 코드·문서·config·테스트 구조, 위험, acceptance 후보를 독립 조사한다. 파일 수정, 테스트·빌드 실행, task 완료 처리는 금지한다.
2. **정확히 1명의 implementation writer**: analyst 결과를 reconcile한 단일 spec 또는 general task execution을 소유한다. 해당 repo의 제품·테스트 코드 편집과 테스트·빌드 실행 권한은 이 writer만 가진다.
3. **별도 read-only verifier**: writer와 다른 agent가 현재 diff, 명령 결과, doctor/eval evidence, 비소유 변경 유무를 검토한다. 수정이 필요하면 직접 고치지 않고 writer에게 fail 사유를 반환한다.
4. **Kiro reconcile**: verifier 결과와 native task 상태를 대조하고 다음 상태를 결정한다.

한 repo에 둘 이상의 implementation writer를 동시에 두지 않는다. 여러 repo는 repo별 단일 writer 원칙을 지키는 범위에서 병렬화할 수 있다. writer 교체가 필요하면 이전 writer가 inactive임을 확인하고 lease·owned paths·마지막 검증 checkpoint를 명시적으로 인계한 뒤 교체한다.

### 6.3 Exact completion handshake

Verifier의 최종 성공 출력은 정확히 다음 토큰이어야 한다.

```text
VERIFIED_PASS
```

설명, 부분 성공, writer의 `done`, UI badge, task progress 100%, 테스트 로그만으로 native task를 Done으로 전환하지 않는다. Kiro는 현재 checkout과 연결된 verifier가 정확한 `VERIFIED_PASS`를 반환했고 approval blocker와 필수 acceptance가 모두 해소된 뒤에만 Done을 기록한다.

Verifier가 근거 부족, stale evidence, 비소유 diff, 실패 gate를 발견하면 `VERIFIED_PASS`를 출력하지 않고 구체적인 fail fingerprint와 writer가 재실행할 acceptance만 반환한다.

### 6.4 이벤트 기반 supervisor 상태기계

Kiro는 agent event, command completion, process exit, native task state transition 또는 사용자 요청이 들어올 때만 active writer/native task를 reconcile한다. 각 관찰은 `task_id`, repo, writer, timestamp, 마지막 신규 event, process/task state, fingerprint를 기록한다. 타이머, `sleep`, 주기 status 요청, 자동 wake-up은 만들지 않는다.

1. **Fresh ACTIVE event**: 새 agent event, diff, commit, 명령 결과 또는 process activity가 있으면 상태만 기록하고 별도 재관찰을 예약하지 않는다.
2. **명시적 status/resume**: 사용자 요청이나 task resume event가 있을 때만 writer와 실제 process/task liveness를 한 번 확인한다.
3. **Inactive 확인**: inactive 또는 유실이면 마지막 검증 checkpoint, owned paths, acceptance를 보존해 같은 task를 한 번 재개한다.
4. **진행 재개 event**: 새 activity를 현재 증거로 기록하며 과거 출력을 새 증거로 재사용하지 않는다.

`ACTIVE`는 UI label만으로 판정하지 않는다. agent event cursor, process liveness, repo diff/log 변화 중 하나 이상의 fresh 근거가 필요하다.

### 6.5 Failure fingerprint와 circuit breaker

Fingerprint는 최소한 실패 command, exit code 또는 exception class, 정규화한 핵심 오류, affected path, acceptance gate를 포함한다.

- 동일 fingerprint가 연속 3회 발생하면 해당 task의 circuit을 `open`으로 전환한다.
- circuit-open task는 자동 재시도·agent 교체·같은 명령 반복을 중지하고 blocker evidence를 남긴다.
- Kiro는 소유 경로가 겹치지 않고 승인 없이 수행 가능한 다른 안전 task를 선택한다.
- 환경·코드·승인 상태가 실제로 바뀌고 새 fingerprint 근거가 생긴 뒤에만 circuit을 half-open으로 재검토한다.
- 명령 문자열만 바꾸거나 agent를 바꿔 동일 원인을 숨기는 것은 새 fingerprint가 아니다.

### 6.6 Approval gate 격리

승인이 필요한 task만 `approval-blocked`로 격리한다. Kiro는 승인을 추정하거나 우회하지 않고, 같은 승인을 반복 요청하지 않으며, 독립적인 안전 task의 analyst→writer→verifier wave는 계속한다.

approval event 또는 사용자 응답이 들어올 때만 승인 상태를 reconcile한다. 승인이 들어오면 그 task만 원래 checkpoint에서 재개하고, 거절되면 `rejected` 또는 `deferred`로 닫는다. 한 task의 auth, package, install, deploy, release 승인이 다른 task로 전파되지 않는다.

### 6.7 Global-kit 명령 계약

`kits/global-agent-kit` 변경 wave의 implementation writer는 다음 native command 경계를 유지한다.

1. 변경 전 조사: `/kit-research`
2. 설치·구성 진단: `/kit-doctor`
3. 한 번의 research/evaluate/implement/verify wave: `/kit-upgrade-loop`

`/kit-upgrade-loop` 한 번을 통과하지 않은 여러 변경 wave를 한 task에 누적하지 않는다. Top-level Kiro는 이 명령들을 대신 실행하지 않고, writer의 실행 결과와 별도 verifier의 `VERIFIED_PASS`를 reconcile한다. 전역 설치나 승인 필요 동작은 기존 approval gate를 그대로 따른다.

### 6.8 CPU/RAM fleet 안전 게이트

Top-level Kiro는 implementation writer dispatch 직전과 heavy command 종료 이벤트 직후에만 macOS host의 CPU/RAM 상태를 read-only로 표본화한다. 이 점검은 제품·테스트 코드 수정이나 테스트·빌드 실행이 아니며, fleet capacity를 결정하기 위한 상태 수집이다. 주기 표본은 예약하지 않는다.

#### 필수 표본

각 표본은 timestamp와 함께 다음을 기록한다.

- logical core 수와 총 RAM
- `uptime`의 1분 load average
- `memory_pressure -Q`의 system-wide free percentage와 pressure warning
- `vm_stat`의 `Pages throttled`
- `vm.swapusage`의 used 값과 직전 표본 대비 delta
- CPU 상위 process와 RSS 상위 process
- 실행 중인 compiler, build, test, package, index, release-gate 같은 heavy process
- 실행 중 heavy task의 repo, native task ID, writer

권장 macOS read-only probe:

```bash
sysctl -n hw.logicalcpu
sysctl -n hw.memsize
uptime
memory_pressure -Q
vm_stat
sysctl vm.swapusage
ps -axo pid,ppid,%cpu,rss,etime,state,comm | sort -k3 -nr | head -n 20
ps -axo pid,ppid,%cpu,rss,etime,state,comm | sort -k4 -nr | head -n 20
```

Process 표본에는 raw argv나 environment를 기록하지 않는다. command line에 credential이나 token이 포함될 수 있으므로 `comm`과 native task metadata만 사용하고, 산출물에는 기존 redaction 규칙을 적용한다.

#### 16-core/64 GiB 기준 상태

다음 표는 이 host의 16 logical cores, 64 GiB RAM profile에 적용한다. 둘 이상의 조건이 충돌하면 더 나쁜 상태를 선택한다.

| 상태 | 판정 조건 | 새 heavy dispatch 한도 |
| --- | --- | --- |
| **GREEN** | free memory `>=30%`, `load1 < 12`, `Pages throttled = 0`, pressure warning과 swap 급증 없음 | fleet 전체 최대 2개, repo당 최대 1개 |
| **YELLOW** | RED 조건은 없고, free memory `20% 이상 30% 미만` 또는 `12 <= load1 < 16` 또는 pressure warning | fleet 전체 최대 1개, repo당 최대 1개 |
| **RED** | free memory `<20%` 또는 `load1 >= 16` 또는 `Pages throttled > 0` 또는 직전 표본 대비 swap 급증 | 새 heavy 0개, read-only analysis만 허용 |

Heavy에는 테스트·빌드·compiler·package·installer·index rebuild·release gate와 이에 준하는 CPU/RAM 집중 명령을 포함한다. 단순 status/diff/read, analyst 조사, verifier의 기존 evidence 검토는 heavy slot을 사용하지 않는다.

Swap 급증은 원시 `used` 값과 직전 이벤트 표본 대비 delta를 반드시 남긴다. fleet profile에 수치 기준이 설정되어 있으면 그 기준을 사용하고, 기준이 없는데 material한 증가가 보이면 GREEN으로 낙관 판정하지 않고 RED로 격리해 운영자 확인을 받는다.

#### 두 표본 hysteresis

- fleet 상태는 최근 유효 표본 두 개를 보존하고 더 나쁜 상태를 현재 dispatch state로 사용한다.
- 한 번의 더 나쁜 표본은 안전을 위해 즉시 capacity를 낮춘다.
- RED→YELLOW/GREEN 또는 YELLOW→GREEN처럼 capacity를 높이는 전이는 서로 다른 heavy lifecycle 경계에서 얻은 두 연속 표본이 모두 새 상태 조건을 만족해야 한다.
- 표본 하나가 누락되거나 probe가 실패하면 이전의 더 보수적인 상태를 유지한다. 누락을 GREEN으로 간주하지 않는다.
- repo당 단일 implementation writer 제한은 GREEN에서도 그대로 유지된다.

#### 실행 중 command와 cooldown

상태가 YELLOW 또는 RED로 내려가도 PID나 process 이름만 보고 임의 kill하지 않는다. 실행 중인 atomic test/build/package 명령은 새 heavy dispatch를 막은 채 안전하게 마무리하도록 둔다. 취소가 꼭 필요하면 owning writer가 checkpoint와 산출물 상태를 확인한 뒤 graceful cancellation을 제안하고, 승인 경계가 있으면 사용자 결정을 받는다.

Atomic command 종료 이벤트 직후 재표본한다. capacity 증가는 두 표본 hysteresis를 통과한 뒤에만 허용한다. Resource RED는 구현 실패 fingerprint로 세지 않으며, 같은 task를 무의미하게 재시도하지 않고 read-only 분석이나 서로 독립적인 안전 task로 전환한다.

#### 현재 기준 표본

문서 보완 시 관찰된 기준값은 16 cores, 64 GiB에서 `load1=3.74`, free memory `88%`, `Pages throttled=0`이다. 단일 Kiro renderer가 약 `99% CPU`를 사용했지만 전체 core/load/memory 기준은 GREEN이었다. 따라서 renderer를 임의 종료하지 않고 top CPU process로 계속 기록한다.

이 값은 시점 표본이지 영구 capacity 증거가 아니다. 모든 새 heavy dispatch 직전 최신 표본과 두 표본 hysteresis를 다시 적용한다.

## 7. 작업 계약

모든 task는 구현 전에 다음 내용을 가진다.

```yaml
round_id: AU-YYYYMMDD-NN
goal: 한 문장으로 표현한 품질 가설
baseline:
  sha: 현재 HEAD
  branch: 현재 branch
  dirty_paths: 시작 시점의 변경 경로
owned_paths:
  - 이 task가 변경할 수 있는 경로
protected_paths:
  - 기존 dirty 또는 승인 필수 경로
non_goals:
  - 이번 task에서 하지 않을 일
risk: low | medium | high
orchestration:
  top_level: kiro
  analysts: read-only
  implementation_writer: exactly-one-per-repo
  verifier: read-only
  monitor_seconds: 300
  fleet_gate: green | yellow | red
approval_required:
  - 필요한 승인 또는 []
acceptance:
  - 실행 가능한 명령
evidence:
  - command
  - observed
  - artifact_path
rollback:
  - 변경 취소 또는 기능 kill-switch 절차
```

`owned_paths` 밖의 변경이 발견되면 자동으로 고치거나 되돌리지 않는다. 현재 task가 만든 것이 아니면 기준선에 추가하거나 owner 판단을 요청한다.

## 8. 프로젝트 고유 검증 명령

### 8.1 Code Brain 코어

세션과 현재 상태:

```bash
git status --short --branch
git log -1 --oneline
.ai/bin/ai version
.ai/bin/ai session start \
  --agent codex \
  --rebuild auto \
  --repair-audit-index \
  --strict \
  --json
```

가까운 검증과 공통 게이트:

```bash
uv run --project .ai/runtime python -m pytest \
  .ai/runtime/tests/test_<affected_area>.py
make lint
make eval
.ai/bin/ai doctor --strict --json
.ai/bin/ai report status --json
git diff --check
```

문서·통합 표면:

```bash
make docs-check
make quick
make ci
```

`make test`는 공유 계약이 넓게 바뀌었거나 전체 회귀가 필요한 경우에 실행한다. 검색·랭킹·도구 discovery 변경은 `make eval`을 생략할 수 없다.

### 8.2 Global agent kit

Repo source 검증:

```bash
make -C kits/global-agent-kit validate
make -C kits/global-agent-kit codex-doctor
kits/global-agent-kit/install.sh --all --dry-run
```

설치 상태 검증:

```bash
make -C kits/global-agent-kit doctor
kits/global-agent-kit/scripts/doctor.sh --target "$PWD"
```

연구·반복 표면:

```bash
kits/global-agent-kit/scripts/research-snapshot.sh
kits/global-agent-kit/scripts/dev-loop.sh --once
kits/global-agent-kit/scripts/evolve-promote.sh --dry-run
```

`doctor`는 현재 설치된 global asset이 source와 다르면 실패할 수 있다. 이 실패는 source validation 실패가 아니라 install drift로 별도 보고한다. `install.sh --all --yes`, `make install-*`, `harness-install-once`는 전역 파일을 바꾸므로 명시 승인 없이는 실행하지 않는다.

### 8.3 패키지·릴리스

패키지 경계를 변경한 승인된 task:

```bash
make smoke
make package
make verify-artifacts
make install-check
make reproducibility-check
make tamper-check
make rollback-drill
```

최종 릴리스 후보:

```bash
make release-gate
.ai/bin/ai report release-gate-summary --json
```

로컬 release gate 통과는 push, tag, GitHub Release, 배포 승인이 아니다. 원격 변경은 별도 승인과 원격 증거가 필요하다.

### 8.4 Consumer canary

consumer 업그레이드는 대표 repo 한 곳에서 먼저 한다. 아래 작업은 대상 checkout을 변경하므로 사용자가 지정하거나 승인한 target에서만 실행한다.

```bash
make upgrade-in TARGET=/approved/consumer/repo
/approved/consumer/repo/.ai/bin/ai version
/approved/consumer/repo/.ai/bin/ai index rebuild --json
/approved/consumer/repo/.ai/bin/ai doctor --strict --json
```

canary 보고는 source SHA/runtime, manifest, MCP, storage, audit, index freshness, secret scan을 개별 상태로 남긴다. 한 항목의 성공으로 전체 consumer 건강을 추정하지 않는다.

## 9. 승인 게이트

### 자동 진행 가능한 범위

- repo/config/docs/tests의 read-only 분석
- 공식 자료의 read-only 조사
- 사용자가 요청한 repo와 소유 경로 안의 최소 구현
- 로컬 lint, unit test, deterministic eval, strict doctor
- `.ai/outputs`의 연구·증거 문서와 local todo/plan 기록
- installer dry-run, promotion dry-run

### 실행 전 명시 승인이 필요한 범위

- auth, credential, OAuth, billing
- 실제 secret, production config, password store
- 데이터·DB 삭제 또는 비가역 migration
- package/dependency 추가·업데이트
- `~/.claude`, `~/.codex` 등 전역 설치
- consumer repo를 변경하는 install/upgrade
- deployment, release, publish, tag
- main/production push 또는 merge
- force-push, history rewrite, protected branch 삭제
- remote memory sync 또는 외부 시스템 write

승인은 한 작업에만 유효하다. 예를 들어 “패키지 빌드 승인”은 publish나 GitHub Release 승인이 아니다.

### 자동 기각 조건

- 보안·redaction·approval 경계를 약화하는 자기개선 규칙
- 실제 secret을 읽어야만 검증 가능한 설계
- 외부 벤치만 있고 로컬 acceptance가 없는 기능
- dirty 소유권을 알 수 없는 파일을 덮어쓰는 변경
- 테스트 삭제·완화로 green을 만드는 변경
- 실패한 doctor를 allowlist 또는 비활성화로 숨기는 변경
- 장시간 background mutation을 사용자 요청 없이 시작하는 변경

## 10. Done, blocked, 다음 라운드

### Task done

- task 범위와 acceptance가 변하지 않았거나 변경 이유가 기록됨
- owned paths만 변경됨
- 가까운 테스트와 모든 필수 gate green
- strict doctor green 또는 task와 무관한 기존 실패가 기준선과 동일함
- typed evidence가 현재 checkout에서 재현됨
- 별도 read-only verifier가 정확히 `VERIFIED_PASS`를 반환함
- rollback 또는 kill-switch가 검토됨
- todo와 plan step이 닫힘

### Round done

- 모든 후보가 `adopted`, `deferred`, `rejected` 중 하나
- adopted task가 모두 acceptance와 reviewer gate를 통과
- 미해결 P0/P1이 없거나 승인/외부 상태 blocker로 명시됨
- 시작 dirty 기준선 밖의 비소유 diff 증가가 없음
- before/after와 기각 근거가 round artifact에 남음
- 다음 라운드 후보는 하나만 in-progress이며 나머지는 pending

### Blocked

다음 중 하나일 때만 blocked다.

- 필요한 승인 또는 사용자 선택이 없음
- 같은 외부 상태 실패가 반복되고 로컬 대안이 없음
- 소유권 충돌로 안전한 편집 범위를 결정할 수 없음
- 재현에 필요한 시스템·consumer·credential 접근이 없음

시간이나 토큰이 부족하다는 이유만으로 완료 처리하지 않는다. 예산이 끝나면 수행한 명령, 관찰, 남은 acceptance를 남기고 중단한다.

### 다음 라운드 트리거

- 공식 Claude/Codex/MCP 표면 변화
- strict doctor 또는 eval 회귀
- 실제 consumer의 설치·검색·메모리 incident
- 동일한 사용자 correction 또는 실패가 3회 재등장
- context/schema/storage/SLO 예산 초과
- 새 연구가 현재 코퍼스에서 측정 가능한 우위를 보임

트리거가 없으면 반복을 멈춘다. “항상 더 개선할 수 있음”은 다음 라운드의 근거가 아니다.

## 11. 권장 첫 실행 순서

1. **현재 global-kit dirty lane 완결**: 기존 14개 파일을 추가 확장하지 않고 validate, Codex doctor, install dry-run, 설치 drift를 분리 판정한다.
2. **Round contract 최소 도입**: 새 오케스트레이터보다 먼저 round ID, baseline, owned paths, acceptance, evidence 템플릿을 실제 한 task에 적용한다.
3. **Doctor 완결성 check 설계**: `autonomous_round_completeness`와 `injected_context_budget`부터 작은 테스트로 시작한다.
4. **작은 retrieval 개선**: query-side symbol exact boost를 `code_retrieval` golden cases로 판정한다.
5. **Skeleton mode**: context bytes와 정답 파일/심볼 유지율을 함께 측정한다.
6. **Repo-map/graph**: 앞선 작은 변경이 충분하지 않을 때만 PageRank/ego-graph로 확장한다.
7. **MCP·memory 후속**: schema budget과 working-tree-aware recall을 별도 라운드로 수행한다.

이 순서는 큰 자율 오케스트레이터를 먼저 만드는 대신, 현재 존재하는 todo, plan, loop acceptance, typed evidence, doctor, eval 표면을 실제 한 라운드에서 연결해 빈틈을 측정한다. 측정으로 확인된 빈틈만 다음 구현 task가 된다.
