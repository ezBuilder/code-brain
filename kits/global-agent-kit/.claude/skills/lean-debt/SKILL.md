---
name: lean-debt
description: `cb-simplify:` 단순화 마커를 읽기 전용으로 수집할 때 사용한다.
---

`cb-simplify:` 마커는 알려진 한계가 있는 단순화를 의도적으로 남길 때만 쓴다.

규칙:

`# cb-simplify: <ceiling>; revisit when <trigger>`

스캔:

- repo에서 `cb-simplify:` comment marker를 찾는다.
- `.git`, `.ai/cache`, build output, dependency directory는 제외한다.
- 읽기 전용으로 보고만 한다.

보고 형식:

`<file>:<line> - <ceiling>. revisit when <trigger>.`

trigger가 없으면 `no-trigger`를 붙인다. 끝에 `<N> markers, <M> no-trigger.`를 적는다. 없으면 `No cb-simplify debt.`만 적는다.
