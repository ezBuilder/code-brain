# 코드브레인 검색 효과 검증

사용자 쿼리가 있으면 `.ai/bin/ai context prove "<쿼리>" --json`, 없으면 `.ai/bin/ai context prove --json` 실행한다.

평문으로 다음을 출력한다: 전체 통과 여부, query/selection, effect status, graph/PPR 적용 여부와 ranked nodes, legacy/v2 signature 수와 receipt 수, durability unchanged, legacy/v2 p95, context bytes/max bytes. expected path가 있으면 양쪽 path rank와 span overlap도 출력한다. 실패한 checks를 모두 나열하며 JSON에 없는 값은 추측하지 않는다.

정답 path/span까지 직접 측정하려면 다음 원시 CLI를 안내할 수 있다.

`.ai/bin/ai context prove "<쿼리>" --expected-path <repo-relative-path> --start-line <N> --end-line <N> --repeats 5 --json`
