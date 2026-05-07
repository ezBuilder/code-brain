---
description: 코드브레인 활동 — Claude+Codex 토큰 + hook/MCP breakdown + PreToolUse 차단 횟수.
---

`.ai/bin/ai obs usage --json` 실행. **정확히 아래 형식만 평문 출력.** 표·박스·이모지·코드블록·헤더·해설 모두 금지.

JSON 경로:
- `actual_token_usage.{claude,codex}.{source, sessions_matched, tokens.{input_tokens, output_tokens, cache_read_input_tokens, cached_input_tokens}}`
- `measured_code_brain_effect.{additional_context_bytes, mcp_response_bytes, hook_events, mcp_requests, pretooluse_blocks, hook_breakdown, mcp_breakdown}`

출력 (천 단위 콤마):

```
코드브레인 활동
- Claude: {claude.sessions_matched}세션 · 입력 {input_tokens} / 출력 {output_tokens} / cache_read {cache_read_input_tokens}
- Codex: {codex.sessions_matched}세션 · 입력 {input_tokens} / 출력 {output_tokens} / cached {cached_input_tokens}
- 주입: {additional_context_bytes} B (hook), {mcp_response_bytes} B (mcp 응답)
- hook {hook_events}회 [{hook_breakdown 요약: 예 "Session 18 / Prompt 35 / PreTool 12(blocked 8)"}]
- mcp {mcp_requests}회 [{mcp_breakdown 요약: 예 "code_query 4 / sandbox_execute 3 / obs_health_summary 5"}]
- ★ PreToolUse 차단 {pretooluse_blocks}회 (Code Brain이 grep 덤프 방지한 횟수)
```

규칙:
- `claude.source != "claude_transcript"` 시 첫 줄을 `- Claude: (transcript 없음 — source: <source>)`.
- `codex.source != "codex_transcript"` 시 둘째 줄 동일 패턴.
- hook_breakdown 요약: 카운트 desc 정렬, 상위 4개만, "이름 N(blocked X)" 형식.
- mcp_breakdown 요약: 동일.
- "saved/savings/절약" 단어 사용 금지.
- 위 7줄 외 한 글자도 추가 금지.
