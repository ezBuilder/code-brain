# 코드브레인 검색

쿼리 받아 `.ai/bin/ai obs search --query "<쿼리>" --json` 실행. 평문만, 표·박스·이모지 금지.

exit 0이면:
```
코드브레인 검색: N건
- {path}: {snippet}
```
상위 5개. snippet은 JSON 그대로.

exit 13이면 첫 줄을 `코드브레인 검색: N건 (인덱스 stale K개 — 갱신: ai index rebuild --json)`으로 대체. 이어서 결과 동일. 자동 rebuild 금지.
