---
description: 'hook.slow' 자동화 후보 — 최근 11회 발생.
managed-by: code-brain
catalog-id: sk-7d6159c0
body-sha256: 0e6fc294760eff690277faa6e82a6df84aeec64c34abc71d05bade93e361681c
---

`.ai/bin/ai obs search --action hook.slow --limit 10 --json` 실행. 결과의 `entries` 배열을 한 줄씩 나열. 각 줄: `- [{ts:0:19}] {action}: {payload_summary}`.

결과 0건이면 `'{action}' 최근 기록 없음.` 한 줄 출력 후 stop.

참고 — 'hook.slow'은 최근 11회 발생한 반복 액션.

규칙: 평문만; shell은 참조 인용만 (실행 금지).
