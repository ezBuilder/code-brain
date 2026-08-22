---
name: "source-command-cb-proof"
description: "코드브레인 검색 효과 검증 — legacy/v2 A/B·PPR·결정론·무증가·지연."
---

# source-command-cb-proof

Use this skill when the user asks to run the migrated source command `cb-proof`.

## Command Template

쿼리가 있으면 `.ai/bin/ai context prove "<쿼리>" --json`, 없으면 `.ai/bin/ai context prove --json` 실행한다.

평문으로 전체 통과 여부, query/selection, effect status, graph/PPR 적용 여부와 ranked nodes, legacy/v2 signature 및 receipt 수, durability unchanged, 양쪽 p95, context bytes/max bytes를 출력한다. expected path가 있으면 path rank와 span overlap도 출력한다. 실패한 checks는 모두 나열하며 JSON에 없는 값은 추측하지 않는다.

정답 path/span 검증 원시 명령:

`.ai/bin/ai context prove "<쿼리>" --expected-path <repo-relative-path> --start-line <N> --end-line <N> --repeats 5 --json`
