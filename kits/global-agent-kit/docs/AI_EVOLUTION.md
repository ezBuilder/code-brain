# AI_EVOLUTION.md

Code Brain Evolution Loop는 지속 개선형 agent 운영 패턴을 Code Brain 경계에 맞게 재설계한 로컬 루프다. 모델 파라미터를 학습하지 않고, 경험 이벤트를 캡처해 후보를 점수화하고 dry-run promotion만 제안한다.

## Research Findings

- Self-evolving agent 계열은 대체로 `Read -> Execute -> Reflect -> Write` 루프를 쓴다.
- 최근 연구는 단순 chronological memory보다 success/failure 관계를 구조화한 experience graph와 skill verification을 강조한다.
- Persistent memory는 memory poisoning, stale rule, token bloat 위험이 크므로 자동 주입량과 자동 승격을 제한해야 한다.

## Loop

1. `capture`: 작업 실패, 사용자 correction, 검증 통과, 반복 후보를 JSONL로 기록한다.
2. `score`: confidence, risk, token budget, signal balance로 promote/reject를 계산한다.
3. `promote`: 전역 파일에 쓰지 않고 dry-run proposal만 출력한다.
4. `snapshot`: 설치된 Claude/Codex kit 자산을 로컬 state에 백업한다.
5. `inject`: SessionStart는 `top-context.txt`의 상위 3줄, 최대 600자만 주입한다.

## Token Budget

- Always-on context: 최대 600자.
- Scoring output: top 5 후보.
- SessionStart injection: top 3 후보.
- Full event history: 자동 주입 금지, 필요할 때만 파일로 조회.

## Memory Poisoning Defense

- Capture 단계에서 token/password/secret/API key 형태를 redaction한다.
- 외부 웹/문서 내용은 fact가 아니라 candidate signal로만 저장한다.
- Promotion은 default dry-run이며, auth, billing, production, OAuth, secret, remote sync 후보는 reject/risky로 점수화한다.
- Snapshot은 secret-like path와 symlink를 제외한다.

## Promotion

`./scripts/evolve-promote.sh --dry-run`은 현재 kit 원본과 설치된 전역 자산의 차이를 promotion proposal로만 출력한다. 실제 반영은 사용자가 승인한 뒤 기존 installer의 `--dry-run`과 명시적 install 명령으로 수행한다.

## Snapshot

`./scripts/evolve-snapshot.sh snapshot`은 `~/.local/state/code-brain-global-kit/evolution-snapshots/` 아래에 설치된 kit 자산만 로컬 백업한다. 대상은 Claude rule/settings/hooks/policies/agents/skills/commands와 Codex `AGENTS.md`이며, token/secret/password/credential처럼 보이는 경로는 건너뛴다.

## Restore Dry-Run

`./scripts/evolve-snapshot.sh restore-dry-run <snapshot>`은 복구 계획만 출력하고 전역 파일에는 쓰지 않는다. 실제 복구 apply 명령은 제공하지 않는다.
