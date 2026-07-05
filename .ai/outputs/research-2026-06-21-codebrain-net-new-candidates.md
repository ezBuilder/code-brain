# Code Brain NET-NEW 접목 후보 — 딥리서치 (2026-06-21)

> 질문: 현 시점 Code Brain(v0.4.0)에 새로 접목할 NET-NEW 고가치 기술이 있는가? (이미 보유/평가한 건 전부 제외)
> 통계: 5각도 fan-out · 소스 21 fetch · 주장 99 추출 · 25 적대적 검증(3표 중 2표 반박 시 폐기) → 확정 21 · **합성 후 바 통과 2건**. 에이전트 103, 도구 682.
> 결론: **바를 명확히 넘는 진짜 신규는 1건(cAST 청킹). 1건은 반쪽만 유효(WATCH). 나머지는 CB 기존보유거나 적대검증 기각.**

---

## 통과 1 — cAST: 구조 인지 청킹 (ADOPT-pilot, medium) ★ 유일한 명확한 신규

**무엇**: 재귀적 AST 노드 분할 + 형제(sibling) 병합으로 "구문 경계에 맞고 크기 균일한" 청크를 만드는 청킹 알고리즘. CB의 `search.py`는 현재 **함수 경계 단위**로만 청킹 → cAST가 그 위 단계.
- 구현체 **astchunk(MIT, 순수 Python, tree-sitter)** — CB는 이미 tree-sitter 의존(codegraph)이라 결합도 최적·오프라인·라이선스 호환(3-0).
**접목 대상**: `search.py`의 chunk/index 경계 (인덱싱 파이프라인 교체).
**근거(검증)**: arXiv 2506.15655(EMNLP 2025). GIST Recall@5 **75.0 vs 70.7 = +4.3**(상한), 범위 +1.8~4.3; RepoEval Pass@1 +2.67.
**정직 caveat**: 다운스트림 EM 우위는 **반박됨**(2605.04763: cAST 45.93 vs SlidingWindow 46.23 EM, recall만 2-1) → "리트리벌 recall 개선"으로만 채택하고 **CB 자체 코퍼스에서 재측정** 후 확정. 효과는 retrieval 단계에 한정.
**출처**: arXiv 2506.15655 · github.com/yilinjz/astchunk · arXiv 2605.04763

## 통과 2 — snapcompact: 이미지 비트맵 컨텍스트 아카이빙 (WATCH, 반쪽만 유효)

**무엇**: 버려지는 대화를 **결정론적으로 PNG 프레임(퍼블릭도메인 픽셀폰트)으로 렌더**해 text→image→text로 재구성하는 compaction(oh-my-pi, MIT, 2026-06-21 릴리스). CB엔 이미지-토큰 아카이빙이 없음(3-0 신규).
**왜 WATCH(채택 아님)**: 핵심 주장 2개가 **반박**됨 —
- "완전 오프라인 복구" → **1-2 기각**: 렌더는 오프라인이나, 이미지 블록을 *소비*하려면 비전 모델 필요 → CB 핫패스 오프라인·경량 제약과 충돌.
- "14M 인코더 reward-hack 탐지" → 1-2 기각.
→ 렌더(오프라인)와 소비(비전모델 필요)를 분리하면 CB에 들일 가치는 modest. 부차적으로 CB는 이미 PreCompact/PostCompact + cold 티어 보유 → 델타 작음.
**출처**: github.com/can1357/oh-my-pi compaction.md · arXiv 2606.08893

---

## 기각 (적대검증 폐기 / 이미 보유)

- **grep > vector retrieval (LongMemEval 116문항)** → 1-2. CB의 lexical-first(BM25/FTS5)를 *재확인*할 뿐 신규 기법 아님. (arXiv 2605.15184)
- **code-execution-evidence 가중 투표** → 0-3. CB의 acceptance 게이트(결정론 재실행)+verified-completion과 동일 개념, 신규 아님. (arXiv 2603.02203)
- snapcompact "완전 오프라인 복구"(1-2), "14M reward-hack 인코더"(1-2) — 위 참조.
- 그 외 9건 budget-drop, 다수 벤치 과장/재현실패 각도는 신규 산출 없음.

## 결론 / 권고

- **실질 추가거리는 cAST 청킹 하나.** 오프라인·MIT·tree-sitter·`search.py` 정확히 맞고, recall +1.8~4.3 근거 有(단 EM은 무승부라 retrieval-recall 파일럿으로, 자체 재측정 필수).
- snapcompact는 비전모델 의존 때문에 CB 에토스와 어긋나 보류.
- 나머지는 "이미 CB가 함" 또는 적대검증 기각. **"없으면 말고" 기준으로는 cAST 1건만 검토 가치.**

*1차 출처: arXiv 2506.15655 / 2605.04763 / 2605.15184 / 2603.02203 / 2606.08893 · github yilinjz/astchunk · can1357/oh-my-pi*
