---
description: 코드브레인 BM25 검색 — stale 자동 refresh.
argument-hint: "<검색어>"
---

`$ARGUMENTS`가 비었으면 `검색어를 입력하세요.` 한 줄 출력 후 stop.

`.ai/bin/ai obs search --query "$ARGUMENTS" --json` 실행. exit 코드 보존.

**표·박스·이모지·헤더 금지.** 코드 블록 금지. 평문만.

exit 0이면:
```
코드브레인 검색: {query.result_count}건
- {results[0].path}: {results[0].snippet}
- {results[1].path}: {results[1].snippet}
...
```
상위 5개까지. snippet은 JSON에서 그대로 (가공 금지).

exit 13이면 첫 줄을 다음으로 대체:
```
코드브레인 검색: {query.result_count}건 (인덱스 stale {query.remediation.stale_count}개 — 자동 refresh 실패/비활성, 갱신: ai index rebuild --json)
```
이어서 결과 동일하게 나열. snippet이 `[stale index: ...]`로 시작하면 그대로 출력.

규칙:
- CLI의 기본 자동 refresh 결과를 그대로 신뢰한다. 별도 수동 rebuild는 실행하지 않는다.
- 위 형식 외 한 글자도 추가하지 않는다.
