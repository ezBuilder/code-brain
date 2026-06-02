---
name: "source-command-cb-federated"
description: "코드브레인 multi-project 공통 패턴 — 다른 설치 프로젝트와 비교."
---

# source-command-cb-federated

Use this skill when the user asks to run the migrated source command `cb-federated`.

## Command Template

`.ai/bin/ai federated summary --json` 실행.

**표·박스·이모지·헤더 모두 금지.** 코드 블록 금지. 평문만.

`scanned_projects == 0`이면 한 줄로 `federated: 다른 설치 프로젝트 없음.`.

scanned ≥ 1이면:
```
federated: {scanned_projects}개 프로젝트 비교
- common tags ({n}): {tag} ×{projects}, ...
- common todo bigrams ({n}): {bigram} ×{projects}, ...
- common precall kinds ({n}): {kind} ×{projects}, ...
- common skills ({n}): {slug} ×{projects}, ...
```

각 카테고리에 항목이 0이면 그 줄 자체 생략. 위 형식 외 한 글자도 추가 금지.
