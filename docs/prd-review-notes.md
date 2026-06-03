# PRD 딥리서치 리뷰 노트 (v1.0 → v1.1)

- **작성**: 2026-06-04
- **대상**: [docs/prd.md](prd.md)
- **방법**: 39-에이전트 멀티에이전트 워크플로우 (자율 진행)
- **원본 결과**: `/private/tmp/claude-501/.../tasks/wdid599lq.output` (휘발성 — 장기 보존하려면 복사할 것)

이 노트는 **리뷰 프로세스·근거·감사 추적**용이다. 보강 *내용*은 PRD 본문 §12에 있다.

---

## 1. 리뷰 구성

| 단계 | 내용 | 수 |
|---|---|---|
| Research | 토픽별 웹 팩트체크 (WebSearch/WebFetch) | 14 |
| Verify | 수치·스펙을 독립 에이전트가 재검색 (적대적) | ≤14 |
| Codebase | PRD 전제 vs 실제 코드 (`code_query`/Read) | 4 |
| Critique | 아키텍처·보안·실행가능성·완전성·단순성 | 5 |
| Synthesize | 정정 목록 + 보강안 종합 | 2 |

39 에이전트 · 223만 토큰 · 645 도구호출 · ~18.6분.

## 2. 본문 직접 정정 (5건 — 적대검증 통과분만)

| § | 정정 | 출처 |
|---|---|---|
| 4.1, 4.4 | Contextual Retrieval "1줄" → **50~100 토큰** | anthropic.com/news/contextual-retrieval |
| 7.1 | 멀티에이전트 "단일 대비 15배" → **chat 대비 15배(단일 대비 ~4배)** | anthropic.com/engineering/multi-agent-research-system |
| 3.3 | nonce 구분자 "인젝션 방어 1차" → **심층방어 한 겹**(단독 약함) | arXiv 2403.14720, 2503.00061 |
| 6.5 | "무인 ingest 전수검토 없이 신뢰" → **adaptive 공격·구조적 격리 단서 추가** | arXiv 2503.00061, 2510.09023 |
| 0, 4.2, 4.3 | 스택 사실 정정 (아래 §3 코드 근거) | 직접 확인 |

## 3. 직접 재확인한 코드 근거 (에이전트 주장 검증 — pass 7 / fail 1)

PRD에 "실제 코드는 X"로 박기 전 핵심 근거를 직접 열람:

| 근거 | 결과 | 확인 |
|---|---|---|
| Python `>=3.11` (PRD: 3.12) | ✅ pass | `.ai/runtime/pyproject.toml:5` |
| 임베딩 = all-MiniLM-L6-v2 384d, opt-in (PRD: Qwen3 1024d) | ✅ pass | `embedding.py:23-24` |
| 리랭커 = ms-marco-MiniLM (PRD: Qwen3-Reranker) | ✅ pass | `reranker.py:19` |
| RRF k = 동적 clamp(30,120) (PRD: 60 고정) | ✅ pass | `search.py:799-831` |
| `remote_llm:false` + doctor가 default-off 강제 | ✅ pass | `config.yaml:9`, `doctor.py:199` |
| `sandbox.execute`가 비격리 subprocess | ✅ pass | `sandbox.py:88-117` |
| 하이브리드·리랭킹·AST청킹 이미 구현 | ✅ pass | `search.py`/`embedding.py`/`reranker.py` |
| `validate.sh:51-52` = "Cadence 언급 금지" | ❌ **fail** | 실제는 `bash -n` 문법체크 — 근거 부정확 |

→ **fail 1건 처리**: "Cadence 흡수/오케스트레이션 부재" 주장의 라인 근거가 틀려, 본문 상단 **"Cadence 흡수" 전제는 사용자 설계 결정으로 유지**하고 §7.1 완화는 '권고(nice)'로만 남겼다. 별도 확인이 필요하면 추가 조사할 것.

## 4. §12 신설 — 보강 (본문엔 사실 오류만, 설계 보강은 §12로 격리)

- **12.1** 코드 정합성 정정표  ·  **12.2** must 12항목  ·  **12.3** should 5/nice 3  ·  **12.4** removals 6  ·  **12.5** 인용 수치 출처표  ·  **12.6** 검증 메타
- 가장 무거운 must: 샌드박스 실격리, Stage 0 LLM 호출 주체(doctor 게이트 충돌), verify 순환 의존, ingest 원자성, 인젝션 구조적 격리(Dual-LLM), SSRF, 락 프로토콜, 비용 계량.

## 5. 정정에서 제외 (검증 결과 정확 — 거짓 정정 금지)

리랭커 MAP+52%/지연 48배(LiveRAG 2025 arXiv 2506.22644로 정확), RouteLLM 26%, contextual 49~67% 범위, Zep 94.8% DMR, 멀티에이전트 90.2%, 청킹 900토큰(방어 가능 범위). → 수치가 옳거나 단지 불완전한 항목은 손대지 않음.

## 6. 상태 / 다음 단계

- **커밋**: 로컬 `develop`. **푸시 안 함** — 문서 정정이라 검토 후 직접 push 결정.
- 검토: `git diff HEAD~1 -- docs/prd.md` 또는 본문 §12.
- 선택지: (a) 그대로 push, (b) §12 must를 본문 각 섹션에 인라인 통합(추가 작업), (c) 일부 보류 항목 재조사(특히 Cadence).
