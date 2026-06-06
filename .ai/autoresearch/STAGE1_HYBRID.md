# AutoResearch Stage 1 — Hybrid Search (opt-in)

> BM25 + dense + RRF + cross-encoder rerank. 코퍼스 임계점 트리거 전엔 비활성(BM25-only).
> 설계: `docs/prd.md` §4 / §12.2.8(기존 search.py 재사용, 재구현 금지).

## 활성 조건 (모두 충족 시에만 dense 작동)

1. 코퍼스 ≥ `autoresearch.search.corpus_threshold_tokens` (`.ai/config.yaml`, 기본 50K).
2. ONNX dense deps 설치(`pip install -e ".[dense]"`) + 모델 캐시 + `AI_SEARCH_DENSE` 정책.

미충족 → `dense.is_active_for()` = False → **순수 BM25(Stage 0 경로, always-on, 0 의존)**.

## 파이프라인 (dense 활성 시)

1. `fts.search` — BM25 후보 풀(상위 30).
2. `dense.embed_text`(질의) + `embeddings_vec0` 페이지 벡터 → 코사인.
3. `rrf.rrf_fuse` — search.py 동적 k(`_compute_rrf_k`) + `1/(k+rank+1)` 합(BM25/dense 동일 공식).
4. `reranker_ar` — cross-encoder shortlist 재정렬(opt-in `AI_SEARCH_RERANK`, shortlist만).

## 모듈 (모두 no-deps no-op 기본; 기존 search.py/embedding.py/reranker.py 재사용)

| 모듈 | 역할 |
|---|---|
| `dense.py` | embedding.py(ONNX MiniLM 384) 위임, `embeddings_vec0` 저장, 코퍼스 게이트 |
| `rrf.py` | search.py `_compute_rrf_k` + 융합 공식(순수 함수) |
| `reranker_ar.py` | reranker.py(ms-marco) 위임, shortlist만 |
| `hybrid.py` | BM25∥dense → RRF → rerank. dim 불일치/빈 id 방어, 실패 시 BM25 degrade |

## 임베딩 라이프사이클

- `ingest.commit_pages` 후 dense 활성 시 쓴 페이지 임베딩(best-effort, 예외 무시).
- `dense.rebuild_embeddings` 전체 재생성 — DERIVED(git = SSOT for `wiki/`).
- 모델/차원 변경 시 재빌드. 검색 중 dim 불일치는 dense 무시(BM25 degrade).

## 안전 / 환경변수

- 비활성·deps 없음·dim 불일치 → BM25로 안전 degrade(크래시 없음).
- query의 `draft`/`taint` 격리는 hybrid 경유해도 유지(§12.2.6).
- 환경변수: `AI_SEARCH_DENSE`, `AI_SEARCH_RERANK`, `AI_SEARCH_RRF_K`.
