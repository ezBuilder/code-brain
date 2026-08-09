# 2026 에이전틱 코드 검색 딥리서치와 Code Brain 개선 결정

기준일: 2026-08-09
대상: Code Brain과 `/Users/ezbuilder/workspace/modern-transport-tycoon`

## 결론

Code Brain 자체가 항상 이득인 것은 아니다. 최신 에이전트 모델에서도 검색 정밀도가 낮거나 인덱스가 실제 소스를 놓치면, 검색 결과가 탐색을 줄이기는커녕 잘못된 문맥에 모델을 고정하고 비용을 늘린다. 따라서 모든 코드 탐색을 Code Brain으로 강제하는 정책을 폐기하고, 정확 심볼·경로는 live native search, 의미 탐색은 fresh/supported index로 분기한다.

## 실제 기준선

`modern-transport-tycoon`의 설치된 v0.7.1을 측정한 결과:

- MCP 196회 중 `code_query` 48회, `code_read_hashline` 143회, `context_pack` 4회였다.
- 도구 호출은 평균 284ms, p95 1.395s, 누적 약 55.7초였다.
- 훅 주입은 1,229 bytes / 약 2ms로 작아 병목이 아니었다.
- 5개 C# 정확 심볼 검색은 Code Brain 0/5, `rg` 5/5였다.
- 후보 9,024개 중 8,815개가 `.ai/outputs/` 아래 생성 산출물이었고, 실제 인덱스는 172개 파일뿐이었다.
- 한 쿼리에서 후보 상태 스캔 약 1.49초, 해시 판정 약 1.43초가 걸렸다.
- C# 37개 파일은 지원 확장자 누락으로 인덱스와 exact fallback 모두에서 제외됐다.
- 오래된 mtime을 가진 신규 파일은 실제로 누락됐어도 `auto_refresh: current`가 될 수 있었다.

## 최신 1차 연구 근거

| 근거 | 핵심 관찰 | 적용 판단 |
|---|---|---|
| [CodeGrep](https://arxiv.org/abs/2608.05886) | 병렬 grep/glob/read 탐색은 해결된 SWE-bench 작업에서 라운드 15%, 토큰 19%를 줄였다. 낮은 정밀도의 BM25는 오히려 성능을 악화했다. | exact live search 우선, 저정밀 검색 강제 금지 |
| [Agent Retrieval Bench](https://arxiv.org/abs/2607.24882) | 25개 저장소·392K 파일 평가에서 단일 검색 계열이 항상 우세하지 않았고, 실제 에이전트 탐색은 27~35%에서 모든 정답 파일을 놓쳤다. | 쿼리 유형별 라우팅, fallback 필수 |
| [SWE-Explore](https://arxiv.org/abs/2606.07297) | 에이전트형 탐색이 고전 검색보다 강했고 line-level coverage와 효율적 ranking이 중요했다. | live exact line을 indexed 요약보다 우선 |
| [CORE-Bench](https://arxiv.org/abs/2606.11864) | 180K 쿼리에서 전통 코드 검색과 agentic retrieval 사이에 큰 일반화 격차가 있었다. | 고정 BM25-first 규칙 폐기 |
| [ContextBench](https://arxiv.org/abs/2602.05892) | 복잡한 스캐폴딩 이득은 제한적이고, 모델은 precision보다 recall을 과도하게 선호했다. | `context_pack` 자동 연쇄 대신 필요할 때만 사용 |
| [Tree-sitter C#](https://github.com/tree-sitter/tree-sitter-c-sharp) | C# 1~14 문법을 폭넓게 지원한다. | AST 단위 C# chunking의 후속 후보 |
| [GPT-5 for developers](https://openai.com/index/introducing-gpt-5-for-developers/) | 최신 코딩 모델도 도구·문맥 설계의 영향을 받는다. | 모델 향상을 검색 품질의 대체재로 보지 않음 |
| [Claude Opus 4.6](https://www.anthropic.com/news/claude-opus-4-6) | 장기 작업과 에이전트 성능이 개선됐지만 정확한 도구 문맥은 여전히 필요하다. | 작고 검증 가능한 retrieval 유지 |

## 채택

1. `.cs`·`.csx`를 인덱스, live exact fallback, 코드베이스 지도, 자율 하네스에 추가한다.
2. 변경이 누적되는 `.ai/outputs/`를 코드 인덱스와 freshness 후보에서 제외한다.
3. 심볼 쿼리에서는 live exact 결과를 BM25 결과보다 앞에 두고 경로명 일치를 우선한다.
4. mtime gate를 제거하고 후보 메타데이터를 매번 확인해 오래된 timestamp의 신규 파일도 감지한다.
5. 인덱스 계약을 schema v11로 올려 기존 설치가 새 정책으로 결정적으로 재구축되게 한다.
6. 에이전트 규칙을 `exact/path -> targeted native`, `semantic -> fresh Code Brain`, `richer context -> context_pack on demand`로 바꾼다.

## 보류 또는 거절

- C# Tree-sitter AST chunking: 효과 가능성은 높지만 새 parser dependency와 공급망 검증이 필요해 이번 무의존성 패치에서는 보류한다.
- dense embedding 기본 활성화: 현 기준선은 정확 C# 누락과 생성 산출물 오염이 원인이므로 비용·메모리를 늘리는 embedding은 거절한다.
- LLM query rewrite: 지연·비결정성·추가 토큰을 만들며 exact symbol 문제를 해결하지 않아 거절한다.
- 모든 요청에 `context_pack`: 중복 호출과 문맥 팽창을 만들기 때문에 거절한다.

## 출하 게이트

- C# exact symbol source가 문서 언급보다 먼저 반환된다.
- `.ai/outputs/` 변경은 index freshness를 깨뜨리지 않는다.
- DB보다 오래된 mtime의 신규 소스도 자동 감지·증분 갱신된다.
- code retrieval eval에서 C# recall@5=1.0, MRR=1.0을 유지한다.
- 대상 Unity 저장소에서 5개 기준 심볼을 모두 찾고 query p95가 기존 1.395초보다 유의하게 낮아진다.
- lint, 전체 테스트, eval, strict doctor, release gate가 통과해야 릴리스한다.
