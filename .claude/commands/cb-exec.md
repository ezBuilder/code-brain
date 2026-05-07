---
description: 코드브레인 sandbox 셸 실행 — grep/find 등 긴 출력을 토큰 절약하며 실행.
argument-hint: "<command and args after -- (예: -- grep -rn pattern src/)>"
---

`$ARGUMENTS`가 비었으면 `사용법: /cb-exec -- <command> [args...]` 한 줄 출력 후 stop.

`.ai/bin/ai exec run --json -- $ARGUMENTS` 실행 후 정확히 다음 형식으로 출력. 표·박스·이모지·코드블록 금지.

```
sandbox 실행: exec_id={exec_id} exit={exit_code}
- 출력: {total_lines}줄 / {total_bytes} B (cache: .ai/cache/sandbox/{exec_id}.txt)
- 첫 줄들:
{first_lines.join("\n")}
- 마지막 줄들 (있으면):
{last_lines.join("\n")}
```

추가 줄 조회: `.ai/bin/ai exec fetch --exec-id {exec_id} --line-start 1 --line-end 200 --json` 또는 `--grep <패턴>`.

규칙:
- 자동 fetch 금지 — 사용자가 exec_id로 명시 요청 시에만 추가 실행.
- 본문 가공 금지. JSON에서 그대로.
- 위 형식 외 한 글자도 추가하지 않는다.
