---
description: 코드브레인 doctor — 실패한 체크만 한 줄씩.
---

`.ai/bin/ai doctor --strict --json` 실행. **표·박스·이모지·헤더 모두 금지.** 코드 블록 금지. 평문만.

`ok=true`면:
```
코드브레인 doctor: {checks.length}개 체크 모두 통과
```
한 줄로 끝.

`ok=false`면:
```
코드브레인 doctor: {pass}/{total} 통과
- {name}: {detail}
- {name}: {detail}
...
```
실패한 체크만 한 줄씩 (통과 항목 나열 금지). detail은 JSON에서 그대로 가져온다 — 요약/해석 금지.

규칙:
- 자동 remediation 금지. 수정 명령 실행 금지.
- 실패 원인 추측·해설 금지.
- 위 형식 외 한 글자도 추가하지 않는다.
