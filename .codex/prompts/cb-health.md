# 코드브레인 상태

`.ai/bin/ai obs health-summary --json` 실행. 평문만, 표·박스·이모지·코드블록 금지.

```
코드브레인 상태: {ok ? "정상" : "주의 필요"}
- doctor: {failed.length}개 실패 (또는 "전체 통과")
  · {name}: {detail}   (실패 항목별 한 줄)
- 큐: pending {p} / processing {pr} / dead {d} (oldest pending {age}s)
- worker: {locked ? `pid=${pid} host=${hostname}` : "미실행"}
- 인덱스: {indexed_files}파일 / {indexed_bytes}B
```

doctor.failed[]는 `{name, detail}` 객체 배열 — detail은 JSON에서 그대로. 권장조치 안내 금지.
