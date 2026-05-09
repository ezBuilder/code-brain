---
description: 코드브레인 슬래시 명령 추천 — 누적 메모리에서 후보 N개.
argument-hint: "[limit]"
---

`$ARGUMENTS`가 비었으면 limit=5, 숫자면 그 값. 그 외 입력은 limit=5로 고정.

`.ai/bin/ai recommend skills --limit ${limit:-5} --json` 실행.

**표·박스·이모지·헤더 모두 금지.** 코드 블록 금지. 평문만.

`candidates` 배열이 비었으면 한 줄로:
```
추천 후보 없음 ({note}).
```

비어있지 않으면 다음 형식으로:
```
추천 후보 {n}건 — 승인은 ai recommend skills accept <id>:
- {id} | {slug} — {description}
- {id} | {slug} — {description}
...
```

각 후보별 evidence는 출력하지 않는다. 자동 accept 금지. 자동 rebuild/refresh 금지.

위 형식 외 한 글자도 추가하지 않는다.
