# AI_DEV_LOOP.md

이 문서는 하네스를 단순 회귀 감시가 아니라 연구, 평가, 구현 루프로 유지하기 위한 운영 기준이다.

## 루프 단계

1. 공식 자료 확인: Claude Code docs와 OpenAI Codex CLI upstream만 우선한다.
2. hook-first 확인: secret/destructive/deployment 경계가 prompt, skill, MCP보다 먼저 작동해야 한다.
3. 후보 추출: settings, hooks, commands, subagents, skills, MCP, AGENTS, sandbox, approval 흐름에서 설치-즉시-사용 가치를 찾는다.
4. 평가: 영향, 안전성, 구현 크기, 검증 가능성을 각각 1-5로 점수화한다.
5. 채택: 합계 14점 이상이고 보안/승인 경계를 약화하지 않는 후보만 구현한다.
6. 보류: credential, production, OAuth, billing, destructive action이 필요한 후보는 문서화만 한다.
7. 검증: `make validate`, `make doctor`, `./scripts/dev-loop.sh --once`를 통과해야 한다.

## 현재 채택 항목

| 항목 | 근거 | 결정 | 이유 |
| --- | --- | --- | --- |
| Claude user slash commands | Claude Code slash commands docs | 채택 | `/kit-*` 명령으로 진단, 연구, 업그레이드 루프를 바로 호출할 수 있다. |
| Claude SessionStart hook | Claude Code hooks docs | 채택 | 새 세션에 kit 사용 맥락을 짧게 주입해 별도 세팅 필요성을 줄인다. |
| MCP 자동 등록 | Claude Code MCP docs | 보류 | OAuth/token/원격 권한 설정이 필요할 수 있어 install-once 기본값으로 위험하다. |
| Codex sandbox full-auto 강제 | Codex CLI docs | 보류 | 사용자 환경의 approval/sandbox 정책을 전역 installer가 강제하면 위험하다. |
| Code Brain Skill Router | workflow routing pattern | 조건부 채택 | 반복 품질을 높이는 짧은 라우팅만 채택하고 background mutation은 거부한다. |
| Code Brain Guardrails | hook-based safety gate pattern | 채택 | 로컬 deterministic 검증과 hard-deny/approval 경계를 훅 정책으로 관리한다. |
| Code Brain Snapshot | backup-before-overwrite pattern | 조건부 채택 | 로컬 백업/retention/restore dry-run만 채택하고 원격 동기화나 credential 처리는 거부한다. |
| Code Brain Context Score | context relevance scoring | 조건부 채택 | 짧은 freshness/relevance 점수는 채택하고 always-on 대용량 context dump는 거부한다. |
| Code Brain Delivery Loop | staged delivery workflow pattern | 조건부 채택 | 계획/리뷰/QA/릴리스 점검을 필요할 때만 호출하고 항상 켜지는 훅으로 만들지 않는다. |
| Codex Skills/MCP prompts | Codex skill and MCP prompt patterns | 조건부 채택 | 문서/선택형 project-local prompt만 채택하고 OAuth/token 기반 전역 MCP 자동 등록은 거부한다. |

## 다음 후보

- project bootstrap: 대상 repo에 `.claudeignore`, local doctor, project `AGENTS.md`/`CLAUDE.md` seed를 안전하게 생성한다.
- settings diff reporter: 현재 user settings와 kit desired settings의 차이를 사람이 읽기 쉬운 표로 출력한다.
- research freshness cache: 공식 URL의 ETag/Last-Modified를 저장해 문서 drift를 감지한다.
- `cb-skill-router`: 항상 짧게 동작하는 요청 라우터. 토큰 상한을 두고 필요한 절차만 주입한다.
- `cb-delivery-loop`: `plan`, `review`, `qa`, `ship`를 호출형 명령으로 제공한다.
- `cb-snapshot`: Claude/Codex 설정 snapshot, compare, restore dry-run을 제공한다.
- `cb-context-score`: 현재 repo AI 설정의 품질 점수만 계산하고 자동 주입하지 않는다.

## 검증 연결

- Hook 변경은 `docs/AI_HOOKS.md` 기준을 먼저 갱신한 뒤 `scripts/validate.sh` required file과 `bash -n` 목록에 반영한다.
- Dev-loop 변경은 `./scripts/dev-loop.sh --once`에서 채택 후보 문서화 여부를 확인한 뒤 `validate`와 `doctor`를 실행해야 한다.
