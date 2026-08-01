# 에이전트 메모리 5종(Mem0 · Letta · Graphiti/Zep · Cognee) Code Brain 접목성 딥리서치 (2026-08-01)

> 조사 기간: 2026-07-31 ~ 2026-08-01. **1차 출처 접근일 = 2026-08-01** (이 문서의 모든 외부 URL 관찰 시점). 신선도 재확인 검색도 같은 날 수행.
> 방식: 병렬 read-only 레인 6종(레포 truth · Mem0 · Letta · Graphiti/Zep · Cognee · 독립 벤치/보안/이슈) → 인용 경화(citation hardening) 레인 → 운영 이슈 증거 레인 → 경쟁 후보 반증 레인 → **구현으로 후보 반증/확인** → 이 보고서.
> 결론 한 줄: **Build core / Borrow concepts / Buy none now.** 그리고 구현이 이 결론을 강화했다 — 이번 라운드 최고가치 작업은 다섯 제품에서 빌려온 기능이 아니라 **Code Brain 자체 읽기 경로의 정확성 결함**이었다.
> 원칙: 벤더 벤치 수치는 채택 근거에서 제외. 접근 불가 커뮤니티(Reddit HTTP 403, HN HTTP 429)는 사실로 인용하지 않는다. 로컬 주장은 `file:line`으로만 말한다.
> **`file:line` 기준**: 현재 작업트리(HEAD `e36e3a0` + 검증 완료된 dirty wave `002`/`003`/`007`/`009`/`010`). 초기 리서치 receipt의 일부 번호는 변경 전 기준이므로, 아래 번호는 이 보고서를 쓰며 직접 재확인한 값이다.

---

## 1. Executive summary

1. **다섯 제품 중 Code Brain에 런타임/서비스로 도입할 가치가 있는 것은 없다.** 개념은 넷에서 빌릴 만하고(범위 승인 쓰기, 고정/탐색 투영, valid/transaction time, 타입드 출처), 코드·의존성·DB·이그레스는 하나도 들일 필요가 없다.
2. **리서치가 제안한 4개 P0 중 2개는 코드 앞에서 무너졌다.** Candidate 1(시간성/출처 봉투)은 `decided_at`이 이미 `recorded_at`이고 `expires_at`이 이미 `valid_to`로 **집행까지** 되고 있어 장식이었다. Candidate 4(정규 export/import)는 단일 사용자 레포-로컬 도구에 두 번째 직렬화를 추가하는 과설계였다.
3. **살아남은 것은 Candidate 2(삭제)와 Candidate 3(메모리 검색 평가축)**, 그리고 재구성된 Candidate 5(네트워크 기본값)다. Candidate 2는 압박 테스트에서 **초안 설계가 살아있는 메모리를 조용히 파괴한다**는 사실이 드러나 재설계됐다.
4. **실제로 출하되고 독립 검증된 것은 정확성 결함 수정이었다.** BUG-1/2/3 + `-009`/`-010`. 최종 스위트 `1934 passed, 5 skipped`. 다섯 제품 중 어느 것에서도 빌려오지 않은 작업이다.
5. **새로 발견된 최고 심각도 항목은 MCP 와이어 계약 위반**이다. `code_query`는 `openWorldHint: False`로 광고되면서 다중 MB 다운로드에 도달할 수 있다(§4.3). 리서치가 "놀라운 기본값"으로 틀리게 프레이밍했던 것이 실제로는 프로토콜 수준 위반이었다.
6. **권고**: 로컬 정확성·측정 가능성을 먼저 닫고(`-011`, `-004`, `-005`, `-006`), 그 다음에야 P1 실험(고정/탐색 투영, PPR 융합)을 평가축 위에서 판정한다.

---

## 2. 증거 등급과 취급 규칙

| 등급 | 정의 | 이 문서에서의 취급 |
| --- | --- | --- |
| **A — 1차(primary)** | 공식 문서·레포·릴리스·라이선스 파일, 그리고 **이 레포의 코드 자체** | 사실로 인용. 로컬은 `file:line` 필수 |
| **B — 독립(independent)** | 벤더가 저자가 아닌 논문·학회 발표·NVD/GHSA 레코드 | 사실로 인용하되 **측정 대상·설정·버전**을 함께 명시 |
| **C — 벤더 자기보고** | 제품 블로그·README 벤치·마케팅 비교표 | **채택 근거로 쓰지 않음.** 존재만 기록 |
| **D — 일화(anecdotal)** | GitHub 이슈 작성자 보고 | "이슈 작성자가 보고함"으로만 인용. 상태는 2026-08-01 관찰값 |
| **E — 접근 불가** | Reddit(HTTP 403), Hacker News(HTTP 429), 포럼 | **인용 금지.** 검증된 사실로 승격하지 않음 |

적용 규칙 네 가지를 이 문서 전체에 강제했다.

