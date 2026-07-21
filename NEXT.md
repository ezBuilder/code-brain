# CodeBrain 다음 재개 지점

기록일: 2026-07-21

## 기존 실행 식별자

- projectId: `code-brain`
- harnessId: `local_harness_eb38109b8f284539`
- leaseId: `lease_0cfb2876-5acf-4a19-8329-adf64e80cd16`
- branch: `c2c/session/lease0cf/code-brain`
- 다음 진행 Wave: `22`
- 격리 worktree: `/Users/ezbuilder/.local/share/chatgpt2codex/worktrees/lease_0cfb2876-5acf-4a19-8329-adf64e80cd16/code-brain`

새 하네스나 새 worktree를 만들지 말고 위 lease/harness를 그대로 재사용한다. 기존 미커밋 변경, 생성된 테스트·평가 자산, 인덱스와 검증 증거를 reset/stash/checkout/clean/delete 하지 않는다.

## 2026-07-21 완료 내용

1. 디스크·메모리 상한
   - audit/JSONL 자동 compaction·rotation·보존 상한과 체인/index 복구를 구현했다.
   - SQLite DB+WAL+SHM 절대 용량 상한, checkpoint, prune, 조건부 VACUUM을 구현했다.
   - diagnostics, 세션/운영 로그, 업그레이드 rollback backup에 통합 retention 정책을 적용했다.
   - sandbox 출력은 tempfile streaming, 4MB 보존 상한, 64MB 원본 출력 하드 상한으로 전환했다.

2. `Killed: 9`·실행기 장애 진단
   - host/cgroup 메모리 전후 상태, process-tree peak RSS, SIGKILL 원인 분류를 저장한다.
   - runner observer를 schema v5까지 강화해 timeout, spawn/observer failure, descendant cleanup, RUN_NOT_FOUND와 transport restart 징후를 fail-closed 관측한다.
   - release gate는 현재 실행과 결합된 evidence token이 없는 오래된 성공 기록을 거부한다.

3. bounded transcript·운영 진단
   - Claude/Codex transcript 스캔에 파일·라인·총량·후보·세션·시간·dedupe 상한과 명시적 partial 진단을 추가했다.
   - sandbox 실행 메타 탐색도 newest-first fixed-size heap과 byte/time 상한으로 제한했다.
   - release summary schema v3 operational bounds에 audit, SQLite, retention, transcript, sandbox, runner 상태를 통합했다.

4. 검색·메모리·컨텍스트 품질
   - Recall@K, MRR, NDCG, latency 기반 deterministic eval을 code retrieval과 memory retrieval에 추가했다.
   - BM25 shortlist 내부 rerank 한계를 없애고 bounded 독립 dense 후보 + RRF fusion을 구현했다.
   - camelCase/snake/path/Unicode 식별자, temporal validity, relations, provenance, duplicate suppression을 반영한 durable-memory recall을 구현했다.
   - query-aware active context compression, negative/protected evidence 보존, redundancy 제거와 절감·coverage 계측을 구현했다.
   - `codebrain.retrieval.v1` 공통 bounded observation을 search, memory recall, context compression, health/report에 연결했다.

5. 정밀 코드 탐색
   - Python import alias, relative import, same-file symbol, self/cls member를 해석하는 syntactic codegraph를 강화했다.
   - LSP 성공 시 precise 결과, 실패 시 read-only syntactic fallback을 반환하도록 구현했다.
   - 함수 chunk provenance/source range와 원본 source slice hash 검증을 추가했다.
   - 기존 schema-v11 인덱스에 `target_leaf`가 없는 과도기 상태도 명시적 rebuild에서 안전하게 감지·재생성하도록 호환 마이그레이션을 보강했다.

6. Codex 훅 활성화 UX
   - README와 한국어·영어·중국어·일본어·스페인어·프랑스어·독일어 문서에 프로젝트 신뢰 및 `/hooks` 승인 절차를 명시했다.
   - macOS/Linux·Windows·직접 설치·업그레이드 출력과 install manifest/upgrade JSON에 수동 활성화·재승인 안내를 추가했다.
   - 실제로는 감사 기록만 하는 `PermissionRequest` 상태 문구를 `Recording Code Brain approval request`로 수정했다.
   - 훅 등록만으로 자동 활성화된다는 기존 오해 문구를 제거했다.

## 대표 검증 증거

- 전체 테스트 최근 증거: `1558 passed, 5 skipped`, 이후 Wave별 변경 범위 회귀 통과.
- deterministic eval 최근 증거: 7개 축 `30/30` 통과.
- `make lint`, `make docs-check` 반복 통과.
- 독립 offline Git snapshot 전체 release gate 통과 증거 보유.
- 실제 runner telemetry에서 성공 실행, `killed_9=false`, `transport_restart=false`, RSS sample error 0을 확인했다.

## 다음 하네스 우선순위

1. `ai doctor --hooks` 또는 동등한 `hook_activation` readiness 진단을 구현한다.
   - `configured`: hooks.json과 feature flag가 정상 등록됨.
   - `observed`: 최근 `SessionStart`/`PreToolUse`/`PostToolUse` 이벤트가 실제 기록됨.
   - `unverified`: 등록은 됐지만 실제 실행 증거가 없음. `/hooks` 승인 절차를 출력한다.
   - Codex 내부 신뢰 저장소를 공식·안정적으로 읽을 수 없으면 승인됨을 추측하지 말고 `unverified`로 표시한다.
2. `.codex/hooks.json` 관리 블록의 canonical fingerprint를 install manifest에 저장하고, 업그레이드 전후 변경 시 `REAPPROVAL_REQUIRED`를 출력한다.
3. 실제 Codex UI/CLI dogfood에서 프로젝트 신뢰 → `/hooks` 승인 → 새 세션 → `SessionStart` 기록까지 E2E 증거를 남긴다.
4. `_read_hook_state_text()`가 아직 연결되지 않은 audit cooldown/recommendation/satisfaction, autonomous accept cooldown, compact meta, env-version reader를 root-confined reader로 통일하고 symlink/hardlink 회귀를 완료한다.
5. secret literal 없이 `test_secret_search_parity.py` fixture를 복구한다.
6. 변경 범위 테스트 → `make eval` → `make lint` → `make docs-check` → 전체 observed test 및 독립 snapshot release gate 순으로 최종 검증한다.

## 운영 제약

- 기존 변경을 초기화하거나 삭제하지 않는다.
- `.chatgpt2codex/` 실행 산출물은 커밋하지 않는다.
- 사용자가 명시하지 않은 commit·push·merge·배포·설치는 하지 않는다.