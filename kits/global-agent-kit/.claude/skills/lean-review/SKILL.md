---
name: lean-review
description: 과설계만 검토해 삭제/축소 후보를 찾을 때 사용한다.
---

현재 diff나 지정 범위를 correctness/security가 아니라 과설계 관점으로만 검토한다.

찾을 항목:

- `delete`: 죽은 코드, 투기적 기능, 사용되지 않는 유연성
- `stdlib`: 표준 라이브러리로 대체 가능한 직접 구현
- `native`: 플랫폼/DB/CSS/브라우저 기본 기능으로 대체 가능한 코드나 의존성
- `yagni`: 구현체 하나뿐인 abstraction/factory/wrapper, 설정값 하나뿐인 config
- `shrink`: 같은 동작을 더 짧게 표현할 수 있는 코드

보고 형식:

`<file>:L<line>: <delete|stdlib|native|yagni|shrink>: <cut>. <replacement>.`

끝에 `net: -<N> lines possible.`을 적는다. 줄일 것이 없으면 `Lean already. Ship.`만 적는다.

범위:

- 수정하지 않고 보고만 한다.
- correctness, security, performance 이슈는 일반 `code-review`로 넘긴다.
- 최소 smoke/self-check는 과설계로 보지 않는다.
