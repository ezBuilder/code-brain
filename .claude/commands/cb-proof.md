---
description: 코드브레인 검색 효과 검증 — legacy/v2 A/B·PPR·결정론·무증가·지연.
argument-hint: "[검증 쿼리]"
---

`$ARGUMENTS`가 비었으면 `.ai/bin/ai context prove --json`, 있으면 `.ai/bin/ai context prove "$ARGUMENTS" --json` 실행. exit 코드 보존.

평문만 출력한다.

```text
코드브레인 효과 검증: {ok ? "통과" : "실패"}
- 쿼리: {query} ({query_selection})
- 효과: {effect.status}
- graph/PPR: {v2.graph_ranking_applied ? `적용 · ${v2.ranked_node_count}노드` : "정책 활성 · 적용 가능한 call edge 없음"}
- 결정론: legacy signature {legacy.signature_count}개 / v2 signature {v2.signature_count}개 / receipt {v2.receipt_count}개
- 무증가: {durability.unchanged ? "통과" : "실패"}
- 지연 p95: legacy {legacy.p95_ms}ms / v2 {v2.p95_ms}ms
- context: {v2.context_bytes}/{v2.max_context_bytes}B
```

`expected.path`가 있으면 다음 줄을 추가한다.

```text
- 정답: legacy rank {legacy.path_rank} span {legacy.span_overlap} / v2 rank {v2.path_rank} span {v2.span_overlap}
```

실패한 `checks`는 마지막에 `- 실패: {check 이름}` 형식으로 모두 출력한다. JSON에 없는 값은 추측하지 않는다.
