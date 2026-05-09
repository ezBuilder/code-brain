---
description: 코드브레인 precall 룰 추천/조회 — 누적 Bash 패턴에서 후보.
argument-hint: "[list|recommend N]"
---

`$ARGUMENTS`가 비었으면 `recommend 5`로 간주.
- `list` → `.ai/bin/ai precall list --json`
- `recommend N` (N은 숫자) → `.ai/bin/ai precall recommend --limit N --json`
- 그 외 입력 → `.ai/bin/ai precall recommend --limit 5 --json`

**표·박스·이모지·헤더 모두 금지.** 코드 블록 금지. 평문만.

`list`:
```
precall 룰 {n}건:
- {id} | {status} | {kind} | {pattern} (관찰 {observed}/{required}, 우회 {user_overrides})
- {id} | {status} | {kind} | {pattern} (관찰 {observed}/{required}, 우회 {user_overrides})
...
```

`recommend`:
```
precall 후보 {n}건 — 승인은 ai precall accept <id>:
- {id} | {kind} | {pattern} | 샘플 {sample_command}
- {id} | {kind} | {pattern} | 샘플 {sample_command}
...
```

`candidates` 또는 `rules`가 비었으면 한 줄로 `precall: 항목 없음 ({note}).`.

자동 accept/activate 금지. 위 형식 외 한 글자도 추가 금지.
