# 코드브레인 활동 (Claude+Codex)

`.ai/bin/ai obs usage --json` 실행. 평문만, 표·박스·이모지·코드블록 금지. 천 단위 콤마.

```
코드브레인 활동
- Claude: {claude.sessions_matched}세션 · 입력 {input_tokens} / 출력 {output_tokens} / cache_read {cache_read_input_tokens}
- Codex: {codex.sessions_matched}세션 · 입력 {input_tokens} / 출력 {output_tokens} / cached {cached_input_tokens}
- 주입 바이트: {additional_context_bytes} B (hook), {mcp_response_bytes} B (mcp)
- hook 호출 {hook_events}회 / mcp 요청 {mcp_requests}회
```

source가 `*_transcript`가 아니면 그 줄을 `(transcript 없음 — source: ...)`로 대체. "saved/savings/절약" 단어 금지. 4줄 외 추가 금지.
