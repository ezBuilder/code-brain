---
name: "source-command-cb-health"
description: "코드브레인 상태 한 줄 요약 — doctor·큐·worker·인덱스."
---

# source-command-cb-health

Use this skill when the user asks to run the migrated source command `cb-health`.

## Command Template

`.ai/bin/ai obs health-summary --json` 실행. **표·박스·이모지·헤더·해설 모두 금지.** 코드 블록 사용 금지. 평문만.

JSON 경로:
- `ok`
- `doctor.{ok, failed[]}`  (failed는 `{name, detail}` 객체 배열)
- `worker.{locked, stale, cross_host, pid, hostname}`
- `queue.{pending, processing, dead, oldest_pending_age_seconds, oldest_processing_age_seconds}`
- `index.{indexed_files, indexed_bytes}`

출력 형식:

```
코드브레인 상태: {ok ? "정상" : "주의 필요"}
- doctor: {failed.length === 0 ? "전체 통과" : `실패 ${failed.length}건`}
{failed 배열의 각 항목마다 한 줄: "  · {name}: {detail}"}
- 큐: pending {pending} / processing {processing} / dead {dead} (oldest pending {oldest_pending_age_seconds}s)
- worker: {locked ? `pid=${pid} host=${hostname}` : "미실행"}
- 인덱스: {indexed_files}파일 / {indexed_bytes}B
```

규칙:
- 실패 detail은 doctor 응답에 그대로 들어있다. "detail 미포함" 같은 추측 금지.
- 권장 조치/troubleshooting 안내 금지. 사용자가 `/cb-doctor`로 직접 확인.
- 위 형식 외 한 글자도 추가하지 않는다.
