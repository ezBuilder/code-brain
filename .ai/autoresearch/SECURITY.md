# AutoResearch Security Model (Stage 0)

> 위협 모델·격리 계층 요약. 상세 근거는 `docs/prd.md` §3.4 / §6.4 / §12.2.

## 신뢰 경계

- 모든 `raw/` 원본은 **untrusted**다. `trust_tier`와 무관하게 격리된다(tier는 승격 정책에만 영향).
- 런타임은 **결정론 작업만** 한다: 파일 I/O, FTS 인덱싱, verify-det, 락. LLM 요약/합성/판단은
  **호출 에이전트**(Claude Code / Codex)가 수행하고 결과를 되돌려 기록한다(에이전트-드리븐).
  → `doctor.py` default-off 게이트(`embeddings`/`remote_llm`/`external_notifications` = false) 불변.

## 격리 계층 (defense-in-depth)

| # | 계층 | 모듈 | 역할 |
|---|------|------|------|
| 1 | nonce 경계 | `nonce_verify` | 128-bit CSPRNG로 untrusted 데이터를 감싼다. content가 nonce/구분자를 포함하면 거부(경계 위조 차단). 가드 문구에도 nonce 포함. |
| 2 | 인젝션 휴리스틱 | `injection_scan` | 조작 패턴 감지 시 raw를 `quarantined`로 표시(신호, 보장 아님). ReDoS 가드(50KB 상한). |
| 3 | verify-det 게이트 | `verify_det` | 인용 포맷·근거 substring·sources 실존을 결정론 검사. 실패 시 `status: draft`. LLM judge는 Stage 3. |
| 4 | taint 전파 | `ingest`/`query` | quarantined·미등록 source 파생 page에 `taint: true`(fail-closed). `query`가 taint/draft를 candidates에서 격리. |
| 5 | 서버측 trust_tier | `trust` | 호출자 자기선언 금지. `config.yaml autoresearch.trust_hosts`에서 도메인→tier 도출. 단일레이블 키 거부. |
| 6 | 원자성 | `ingest.commit_pages` | write+FTS 단일 트랜잭션, 실패 시 롤백(FTS·새 파일·덮어쓴 원본). 전역 ingest 락 직렬화. |

## 에이전트 규약

- `commit_pages`를 직접 호출하지 말 것. 항상 `autoresearch_ingest_stage`(nonce-wrap 반환)
  → 에이전트가 요약 → `autoresearch_ingest_commit`.
- `query`의 `quarantined` 후보는 낮은 신뢰로만, 명시적 주의와 함께 인용한다.

## 한계 / 미구현 (정직하게)

- `injection_scan` 휴리스틱은 **신호이지 보장이 아니다**. 구조적 경계(nonce + verify-det + 에이전트 리뷰)가 본체.
- **Dual-LLM 완전 분리**(quarantined LLM의 도구·네트워크·시크릿 차단)는 호출 에이전트 측 책임이며
  런타임이 강제하지 못한다. 에이전트 하니스가 untrusted 원문을 읽는 단계에서 도구·네트워크를 끊어야 한다.
- **SSRF/fetch 방어**는 Stage 3(`deepresearch` url ingest)로 defer. Stage 0는 로컬 content ingest만 다룬다.
- 정적 평가만으로 "전수검토 없이 신뢰"를 선언하지 않는다. adaptive 공격을 포함한 자체 평가가 필요하다.
