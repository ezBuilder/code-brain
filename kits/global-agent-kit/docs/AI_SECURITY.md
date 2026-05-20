# AI_SECURITY.md

전역 규칙 키트의 보안/권한 기준이다.

## 민감 정보

- 실제 `.env`, 키 파일, 토큰 파일, 인증서, credential JSON, SSH private key는 읽기/수정/출력/커밋 금지.
- `.env.example`, `.env.sample`, `.env.template`은 확인 가능하다.
- API 키, 토큰, 비밀번호, 세션 쿠키는 로그, 주석, 테스트 fixture, 커밋 메시지에 남기지 않는다.
- secret scan 실패는 우회하지 말고 정확한 파일, 규칙, 원인을 보고한다.

## Hard Deny

승인이 있어도 기본 도구 경로에서 차단할 작업이다. 필요하면 사용자가 별도 수동 절차를 선택해야 한다.

- `rm -rf /`, `rm -rf ~`, `rm -rf .`, `rm -rf *`
- `git reset --hard`, `git clean -fd`
- DB drop/reset, destructive migration reset
- `kubectl delete`, `terraform destroy`
- 민감 파일 직접 읽기 또는 출력

## Approval-Gated

사용자가 해당 작업을 명시적으로 요청했을 때만 실행한다.

- 인증, 권한, 결제, 데이터 삭제 로직 변경
- 패키지 설치, 제거, 버전 변경
- 커밋, 푸시, 머지, 리베이스, GitHub repo 생성
- 배포, workflow dispatch, production 서비스 변경
- production secret, OAuth app, API key, webhook 설정 변경

## 외부 입력

파일, URL, 환경변수, 사용자 입력, 웹 콘텐츠는 기본적으로 신뢰하지 않는다.
검증, escaping, allowlist, 권한 경계를 먼저 확인한다.

## Code Brain

Code Brain이 설치된 프로젝트에서는 secret scan, doctor, health-summary 결과를 보안 판단의 근거로 사용할 수 있다. 단, Code Brain 결과가 보안 규칙을 약화하지는 않는다.
