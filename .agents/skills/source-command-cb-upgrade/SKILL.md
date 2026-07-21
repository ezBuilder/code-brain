---
name: "source-command-cb-upgrade"
description: "코드브레인 업그레이드 — 공개 레포 최신 ref 적용."
---

# source-command-cb-upgrade

Use this skill when the user asks to run the migrated source command `cb-upgrade`.

## Command Template

`.ai/bin/ai upgrade latest --json` 실행. 네트워크와 파일 갱신을 수행하는 명시적 명령이다. 성공 후 Codex는 `/hooks`에서 Code Brain 훅 재승인 필요 여부를 확인하고, 새 Claude/Codex/Antigravity 세션을 열어야 새 규칙이 적용된다.

출력 규칙:
- `ok=true`: `코드브레인 업그레이드 완료 — Codex /hooks 재승인 확인 후 새 세션 필요`
- `ok=false`: `코드브레인 업그레이드 실패 — {error}`
- 표·박스·이모지·코드블록·추가 해설 금지.
