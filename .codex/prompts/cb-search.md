# 코드브레인 검색

쿼리 받아 `.ai/bin/ai obs search --query "<쿼리>" --json` 실행. 평문만, 표·박스·이모지 금지.

exit 0이면:
```
코드브레인 검색: N건
- {path}: {snippet}
```
상위 5개. snippet은 JSON 그대로.

exit 13이면 첫 줄을 `코드브레인 검색: N건 (인덱스 stale K개 — 자동 refresh 실패/비활성, 갱신: ai index rebuild --json)`으로 대체. 이어서 결과 동일. CLI의 기본 자동 refresh 결과를 그대로 신뢰하고, 별도 수동 rebuild는 실행하지 않는다.