- **관리형(managed) / OSS 경계를 제품마다 분리한다.** Mem0의 벤치 수치는 OSS SDK에 없는 독점 최적화를 포함한다고 Mem0 자신이 배포 페이지에 밝히고 있다([mem0ai PyPI](https://pypi.org/project/mem0ai/), 접근 2026-08-01).
- **Letta는 현행 Letta Code**(`letta-ai/letta-code`)로 비교한다. 레거시 MemGPT/Letta V1의 CVE를 현행 릴리스로 일반화하지 않는다.
- **현행 Zep을 Apache 자체호스팅 OSS로 서술하지 않는다.** Zep Community Edition은 유지·릴리스가 중단됐고(레포는 Apache-2.0으로 남지만 업데이트·지원 없음, [Zep 블로그](https://blog.getzep.com/announcing-a-new-direction-for-zeps-open-source-strategy/) 및 [Zep FAQ](https://help.getzep.com/faq)), 현행 Zep은 호스티드/엔터프라이즈 BYOC이고 컨텍스트 그래프 엔진은 독점이다. Apache-2.0 자체호스팅 조각은 **Graphiti**라는 별개 컴포넌트다([zep-vs-graphiti](https://help.getzep.com/zep-vs-graphiti)).
- **CVE는 영향 컴포넌트·버전으로 스코프한다.** 패치 버전이 없는 미심사 GHSA를 "고쳐졌다"고 쓰지 않는다.

> 외부 웹 출처의 내용은 라이선스 준수를 위해 재서술(rephrase)했다.

---

## 3. 제품별 비교

### 3.1 Mem0

| 항목 | 관찰 (2026-08-01) |
| --- | --- |
| 아키텍처 | 벡터 스토어 + 선택적 지식 그래프. `add`/`search` 중심의 얇은 메모리 레이어 |
| 라이선스 | 레포 루트 Apache-2.0 ([mem0ai/mem0](https://github.com/mem0ai/mem0)) |
| 배포/데이터 모델 | OSS SDK 자체호스팅 가능. 별도 **관리형 Platform** 존재. 벡터 백엔드 다수 지원 |
| 프라이버시 | OSS는 백엔드 선택에 종속. 관리형은 데이터 위탁 |
| 보안 이력 | `CVE-2026-59705`(GHSA-xgj7-grxr-prrp), `CVE-2026-59706`(GHSA-225m-p565-9h68), `CVE-2026-7597`(GHSA-xqxw-r767-67m7), `CVE-2026-31241`(GHSA-gq6f-qwv9-rf4j), `CVE-2026-49948`(GHSA-hp66-92p5-jh23) |
| 커뮤니티 | 이 분야 최대 규모(등급 C 비교글 다수, 근거로 사용 안 함) |
| 통합 표면 | Python/TS SDK, 다수 프레임워크 어댑터 |

**보안 스코프(중요).** 59705·59706·49948은 **미심사 GHSA이며 패키지 수준 최초 수정 릴리스가 없다**. 31241도 패치 버전 표기가 없다. 7597은 설명과 패키지 범위가 불일치한다. 따라서 "Mem0 CVE 5건"을 현재 악용 가능성의 증거로 읽으면 안 되고, **도입 위험 이력**으로만 읽어야 한다.

**신선도 확인에서 새로 발견(등급 C, 2026-08-01 접근).** Mem0가 OSS v2→v3 메모리 알고리즘 이행 문서를 공개했고 LoCoMo·LongMemEval 대폭 개선을 자기보고한다([oss-v2-to-v3](https://docs.mem0.ai/migration/oss-v2-to-v3)). OSS/관리형 격차가 좁아졌을 가능성을 시사하지만 **벤더 자기보고이며 독립 재현이 없다.** 채택 근거로 쓰지 않는다.

**운영 이슈(등급 D, "이슈 작성자 보고", 상태 2026-08-01 관찰).** ADD-only 경로에서 최신 사실이 충돌한 상태로 남는다는 보고([#5867](https://github.com/mem0ai/mem0/issues/5867), open), 동시 해시 중복제거의 TOCTOU 중복([#6515](https://github.com/mem0ai/mem0/issues/6515), open), 규모에서의 중복/지연([#5850](https://github.com/mem0ai/mem0/issues/5850), open), 임베디드 Qdrant 동시 쓰기 HNSW 손상 보고([#4892](https://github.com/mem0ai/mem0/issues/4892), open/P1-high), 통합별 컬렉션 설정 경쟁([#4056](https://github.com/mem0ai/mem0/issues/4056), closed). **안전한 적용**: 승계(supersession), 멱등성 키, 중복 예산, 백엔드 동시성 테스트. Qdrant 일반론이나 보편적 지연 주장은 지지되지 않는다.

**Code Brain에 빌릴 것**: 범위 지정 제안→승인 쓰기, 하이브리드 검색, 시간적 진실, **삭제 영수증**.

### 3.2 Letta / Letta Code

| 항목 | 관찰 (2026-08-01) |
| --- | --- |
| 아키텍처 | 장수(long-lived) 에이전트 + 컨텍스트 계층. **memory blocks**(컨텍스트에 고정) vs **archival memory**(도구로 온디맨드 질의) |
| 현행 표면 | `letta-ai/letta-code` (활성). `letta-ai/letta`는 레거시/MemGPT 계보 |
| 배포 | 로컬 모드 문서 존재. 클라우드 별도 |
| 보안 이력 | 레거시 `CVE-2024-39025`(GHSA-7p2g-2vxc-5g55, **패치 버전 표기 없음**), AgentFile 경로 탐색 수정 PR [letta-code#2777](https://github.com/letta-ai/letta-code/pull/2777) |
| 통합 표면 | CLI 하네스, skills/subagents |

**핵심 개념 인용(등급 A).** Letta 문서는 archival memory 조각이 **컨텍스트 창에 고정될 수 없고 도구로 질의되어야 한다**고 명시한다([archival-memory-overview](https://docs.letta.com/guides/agents/archival-memory-overview)). 이것이 P1 후보 "pinned vs discoverable 투영"의 1차 근거다. Code Brain의 HOT/WARM/COLD와 같은 축이지만, Letta는 *에이전트가 스스로* 블록을 다시 쓴다.

**신선도 확인에서 새로 발견(등급 A/C 혼합, 2026-08-01 접근).** Letta가 **Context Repositories** — 프로그래매틱 컨텍스트 관리와 **git 기반 버저닝**으로 Letta Code 메모리를 재구축했다고 발표했다([context-repositories](https://www.letta.com/blog/context-repositories/)). 이는 Code Brain이 이미 하고 있는 것과 **수렴한다**: `.ai/memory`는 git 동기 대상이고(`.ai/.gitattributes:1` `*.jsonl merge=union`), 그래서 이번 라운드의 삭제 설계는 "union 머지로 복원 가능"을 정직하게 보고해야 한다(§7). 독립 검증된 우위가 아니라 **설계 방향의 외부 확증**으로만 취급한다.

**운영 이슈(등급 D, 상태 2026-08-01 관찰).** 영속 설정과 라이브 소스 바인딩 불일치([#3140](https://github.com/letta-ai/letta-code/issues/3140)), 동일 Bash 호출 93회 반복에 진전 없음([#3505](https://github.com/letta-ai/letta-code/issues/3505)), 무상태 서브에이전트 스텁 누적([#3523](https://github.com/letta-ai/letta-code/issues/3523)), Agent 디스패치가 PreToolUse 모델/비용 게이트를 우회([#3150](https://github.com/letta-ai/letta-code/issues/3150)), BYOK 도구 호출이 저장되지만 디스패치되지 않음([#2973](https://github.com/letta-ai/letta-code/issues/2973)). **안전한 적용**: 상태 재조정, 행동/결과 서킷 브레이커, 워커 TTL, 디스패치 정책 불변식. (레거시 레포의 스팸-종료된 ADE UI 이슈는 상태/소스 증거가 아니므로 **제외**했다.)

**Code Brain에 빌릴 것**: 고정/탐색 투영, 콘텐츠 해시 출처, sleep-time 제안(승인 경유).

### 3.3 Graphiti (OSS) vs Zep (호스티드) — **반드시 분리**

| 항목 | Graphiti | Zep (현행) |
| --- | --- | --- |
| 라이선스/배포 | Apache-2.0, 자체호스팅 ([getzep/graphiti](https://github.com/getzep/graphiti)) | 호스티드 / 엔터프라이즈 BYOC |
| 필수 인프라 | 그래프 DB 직접 운영(Neo4j / FalkorDB 등) | 관리형 |
| 엔진 | 시간적 지식 그래프 프레임워크 | 컨텍스트 그래프 엔진 **독점** |
| Community Edition | 해당 없음 | **유지·릴리스 중단**, 지원 없음 |
| 보안 이력 | `GHSA-gg5m-55jj-8m5g` / `CVE-2026-32247` — **`v0.28.2`에서 수정(명확)** | 별도 |

Graphiti의 가치는 **bitemporal 모델**(valid time / transaction time), 에피소드 출처, 무효화(invalidation), 멱등성이다. 이것이 Candidate 1의 영감이었고, 그래서 §4.1의 반증이 중요하다.

**운영 이슈(등급 D, 상태 2026-08-01 관찰).** LLM 호출 다중성과 레이트리밋/인제스션 트레이드오프([#1516](https://github.com/getzep/graphiti/issues/1516), [#1262](https://github.com/getzep/graphiti/issues/1262)), 공유 드라이버의 `group_id` 교차 FalkorDB 라우팅 경쟁([#1676](https://github.com/getzep/graphiti/issues/1676)), 큐잉/성공 응답 뒤 관측 불가한 종단 추출 손실([#1707](https://github.com/getzep/graphiti/issues/1707)), 삭제 라우팅에 `group_id` 부재([#1705](https://github.com/getzep/graphiti/issues/1705), closed/not planned). **안전한 적용**: 호출 예산/백프레셔, 요청 스코프 테넌트 핸들, 내구성 있는 잡 상태/DLQ, 그룹 인식 삭제. (중복 에피소드·고아 정리·마이그레이션 실패 주장은 정본 이슈 지지가 약해 **제외**했다.)

**Zep 판정: 현재 `NO-GO`.** 관리형 결합, CE 지원 경계, 삭제 잔여물 caveat 때문이다.

### 3.4 Cognee

| 항목 | 관찰 (2026-08-01) |
| --- | --- |
| 아키텍처 | 비정형 문서에서 엔티티/관계/개념을 추출해 지식 그래프 구성 |
| 라이선스 | 코어 Apache-2.0 ([topoteretes/cognee](https://github.com/topoteretes/cognee)). Cloud 및 일부 프로덕션 백엔드는 별개 |
| 기본 스택 | 파일 기반 임베디드: 그래프 Kuzu, 벡터 LanceDB, 관계형 SQLite. 프로덕션에서 Neo4j/FalkorDB, Qdrant/pgvector 등으로 외부화 ([deployment-options](https://cognee.mintlify.app/how-to-guides/cognee-sdk/deployment/deployment-options)) |
| **프라이버시(결정적)** | **LLM과 임베딩 provider를 반드시 함께 로컬로 설정해야 한다. 한쪽만 설정하면 다른 쪽이 OpenAI로 폴백한다** ([local-setup](https://docs.cognee.ai/guides/local-setup), [llm-providers](https://docs.cognee.ai/setup-configuration/llm-providers)) |
| 보안 이력 | `CVE-2026-58473` — **`<1.2.0` 영향, `1.2.0`에서 수정(명확)**. `CVE-2026-31231` — 구조화된 수정 버전 증거 없음 |

프라이버시 항목이 Code Brain의 하드 게이트 "기본 네트워크/텔레메트리 0"과 **직접 충돌**한다. 설정 실수 하나가 조용한 이그레스가 되는 기본값은, 레포-로컬 오프라인 우선 도구가 채택할 수 없는 실패 모드다.

**운영 이슈(등급 D, 상태 2026-08-01 관찰).** 대형 COGX 기본-Kuzu 마이그레이션 단정([#3945](https://github.com/topoteretes/cognee/issues/3945), closed), 비멱등 재시도가 중복 세션 행과 반복 503을 만든다는 보고([#4226](https://github.com/topoteretes/cognee/issues/4226), open), 처리 중 삭제가 미분화된 500을 반환([#3766](https://github.com/topoteretes/cognee/issues/3766), open), 로컬 구조화 출력 능력과 고정 용량 백엔드 견고성([#3647](https://github.com/topoteretes/cognee/issues/3647), [#3870](https://github.com/topoteretes/cognee/issues/3870), open). **안전한 적용**: 재개 가능/차분 마이그레이션, 멱등성 키/업서트, 타입드 재시도 상태, 시작 시 능력 프로브와 승인 제어. 정확한 락 고갈 메커니즘과 "로컬 모델 보편적 실패"는 지지되지 않는다.

**Code Brain에 빌릴 것**: 타입드 엔티티/온톨로지/출처 아이디어(개념만).

### 3.5 통합 비교

| | Mem0 | Letta Code | Graphiti | Zep | Cognee |
| --- | --- | --- | --- | --- | --- |
| 라이선스(핵심) | Apache-2.0 | 레포 공개 | Apache-2.0 | 호스티드/독점 엔진 | Apache-2.0 코어 |
| 완전 오프라인 실현성 | 백엔드 종속 | 로컬 모드 문서 존재 | 그래프 DB 운영 필요 | **불가** | **양쪽 provider 설정 필수, 미설정 시 폴백** |
| 추가 의존성 | 벡터 스토어 | 런타임/API | 그래프 DB | — | Kuzu+LanceDB+SQLite(최소) |
| 시간성 모델 | 부분 | 컨텍스트 계층 | **bitemporal(최강)** | Graphiti 경유 | 부분 |
| 삭제/정정 증명 | 이슈 다수 | — | 그룹 삭제 미완 이슈 | 잔여물 caveat | 처리 중 삭제 500 보고 |
| 개찬 방지 감사 | 없음 | 없음 | 없음 | — | 없음 |
| 명확한 CVE 수정 | **0건** | 0건 | **1건(`0.28.2`)** | — | **1건(`1.2.0`)** |

---

## 4. Code Brain 역량/격차 매핑 (`file:line` — 직접 재확인)

### 4.1 Candidate 1 — 시간성/출처 봉투: **명세대로는 기각**

- `decided_at`은 append 시점 `now_iso()`다 → **이미 `recorded_at`이다**(`memory.py:411`). 개명은 아무것도 추가하지 않는다. 참고로 `recorded_at`이라는 이름 자체는 이 레포의 다른 저장소들이 이미 쓴다(`evidence.py:189`, `security_findings.py:64`, `loop_engineering.py:250`).
- `expires_at`은 **이미 `valid_to`이고 집행된다**: 검증(`memory.py:298-330`), 판정(`memory.py:333-343`), 공유 필터(`memory.py:346-382`), 두 공개 리더의 `include_expired` 탈출구(`memory.py:465`, `memory.py:498`).
- `valid_from`은 **런타임 소스 전체에 0회 등장한다**(전수 grep). 이 코드베이스가 실제로 분기하는 유효성 차원은 벽시계가 아니라 **버전 스코프**다: `_failure_retest_flag`가 레코드의 `observed_versions`를 `.ai/memory/env-versions.json` 스냅샷과 diff한다(`hooks.py:2119-2132`, 스냅샷 로더 `hooks.py:2100-2116`).
- **결론**: Graphiti의 bitemporal을 통째로 빌리는 것은 장식이었다. 진짜 격차는 새 필드가 아니라 **도달성**이었고, 그것이 `-007`(MCP 패리티)로 출하됐다.

### 4.2 Candidate 2 — 승계 + tombstone + 삭제 영수증: **확인, 설계 완료, 미구현**

- `tombstone`/`forgotten_id`는 런타임 소스에 **0회** 등장한다. 삭제 경로가 없다.
- 감사 페이로드가 **id만** 담는다: `payload={"id", "kind"}` (`memory.py:445-446`). 이것이 결정적이다 — 해시 체인을 건드리지 않고 정본 저장소의 본문을 정직하게 제거할 수 있다는 뜻이다.
- **압박 테스트가 초안 설계를 무너뜨렸다.** tombstone을 `{"id": target, "kind": "tombstone"}`로 쓰고 리더에서 억제하면, `read_decisions_for_surface`의 `plain` 파티션(`memory.py:466`)에 들어가 tail 창을 소비한다. `DECISIONS_TAIL = 3`(`hooks.py:60`)이므로 **forget 3회로 SessionStart `decisions:` 블록의 실제 결정 전부가 축출된다.** 억제는 반드시 `live_decision_records` **내부**에서 일어나야 한다.
- **대상 id 재사용 금지**: 공유 필터는 `kind == "failure"`만 id로 fold하고(`memory.py:370-373`), `_short_id`는 32비트만 발행한다(`memory.py:240-242`, `secrets.token_hex(4)`) → 미래의 무관한 레코드를 억제할 수 있다.
- **압축은 무조건이어야 한다**: 공유 헬퍼를 우회하는 리더가 아직 둘 남아 있다 — `hooks.py:2333-2334` 예외 폴백과 `loop_engineering._conflicting_decisions`(`loop_engineering.py:745-760`, `decisions_path`를 raw로 읽는다).
- **영구성 주장 금지**: `.ai/.gitattributes:1`이 `*.jsonl merge=union`이므로, 구 파일을 가진 피어의 머지가 압축된 줄을 **복원**한다. 영수증은 `union_merge_restorable: true`를 보고해야 한다.

### 4.3 Candidate 5 — `AUTO_INSTALL` 네트워크 기본값: **두 번 재구성됨**

**리랭커 절반 — 반박(REFUTED).** `reranker.py:113`이 `ai reranker install --json`을 spawn하는데, **그 명령은 존재하지 않는다.** 전수 검증: 문자열 `"reranker"`는 `ai_core/*.py` 전체에서 `reranker.py:113` 단 한 곳에만 나타나고, `.ai/bin/*`에도 `reranker` 디스패치가 없다. 대조적으로 `embedding`은 `cli.py:633`에 정식 등록돼 있다. 따라서 detached 자식은 argparse에서 죽고, 출력은 devnull로 간다. **이그레스가 아니라 broken-spawn 결함**이며 TTL 3600초(`reranker.py:99`)로 매시간 재발화한다. 온디스크 상태가 이를 확증한다: `.ai/cache/reranker-model/`에는 7바이트 `.install-lock`만 있고 아티팩트가 없다.

**임베딩 절반 — 확인, 그리고 프레이밍보다 나쁨: MCP 와이어 계약 위반.** 체인을 전 구간 직접 확인했다.

```
mcp_server.py:862   code_query ∈ _READ_ONLY_TOOLS
mcp_server.py:889   → annotations = {"readOnlyHint": True, "openWorldHint": False}
mcp_server.py:882   _OPEN_WORLD_TOOLS = {"sandbox_execute", "autoresearch_ingest_stage"}  ← code_query 없음
mcp_server.py:1207  code_query → query(...)
search.py:1681-1682 → embedding.is_active_for(root)
embedding.py:86-92  → AI_SEARCH_DENSE_AUTO_INSTALL 기본 "1" → _maybe_spawn_background_install
embedding.py:128    → Popen([ai, "embedding", "install", "--json"])
cli.py:1637         → embedding 명령 디스패치
model_artifacts.py:154 → urllib.request.urlopen(...)
```

`openWorldHint: False`를 광고하는 도구가 다중 MB 다운로드를 촉발할 수 있다. 스톡 설치에서는 `_deps_present()`(`embedding.py:86`)가 옵셔널 `[dense]` extras를 요구하므로 도달 불가지만, **문서화된 extra를 설치한 사용자에게는 read-only `code_query` 한 번이 다운로드가 된다.**

**`features.embeddings`는 장식이다 — 전수 확인.** 런타임 소스에서 이 플래그를 읽는 곳은 `doctor.py:275` 단 하나이고, 거기서는 `false`가 아니면 하드 실패시킨다(`doctor.py:274-277`). `embedding.is_active_for`(`embedding.py:63-93`)는 `load_config`를 아예 호출하지 않고 환경변수만 읽는다. `.ai/config.yaml:8`은 `embeddings: false`다. 즉 **doctor가 집행하는 플래그와 실제 동작이 분리돼 있다.** 체인 어디에도 오프라인/에어갭 가드가 없다: `policy.WRITE_COMMANDS`(`policy.py:17-40`)는 `embedding`/`reranker`를 포함하지 않고, `model_artifacts.py`에는 환경·정책·trust 검사가 없다.

**`hooks.py`는 깨끗하다.** `search`/`embedding`/`reranker` import가 0건이므로 AGENTS.md의 "훅 핫패스는 네트워크를 호출하지 않는다" 조항은 유지된다. 위반은 MCP 조항 쪽이다.

### 4.4 Candidate 3 — 메모리 검색 평가축: **확인**

- `make eval`은 5축을 명시적으로 나열한다: `Makefile:70` (`precall_routing`, `context_budget`, `tool_discovery`, `autoresearch_retrieval`, `code_retrieval`). **메모리 축은 없다.**
- 결정성 함정 하나가 기록돼 있다: `memory_recall.py:99`가 `read_decisions_filtered(root, limit=10_000)`을 `now=` 없이 호출하고, `read_decisions_filtered`(`memory.py:472-483`)에는 `now` 파라미터가 아예 없다. 그래서 `live_decision_records`는 벽시계로 폴백한다(`memory.py:343`). `now=`가 만료를 통제한다고 가정하면 **불안정한(flaky) 축을 출하한다** — 픽스처는 먼 과거/먼 미래 경계를 써야 한다.

### 4.5 기존 강점(빌려올 필요 없음)

로컬 HOT/WARM/COLD 계층과 결정적 페이징, append-only decisions/todos/sessions, 해시 체인 감사, 모순 메타데이터, BM25/FTS5 + 옵션 dense/RRF/rerank, 출처/증거, Python AST 그래프, 레포-로컬 MCP, 원격 기능 기본 off.

---

## 5. 독립 벤치마크 — **교차 비교 불가 주의를 인라인으로**

| 출처 | 무엇을 측정하는가 | 관찰된 수치 | 인라인 caveat |
| --- | --- | --- | --- |
| [LongMemEval](https://arxiv.org/abs/2410.10813) | 장기 기억 QA | — | 대화형 QA. 코드 레포 메모리와 과제가 다름 |
| [LoCoMo](https://aclanthology.org/2024.acl-long.747/) | 장기 대화 | — | 대화 도메인 전용 |
| [MemoryAgentBench](https://arxiv.org/abs/2507.05257) | 에이전트 메모리 종합 | Mem0 `21.1` · Cognee `20.6` · Zep `24.0` · 레거시 MemGPT `28.3` · HippoRAG-v2 `41.6` | **버전·청킹·모델·판정자가 다르다.** 제품 간 추론을 실질적으로 제한한다 |
| [MemBench](https://aclanthology.org/2025.findings-acl.989/) | 메모리 벤치 | — | 또 다른 과제 정의 |
| [LongMemEval-V2](https://arxiv.org/abs/2605.12493) | 웹 에이전트 경험 | — | **WIP** |
| [LTM 비용/정확도 비교](https://arxiv.org/abs/2601.07978) | LoCoMo 정확도 + TCO | Mem0/simple RAG/full-context `77–81%` vs Graphiti/Cognee `55–56%`. simple RAG가 Mem0보다 TCO `8.4x` 낮음 | **COMPSAC 2026 게재 원고, 설정 특이적.** 보편적 제품 순위가 아니다 |

**이 표에서 도출 가능한 유일한 결론**: 어떤 제품도 Code Brain의 과제(레포-로컬 코드 검색 + 결정 출처 + 시간적 정정)에서 우월함을 입증하지 못했고, 위 수치 중 어느 것도 로컬 재현 없이는 채택 근거가 되지 않는다. 흥미로운 방향성 신호 하나는 기록해 둔다 — 그래프 지향 시스템(Graphiti/Cognee)이 정확도에서 뒤지고 단순 RAG가 TCO에서 압도한다는 관찰은, Code Brain의 기존 **BM25 lexical-first** 결론과 같은 방향이다. 단일 설정 결과이므로 확증이 아니라 정합성 확인이다.

---

## 6. 가중 결정 매트릭스 → 채택 / 보류 / 기각

가중치는 이 레포가 명시한 하드 게이트에서 유도했다(§8). **이 점수는 저자 판단(labelled inference)이며 측정값이 아니다.**

| 기준 | 가중 | Mem0 | Letta Code | Graphiti | Zep | Cognee |
| --- | --- | --- | --- | --- | --- | --- |
| 로컬 우선 / 기본 네트워크 0 | 0.25 | 2 | 2 | 2 | 0 | **1** |
| 의존성 0 / stdlib 유지 | 0.20 | 1 | 1 | 0 | 0 | 0 |
| 삭제·정정 증명 가능성 | 0.15 | 1 | 1 | 2 | 1 | 1 |
| 시간성/출처 모델 품질 | 0.15 | 2 | 3 | **5** | 4 | 3 |
| 검색 품질 로컬 실측 가능성 | 0.10 | 2 | 2 | 2 | 1 | 2 |
| 운영 부담 / 보안 이력 | 0.10 | 1 | 2 | 3 | 2 | 2 |
| 커뮤니티 / 유지보수 | 0.05 | 5 | 4 | 4 | 4 | 3 |
| **가중 합 (0–5)** | | **1.70** | **1.90** | **2.20** | **1.15** | **1.40** |

전 제품이 중간값 미달이다. 최고점 Graphiti조차 그래프 DB 운영을 요구해 `의존성 0` 축에서 0점이다.

**판정**

- **채택(개념만, 코드 0줄)**: Mem0의 삭제 영수증·멱등성, Letta의 고정/탐색 투영·콘텐츠 해시 출처, Graphiti의 valid/transaction time·에피소드 출처·무효화, Cognee의 타입드 출처 아이디어.
- **보류(P0 이후 측정 후 판정)**: 관계/PPR 융합, 레코드 수준 소스 신선도, 고정 vs 탐색 투영.
- **기각**: 다섯 제품의 런타임/서비스 도입, 즉시 Neo4j/FalkorDB/Kuzu 도입, 벤더 벤치 기반 선택, 의존성 설치, 호스티드 호출 또는 레포 데이터 이그레스. **Zep은 현재 `NO-GO`.**
- **쓰기 계약(불변)**: 자동 메모리 변경은 `제안 → 승인 → append 이벤트`. 무제한 자율 재작성과 그래프 전용 검색은 기각.

---

## 7. 이번 라운드에 실제 출하·검증된 것 vs 큐에 남은 것

### 출하 + 독립 검증 완료 (`VERIFIED_PASS`, 다른 read-only 에이전트)

| 항목 | 내용 |
| --- | --- |
| BUG-1 | id 없는 레거시 todo에서 `close_todo`가 `KeyError: 'id'`를 던지던 문제. 리더와 문자 단위로 동일한 파생 키로 닫는다 |
| BUG-2 | `expires_at`/retired 집행이 9개 결정 읽기 경로 중 2개에만 있던 문제. **단일 공유 술어** `live_decision_records`(`memory.py:346`)를 도입해 `memory.py:465`, `memory.py:498`, `memory_tier.py:480`, `recommend.py:540`, `agent_recommend.py:168`, `memory_conflicts.py:77`, `session_resume.py:109`에 배선 |
| BUG-3 | `expires_at`이 임의 문자열을 받아 도착 즉시 만료되던 문제. `_valid_expires_at`(`memory.py:298`)이 shape-gate 후 UTC 정규화, 불량은 fail-soft로 드롭 |
| `-007` | MCP 시간성 패리티. `record_decision`에 `contradicts`/`derives_from`/`expires_at`, `list_decisions`에 `include_expired` |
| `-009` | `test_cli.py` 레거시 스키마 픽스처의 mtime 민감성. `AI_SEARCH_AUTO_REFRESH=0` 결정화 |
| `-010` | `_valid_expires_at`의 "never raises" 계약 위반. 가드를 `except (ValueError, OverflowError)`로 확대(`memory.py:329`) |

최종 스위트 **`1934 passed, 5 skipped`**. 산술이 정확히 맞는다: 기준선 `1 failed, 1931 passed, 5 skipped` → `1931 + 1`(고쳐진 실패) `+ 2`(신규 경계 케이스) `= 1934`, skip은 `5`로 불변. 초록으로 만들기 위해 삭제·스킵된 테스트는 없다. 보호 대상 kit 기준선은 전 wave 내내 정확히 `14 files changed, 126 insertions(+), 227 deletions(-)`로 유지됐다.

**정직한 관찰**: 이 여섯 항목 중 **어느 것도 다섯 제품에서 빌려온 기능이 아니다.** 전부 Code Brain 자체 읽기 경로의 정확성 결함이었다. 이것이 "Build core" 논지를 리서치 단계보다 강하게 만든다.

### 큐에 남은 것 (미디스패치)

| ID | 내용 | 상태 |
| --- | --- | --- |
| `-011` | naive-vs-aware datetime `TypeError`가 SessionStart 경로의 fail-soft 가드를 탈출. `memory_tier.py:92`가 offset 없는 값에 naive를 반환하고 `memory_tier.py:135`가 aware cutoff와 비교. 레포 내 정답 구현이 `lessons.py:103`에 이미 있다 | `READY`, **P0** |
| `-004` | 결정 tombstone + hard-forget + 삭제 영수증. 설계 완료·압박 테스트 완료(§4.2) | `READY` |
| `-005` | `memory_retrieval` 평가축. `Makefile:70`과 `test_repo_evals.py` 집계를 락스텝으로 갱신해야 함 | `READY` |
| `-006` | `AI_SEARCH_DENSE_AUTO_INSTALL` / `AI_SEARCH_RERANK_AUTO_INSTALL` ↔ `features.embeddings` 정합 + doctor 이그레스 체크. 리랭커 절반은 broken-spawn 수정으로 재분류 | `READY` |
| `-008` | 세션 노트 forget (id 없는 Markdown append 로그이므로 다른 메커니즘 필요) | `READY` |
| `-012` | 구조적 레거시 인덱스에 대해 `search.py`가 mtime에 따라 두 가지 답을 준다 | **`DEFERRED` — 사용자 제품 결정 필요.** 버그 수정으로 디스패치하면 안 된다 |

---

## 8. P0/P1 실험과 측정 가능한 게이트

**하드 게이트(전 실험 공통, 위반 시 실험 실패)**

| 게이트 | 목표값 |
| --- | --- |
| 테넌트/스코프 누출 | `0` |
| 삭제 잔여물 | `0` (단, `union_merge_restorable: true`를 정직하게 보고) |
| 미지원 durable write | `0` |
| 출처/시간성 완전성 | `100%` |
| 기본 네트워크/텔레메트리 | `0` |
| 상태 해시 | 결정적(동일 입력 → 동일 해시) |

**P0 — 순서 고정**

1. **`-011` (naive/aware 정규화)** — 모듈별 정규화 헬퍼 하나씩, `lessons.py:103` 템플릿 복제. 게이트: offset 없는 타임스탬프를 심은 감사 레코드가 `build_context` 경로 어디서도 raise하지 않고 컨텍스트가 렌더된다. 전부 `Z`인 입력에 대해 바이트 동일. `memory.py:788` 회전 가드에 `"ts": null` / `""` 테스트.
2. **`-004` (tombstone / hard-forget)** — CLI 전용, `reject_ci_write("memory")`(`policy.py:57`, `"memory"` ∈ `WRITE_COMMANDS` `policy.py:17-40`) + 필수 `--yes`/`--confirm-id`. **MCP 비노출**(`destructiveHint`가 `sandbox_execute`에만 하드코딩돼 있어 첫 파괴적 메모리 연산이 `destructiveHint: false`로 광고된다). 게이트: 센티널 텍스트가 모든 purge 대상 표면에서 부재, 감사 체인 무손상, 무-tombstone 시 바이트 동일, tombstone이 대상보다 앞선 union-merge 케이스, 압축 중 동시 append 보존, 데드락 가드.
3. **`-005` (`memory_retrieval` 축)** — stdlib만. `ai_core/ranking_metrics.py`(import가 `math`/`time`/`typing`/`collections.abc`뿐인 결정적 랭킹 메트릭)와 `recall_memory(..., now=)` 재사용. 게이트: 결정적 Recall@K/MRR, `make eval` 집계와 `Makefile:70`이 락스텝, **만료 픽스처는 먼 과거/미래 경계 사용**(§4.4 결정성 함정). 주의: `scripts/lint.sh:14`는 `compileall`을 `.ai/runtime/src`와 `.ai/runtime/tests`에만 걸므로 `make lint`가 `.ai/evals/run.py`의 구문 오류를 잡지 못한다.
4. **`-006` (네트워크 기본값 정합)** — 게이트: `features.embeddings: false`인 레포에서 `code_query`가 어떤 소켓도 열지 않음(doctor 이그레스 체크로 증명), `openWorldHint`가 실제 도달 범위와 일치, 리랭커 broken-spawn 제거.

**P1 — P0 이후, 평가축 위에서만 판정**

5. **고정 vs 탐색 투영** — Letta의 memory blocks / archival 구분을 Code Brain HOT/COLD에 대응. 게이트: `memory_retrieval` 축에서 Recall@K 개선 + 주입 토큰 예산 불변.
6. **레코드 수준 소스 신선도** — 결정에 소스 경로/해시/hashline 앵커. 게이트: 출처 완전성 100%, stale 앵커 탐지율 측정.
7. **어휘 baseline 유지 + 선택적 관계/PPR 융합** — **무설치 stdlib/SQLite 비교**: 합성 레코드 10–20건(정정 1건, 삭제 1건, 소스 출처 단정 포함)에서 어휘 baseline vs 인접/PPR 스타일 재랭크. 게이트: 축에서 유의미 개선이 없으면 기각.

---

## 9. 기각 옵션과 이유

**리서치 후보 중 코드 앞에서 무너진 것**

- **Candidate 1(시간성/출처 봉투)** — `decided_at`이 이미 `recorded_at`(`memory.py:411`), `expires_at`이 이미 `valid_to`이며 집행됨, `valid_from`은 소비자 0. 실제 유효성 차원은 버전 스코프(`hooks.py:2119-2132`). Graphiti bitemporal 통째 차용은 장식이었다.
- **Candidate 4(정규 export/import)** — durable 저장소가 이미 평문 append-only JSONL/Markdown이므로 `tar`/`cp`/`git`이 export이고 `ai memory sync`가 이동을 담당한다. 별도 봉투는 스키마 변경마다 동기화할 두 번째 직렬화를 만든다. 이 후보 안의 진짜 필요는 삭제였고, 그건 Candidate 2다.
- **Candidate 5의 리랭커 절반** — 이그레스가 아니라 broken-spawn(§4.3).

**경쟁 후보 반증(2026-08-01 접근)**

- **HippoRAG 2** ([레포](https://github.com/OSU-NLP-Group/HippoRAG), [PMLR](https://proceedings.mlr.press/v267/gutierrez25a.html), [arXiv:2502.14802](https://arxiv.org/abs/2502.14802), MIT) — KG+임베딩+PPR과 ML/LLM/리랭킹 의존성이 운영 비용을 올린다. 검토한 표면은 Code Brain이 요구하는 1급 bitemporal, 해시 감사, 출처 제약, hard-delete 계약을 제공하지 않는다. **전면 도입 기각, PPR 재랭크 개념만 보류.**
- **LightRAG** ([레포](https://github.com/HKUDS/LightRAG), [EMNLP Findings](https://aclanthology.org/2025.findings-emnlp.568/), MIT) — KV/벡터/그래프/문서상태 역할 + LLM/임베딩 필요. 삭제 전파와 local/global 검색 모드는 유용한 개념이지만, 불변 출처·bitemporal 정정·개찬 방지 감사를 공급하지 않는다. **전면 도입 기각.**
- **LangGraph long-term store** ([레포](https://github.com/langchain-ai/langgraph), [stores](https://docs.langchain.com/oss/python/langgraph/stores), [long-term-memory](https://docs.langchain.com/oss/python/langchain/long-term-memory), MIT) — namespace/key JSON 저장과 CRUD는 깔끔한 프리미티브지만, 내구성 있는 프로덕션 예제는 Postgres/pgvector를 추가하고 시간 이력·출처·정정/forget 정책·감사는 애플리케이션 책임으로 남는다. **대체 기각, API 형태만 참고.**

**기존 기각 재확인**: 임베딩/리랭커 기본화(조건부 이득, 스테일 부담 2배 → opt-in 유지), LLM 질의 재작성, 벤더 벤치 기반 선택, 의존성 설치, 호스티드 호출.

---

## 10. 한계와 미증명 사항

1. **커뮤니티 증거 부재.** Reddit HTTP 403, Hacker News HTTP 429로 직접 접근 가능한 항목이 이 패스에서 0건이다. "사용자들이 X를 불만한다"는 어떤 주장도 이 문서에 사실로 들어가지 않았다. GitHub 이슈만 "이슈 작성자 보고"로 인용했고 상태는 2026-08-01 관찰값이므로 이후 바뀔 수 있다.
2. **모든 벤치마크는 미재현.** 다섯 제품 중 어느 것도 이 호스트에서 실행하지 않았다(의존성 설치가 승인 게이트). §5의 수치는 전부 외부 결과다.
3. **CVE는 현재 악용 가능성의 증거가 아니다.** 명확한 것은 Graphiti `0.28.2`와 Cognee `CVE-2026-58473`(`<1.2.0`, `1.2.0` 수정) **둘뿐**이다. 나머지는 미심사 GHSA이거나 패치 버전 표기가 없다.
4. **`-011`의 도달성은 미증명.** 레포 내 모든 writer는 `Z` 접미 `now_iso()`를 발행하므로(`memory.py:55`) 1차 경로에서 naive 타임스탬프는 생기지 않는다. 노출은 외부·수동 편집·구버전·머신 간 git 동기 레코드에 대한 것이다. `.ai/`가 git 동기 대상이고 킷이 15개 소비 프로젝트에 설치돼 있어 개연성은 있으나, **실제로 그런 레코드를 담은 감사 파일을 읽은 분석가는 없다.** 수정은 계약 근거(문서화된 fail-soft를 실제로 fail-soft로 만들고 형제 구현과 정렬)로 정당하다.
5. **공유 술어에 우회 경로가 아직 둘 남아 있다**: `hooks.py:2333-2334` 예외 폴백과 `loop_engineering._conflicting_decisions`(`loop_engineering.py:745-760`). 그래서 `-004`는 리더별 억제가 아니라 헬퍼 내부 억제 + 무조건 압축을 요구한다.
6. **호스트 게이트로 인해 이 문서 작성 중 어떤 테스트·빌드·린트·평가·doctor도 실행하지 않았다.** §7의 수치는 이전 wave의 영수증에서 인계된 값이며, 각 값은 자신을 생산한 산출물 파일에서 교차 확인됐다. `009+010` wave에는 lint 산출물이 없어 그 wave 기준으로는 **미교차확인**으로 기록한다(`1934 passed`가 두 파일의 컴파일·임포트를 이미 증명하므로 실질 손실은 없다).
7. **§6 가중치와 점수는 저자 판단이다.** 측정값이 아니며, 다른 가중치로는 순위가 바뀔 수 있다. 다만 전 제품이 중간값 미달이라는 결론은 `의존성 0`·`기본 네트워크 0` 두 축 어디에 가중을 두어도 유지된다.
8. **Letta Context Repositories와 Mem0 OSS v3는 신선도 검색에서 새로 발견된 항목**(2026-08-01 접근)이며, 원래 분석 receipt에 반영되지 않았다. 어느 것도 독립 재현되지 않았고 판정을 바꾸지 않는다. 다음 라운드 재평가 대상으로 기록한다.

---

*1차 출처(전 URL 접근일 2026-08-01): docs.mem0.ai/overview · docs.mem0.ai/open-source/overview · docs.mem0.ai/migration/oss-v2-to-v3 · github.com/mem0ai/mem0(+/releases) · pypi.org/project/mem0ai · docs.letta.com/reference/terminology · docs.letta.com/letta-code/local-mode · docs.letta.com/guides/agents/archival-memory-overview · letta.com/blog/context-repositories · github.com/letta-ai/letta-code(+/pull/2777) · github.com/letta-ai/letta · help.getzep.com/graphiti/getting-started/overview · help.getzep.com/faq · help.getzep.com/zep-vs-graphiti · blog.getzep.com/announcing-a-new-direction-for-zeps-open-source-strategy · github.com/getzep/graphiti(+/releases) · github.com/getzep/zep · docs.cognee.ai/guides/local-setup · docs.cognee.ai/setup-configuration/llm-providers · docs.cognee.ai/how-to-guides/cognee-cloud · cognee.mintlify.app/how-to-guides/cognee-sdk/deployment/deployment-options · github.com/topoteretes/cognee(+/releases) · GHSA-xgj7-grxr-prrp · GHSA-225m-p565-9h68 · GHSA-xqxw-r767-67m7 · GHSA-gq6f-qwv9-rf4j · GHSA-hp66-92p5-jh23 · GHSA-7p2g-2vxc-5g55 · GHSA-gg5m-55jj-8m5g(CVE-2026-32247, fixed v0.28.2) · NVD CVE-2026-58473 · NVD CVE-2026-31231 · arxiv.org/abs/2410.10813 · aclanthology.org/2024.acl-long.747 · arxiv.org/abs/2507.05257 · aclanthology.org/2025.findings-acl.989 · arxiv.org/abs/2605.12493 · arxiv.org/abs/2601.07978 · github.com/OSU-NLP-Group/HippoRAG · proceedings.mlr.press/v267/gutierrez25a.html · arxiv.org/abs/2502.14802 · github.com/HKUDS/LightRAG · aclanthology.org/2025.findings-emnlp.568 · github.com/langchain-ai/langgraph · docs.langchain.com/oss/python/langgraph/stores · docs.langchain.com/oss/python/langchain/long-term-memory · docs.langchain.com/oss/python/langgraph/persistence. 외부 출처 내용은 라이선스 준수를 위해 재서술했다. 로컬 주장의 근거는 본문 `file:line`이며 전부 현재 작업트리에서 직접 재확인했다.*
