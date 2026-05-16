---
description: 서버 SSH 접속 방법
---

# 서버 SSH 접속

## AI서버

### 접속 명령어

```bash
// turbo
ssh -o ConnectTimeout=10 -i ~/.ssh/github_deploy_key root@72.62.250.251
```

### 접속 정보

| 항목 | 값 |
|------|-----|
| **호스트** | `72.62.250.251` |
| **호스트명** | `llm.ezbuilder.app` |
| **사용자** | `root` |
| **인증 키** | `~/.ssh/github_deploy_key` |
| **연결 타임아웃** | 10초 |

## 참고사항

- 접속 전 `~/.ssh/github_deploy_key` 파일이 존재하고 권한(600)이 올바른지 확인
- 서버 방화벽에서 SSH(22번 포트) 허용 필요
- 두 서버 모두 동일한 SSH 키 사용 가능