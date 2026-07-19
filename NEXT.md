# CodeBrain 다음 재개 지점

기록일: 2026-07-20

## 현재 상태

- 이번 작업의 성능·보안·신뢰성 개선은 `develop`에 병합하기 위해 검증된 격리 브랜치에서 정리했다.
- 전체 테스트는 실행하지 않았다. 직접 변경한 모듈과 공유 계약에 영향이 있는 범위만 집중 검증했다.
- 대표 검증 결과: common memory append 60 passed, private memory reader 19 passed, rotation 13 passed, directory context 11 passed, audit/index 9 passed, hook/cache trust 및 관련 회귀 통과.
- `.ai/runtime/tests/test_secret_search_parity.py`는 의도적인 secret-pattern fixture 때문에 로컬 도구의 secret 분류에 걸려 이번 커밋에서 제외됐다. 다음 세션에서 literal token 없이 동적 조합 fixture로 재작성한 뒤 별도 반영한다.

## 바로 이어서 할 작업

1. `.ai/runtime/src/ai_core/hooks.py`에 추가된 `_read_hook_state_text()`를 아래 direct reader에 연결한다.
   - audit cooldown/recommendation/satisfaction 계산
   - autonomous accept cooldown 검사
   - compact meta 계산
   - `.ai/memory/env-versions.json` reader
2. 외부 symlink·hardlink audit/env-version 파일이 hook context 또는 추천 판정에 사용되지 않는 회귀 테스트를 추가한다.
3. secret 분류로 제외된 `test_secret_search_parity.py`를 literal secret 없이 재작성하고 matcher parity 회귀를 복구한다.
4. 변경 범위 테스트를 실행한다. 전체 테스트는 실행하지 않는다.
5. 변경 범위가 안정되면 `make doctor` strict gate와 `git diff --check`를 마지막으로 실행한다.

## 운영 제약

- 기존 `develop` 변경을 초기화하거나 삭제하지 않는다.
- `.chatgpt2codex/` 실행 산출물은 커밋하지 않는다.
- 별도 요청 없이는 push·배포·설치를 하지 않는다.
