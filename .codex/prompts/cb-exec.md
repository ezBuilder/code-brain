# 코드브레인 sandbox 실행

`$ARGUMENTS`로 받은 명령을 `.ai/bin/ai exec run --json -- $ARGUMENTS`로 실행. 평문 출력만, 표·박스·이모지 금지.

```
sandbox 실행: exec_id={exec_id} exit={exit_code}
- 출력: {total_lines}줄 / {total_bytes} B
- 첫 줄들:
{first_lines}
- 마지막 줄들 (있으면):
{last_lines}
```

추가 조회: `ai exec fetch --exec-id {id} --line-start N --line-end M --json` 또는 `--grep <pattern>`. 자동 fetch 금지.
