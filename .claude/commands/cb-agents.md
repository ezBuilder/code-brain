---
description: 코드브레인 sub-agent 추천 — 누적 transcripts에서 후보.
argument-hint: "[limit]"
---

`$ARGUMENTS`가 비었으면 limit=5, 숫자면 그 값.

`.ai/bin/ai agents recommend --limit ${limit:-5} --json` 실행.

**표·박스·이모지·헤더 모두 금지.** 코드 블록 금지. 평문만.

`candidates` 배열이 비었으면 한 줄로 `agents 추천 후보 없음 ({note}).`.

비어있지 않으면:
```
agents 후보 {n}건 — 승인은 ai agents accept <id>:
- {id} | {slug} — {description}
- {id} | {slug} — {description}
...
```

자동 accept 금지. 위 형식 외 한 글자도 추가 금지.
