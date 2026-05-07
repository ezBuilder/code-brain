# 코드브레인 doctor

`.ai/bin/ai doctor --strict --json` 실행. 평문만, 표·박스·이모지 금지.

ok=true면 `코드브레인 doctor: N개 체크 모두 통과` 한 줄.
ok=false면 `코드브레인 doctor: {pass}/{total} 통과`에 이어 실패 체크만 `- {name}: {detail}` 줄로. 통과 항목 나열 금지, 자동 remediation 금지, 추측 해설 금지.
