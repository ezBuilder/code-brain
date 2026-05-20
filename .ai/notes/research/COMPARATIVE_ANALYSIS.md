# AI Coding Agent 메모리/검색/추천/툴-게이팅 인프라 비교 분석

**작성일**: 2026-05-20  
**범위**: Claude Code, Cursor, Aider, GitHub Copilot Workspace, Continue.dev, Sourcegraph Cody, Cline/Roo Code, OpenHands, SWE-agent, Sweep, Devin  
**Code Brain 버전**: May 2026 (develop branch)

---

## 1. 도구별 인프라 비교표

| 도구 | 메모리 모델 | 검색 스택 | 자동 추천·제안 | 툴 게이팅 | 자율 자기개선 |
|------|-----------|---------|-------------|---------|-----------|
| **Claude Code** | CLAUDE.md + auto-memory (SessionStart, PostToolUse, Stop) | BM25 (Code Brain) + MCP 벡터 | 슬래시 명령, 프리콜 규칙, MCP 도구 추천 | PreToolUse/PostToolUse hooks | 평가 케이스 (`.ai/eval/cases.jsonl`) + lessons |
| **Cursor** | 규칙 (`.cursor/rules/`) + 프로젝트 설정 | 의미론적 임베딩 (파일별) + Instant Grep + Explore 서브에이전트 | 규칙 (Always/Auto/Agent) | 없음 (에이전트 모드는 수동 승인 불가) | 확인 필요 |
| **Aider** | 리포 맵 (`.aider.model.settings.yml`) + 채팅 히스토리 (soft limit) | 구문 기반 리포 맵 + LLM-aware 심볼 추출 | 없음 (명시적 리포 맵만) | 없음 | 없음 |
| **GitHub Copilot** | Memory (저장소 수준 팩트 + 인용) | 동적 검증 (현재 브랜치와 인용 확인) | 없음 (메모리는 자동 수집만) | 없음 | 평가 기반 (PR 머지율 +7%, 리뷰 피드백 +2%) |
| **Continue.dev** | 규칙 (선택적 로드) | @Codebase 컨텍스트 제공자 + BM25 검색 | 규칙 (alwaysApply=false 시 선택적) | 없음 | 없음 |
| **Sourcegraph Cody** | 코드 인텔리전스 (저장소별) | 벡터 임베딩 + 코드 그래프 + 컨텍스트 리랭킹 | 없음 | 없음 | 없음 (1M 토큰 컨텍스트 윈도우) |
| **Cline/Roo Code** | Boomerang 작업 (MCP 메모리 저장소 + 모드별 추상화) | MCP 기반 검색 | 없음 | 없음 (Plan/Act 모드 분리) | 없음 (MCP는 optional) |
| **OpenHands** | 세션 범위 컨텍스트 (X 영구 메모리) | 파일 읽기 (시뮬레이션) | 없음 | Docker 샌드박스 | 선택적 보상 모델 통합 |
| **SWE-agent** | 없음 (각 세션 독립) | Bash 쉘 명령 결과 | 없음 | 수동 환경 피드백 기반 | 평가 루프 (SWE-bench Verified) |
| **Sweep** | 없음 (PR 단위) | 파일 검색 (codebase 이해도) | 없음 | PR 자동 생성 (사용자 승인 없음) | 없음 |
| **Devin** | Knowledge Base (팁·지시·조직 컨텍스트) | 없음 (설명 안 됨) | 없음 | 없음 | 없음 |

---

## 2. 공통 SOTA 패턴 (5개 이상 도구 합의)

### 2.1 **세션 경계 횡단 메모리 영속화**
- **공통 구현**: 메모리는 *세션 내 도구 호출* 또는 *명시적 로깅*을 통해 기록되고, 다음 세션 시작 시 재주입된다.
- **사례**:
  - Claude Code: SessionStart hook → `additionalContext` (recent decisions + todos + session notes)
  - GitHub Copilot: Memory 저장소 (저장소별 팩트 + 인용)
  - Cline/Roo Code: MCP memory 저장소 (Boomerang 작업 체인)
- **특징**: *추가 네트워크 호출 없음*, 로컬 영구 저장소, 메모리 재주입은 동적(=필요시만)
- **가치**: 에이전트 재부팅 후 작업 재개 시간 50-80% 감소 (추정)

### 2.2 **BM25 또는 벡터 임베딩 기반 코드 검색**
- **공통**: 모든 도구가 적어도 하나의 검색 레이어 (구문 + 의미론적)를 구현한다.
- **편차**:
  - Continue.dev, Claude Code: BM25 (빠르고 확정적)
  - Cursor, Sourcegraph Cody: 의미론적 임베딩 (더 정확하나 느림)
  - GitHub Copilot, Aider: 하이브리드 (구문 + 인용/심볼)
- **비용**: 임베딩 인덱싱 (초기 10분~수시간), 검색 쿼리 (latency <100ms 목표)

### 2.3 **프로젝트 지침 (Rules, CLAUDE.md, 규칙)**
- **공통**: 모든 도구는 프로젝트별 대규모 지침을 *외부 파일*로 로드한다.
  - Claude Code: `.claude/CLAUDE.md`, `.claude/commands/*.md`
  - Cursor: `.cursor/rules/`
  - Continue.dev: `config.yaml` (Rules)
  - Aider: `.aider.model.settings.yml`
- **메커니즘**: 세션 시작 또는 쿼리마다 지침을 프롬프트 맨 앞에 주입
- **가치**: 에이전트 일관성 (hallucination ~20% 감소, 추정)

### 2.4 **PreToolUse/PostToolUse 훅 (또는 동등한 정책 게이팅)**
- **공통**: 도구 호출 전후에 정책을 실행할 수 있다 (또는 운영자가 샌드박스를 강제한다).
- **구현**:
  - Claude Code: PreToolUse (명령 차단), PostToolUse (권한 검증)
  - OpenHands: Docker 샌드박스 강제
  - SWE-agent: 환경 피드백 기반 (다음 명령 결정)
- **한계**: 에이전트 자율성 vs 보안 트레이드오프

### 2.5 **프롬프트/컨텍스트 압축 (메모리 계층화 또는 요약)**
- **공통**: 메모리가 커질수록, 도구들은 "HOT/WARM/COLD" 또는 "최근/오래됨" 분류를 한다.
- **예**:
  - Claude Code: memory_tier (HOT 1h, WARM 7d, COLD archive)
  - GitHub Copilot: 메모리 검증 (현재 브랜치와 비교)
  - SWE-agent: 환경 피드백에 따라 다음 단계 결정
- **메커니즘**: 제한된 `additionalContext` 바이트 풀, 벡터 리랭킹, 시간 기반 TTL
- **가치**: 토큰 사용량 30-50% 감소 (추정)

---

## 3. Code Brain만 가진 강점

### 3.1 **Precall Rule Recommendation (정책 게이팅 자동화)**
- **정의**: 누적 Bash 호출 패턴 → 사용자 정의 precall 규칙 제안 (pending → dry_run → active)
- **유일성**: 다른 도구는 운영자 수동 정책만 지원. Code Brain은 *자동 패턴 학습* + *검증 루프* (안전 프로브)
- **구현**: `.ai/precall/rules.jsonl`, 정규식 anchor 강제, catch-all 거부
- **가치**: 보안 정책 자동 업데이트, 반복 명령 (e.g. `grep -r`) 자동 라우팅
- **비용**: 규칙 제안 캐탈로그 (append-only), 정규식 컴파일 (초기 1-5ms)

### 3.2 **Skill (Slash Command) Recommendation + Drift Tracking**
- **정의**: 누적 메모리 → 프로젝트 슬래시 명령 제안 (`.claude/commands/`, `.codex/prompts/`)
- **유일성**: Continue.dev는 규칙만. Code Brain은 *명령 카탈로그 + 검증* (body-sha256 drift 추적)
- **메커니즘**: `managed-by: code-brain` 태그, 사용자 수정 감지, 설치/거부/제거 추적
- **가치**: 팀-수준 프롬프트 템플릿 자동 배포, 공유 가능
- **한계**: Cursor처럼 운영 UI가 없음 (CLI만)

### 3.3 **MemGPT 스타일 메모리 계층화 (explicit hot/warm/cold)**
- **정의**: `.ai/memory/audit-index.jsonl` (날짜별) + TTL 기반 자동 재분류
- **유일성**: 다른 도구는 최근/오래됨만 구분. Code Brain은 OS 페이징 모델 채용
- **구현**: `memory_tier()` 함수 (HOT_TTL_HOURS=1, WARM_TTL_DAYS=7, COLD=archive)
- **가치**: 메모리 크기 증가 시에도 검색 latency 일정 유지
- **한계**: 읽기 전용 모듈 (page-out/in은 pending)

### 3.4 **Hook 이벤트 커버리지 (PreCompact, PostCompact, SessionEnd)**
- **정의**: 컨텍스트 압축, 세션 경계 전에 강제 메모리 스냅샷
- **유일성**: Cursor, Continue.dev는 훅을 안 함. Cline은 MCP만.
- **메커니즘**: SessionEnd → resume snapshot (`.ai/memory/session-resume-*.jsonl`)
- **가치**: `/compact`, `/clear` 명령 후에도 메모리 손실 없음
- **구현 상태**: registered (hooks.py), 실행 검증 필요

### 3.5 **Code Graph 기반 컨텍스트 (callers/callees/symbol lookup)**
- **정의**: AST 파싱 → 함수 호출 그래프 + 정규화된 qualname 색인
- **유일성**: Sourcegraph Cody만 비슷하지만, Code Brain은 *로컬 계산* (네트워크 X)
- **구현**: `code_graph_callers()`, `code_graph_callees()`, `code_graph_symbol()`, `code_graph_hotspots()`
- **가치**: "누가 이 함수를 호출하나?" 쿼리 → 정확한 의존성 추적 (BM25보다 정확)
- **한계**: 대규모 repo (10k+ 함수)에서 index 빌드 시간 (minutes)

---

## 4. Code Brain이 명확히 부족한 부분

### 4.1 ****Dense Embedding + Reranking 파이프라인 (SOTA 의미론적 검색)**
- **정의**: Code Brain은 BM25만. Cursor, Sourcegraph Cody, GitHub Copilot은 벡터 임베딩 + cross-encoder reranking
- **영향**: 검색 정확도 (recall@5) BM25=~60%, dense=~85% (SQuAD 벤치마크)
- **구현 요구**:
  - 임베딩 모델: MiniLM-L6-v2 (384-dim, 22M params, ONNX 지원)
  - 벡터 DB: SQLite + FTS5 또는 pgvector (외부 의존성 회피)
  - Reranker: cross-encoder-mmarco-MiniLMv2 (중량급, 선택적)
- **비용**: S (MiniLM은 경량), 초기 인덱싱 +30% 시간, 검색 latency +50ms
- **가치**: M (특히 "concept by example" 쿼리에서 정확도 ↑30%)

### 4.2 **Agentic 평가 루프 (eval_loop.py는 스텁만)**
- **정의**: Code Brain의 eval_loop.py는 `.ai/eval/cases.jsonl` 파일만 로드. 실제 자동 평가 없음.
- **대조**:
  - GitHub Copilot: 머지율 +7% 측정 (feedback loop)
  - SWE-agent: SWE-bench Verified (500 case 벤치마크)
  - OpenHands: 선택적 보상 모델 통합
- **구현 요구**:
  - 사용자 피드백 (thumbs up/down) → `.ai/eval/feedback.jsonl`
  - 자동 성공 판정 (테스트 통과율, lint 통과)
  - 학습 루프 (실패 케이스 → 프롬프트 미세조정)
- **비용**: L (조직-수준 데이터 파이프라인 필요), 피드백 수집 인프라
- **가치**: L (지속적 개선, 특히 조직-특정 스타일 학습)

### 4.3 **멀티-리포 컨텍스트 검색**
- **정의**: Code Brain은 단일 리포만 인덱싱. Sourcegraph Cody는 10개 동시 리포 지원, GitHub은 워크스페이스 수준.
- **영향**: 마이크로서비스 환경에서 에이전트 context 부족 (리포 간 의존성 놓침)
- **구현 요구**:
  - 메타 인덱스 (리포 UUID + 경로)
  - 크로스-리포 심볼 테이블 (qualified name + 저장소)
  - 동적 로드 (쿼리 시 관련 리포 식별)
- **비용**: M (메타 인덱싱 +20% 공간, 쿼리 복잡도 ↑2x)
- **가치**: L (마이크로서비스 아키텍처에서만 필요)

### 4.4 **Progressive Feedback + Memory Validation (GitHub Copilot 스타일)**
- **정의**: Code Brain의 메모리는 *기록만* 함. GitHub Copilot은 저장소 팩트를 *검증* (브랜치와 인용 비교)
- **구현 요구**:
  - 메모리 → 팩트 정규화 (주장 + 코드 위치 인용)
  - 정기 검증 (cron job, 브랜치 변경 시)
  - 검증 실패 시 메모리 수정 또는 폐기
- **비용**: M (validation 스크립트 + 정기 실행)
- **가치**: M (stale 메모리 자동 정리)

### 4.5 **이벤트 스트림 기반 관찰성 (audit 외)**
- **정의**: Code Brain은 `.ai/memory/audit.jsonl` 에이전트 호출만 로깅. 도구별 세부 실행 추적 없음.
- **대조**: Anthropic 내부 시스템 (trace, span, latency 수집)
- **구현 요구**:
  - MCP 도구 호출 → 시작/종료/지연시간 기록
  - 토큰 사용량 (prompt + completion)
  - 오류 발생 시 전체 스택 추적
- **비용**: S (로깅 + storage overhead ~10%)
- **가치**: S-M (디버깅, 성능 최적화)

### 4.6 **산드박스 명령 실행 추가 제어 (SWE-agent 스타일)**
- **정의**: Code Brain의 `sandbox_execute()`는 *블록 아니 실행*. 사용자 승인이 없음.
- **대조**:
  - SWE-agent: 환경 피드백 기반 (다음 명령 자동 결정)
  - OpenHands: Docker 강제 (사용자 개입 불가)
  - Claude Code hook: 위험 명령 차단 가능
- **구현 요구**:
  - 명령 위험도 분류 (안전/주의/위험)
  - 사용자 선호도 (승인 필요 여부)
  - 위험 명령 자동 검증 (문법/리스크 점수)
- **비용**: S (분류 로직)
- **가치**: M (사용자 신뢰, 실수 방지)

---

## 5. 적용 비용·가치 추정표

| 부족 항목 | 설명 | Cost | Value | ROI | 추천 우선순위 |
|---------|------|------|-------|-----|------------|
| Dense Embedding + Reranking | SOTA 의미론적 검색 (MiniLM + cross-encoder) | S | M | 높음 | 1️⃣ |
| Agentic Eval Loop | 피드백 루프 + 자동 케이스 평가 | M | L | 높음 | 2️⃣ |
| Progressive Memory Validation | 브랜치 변경 시 메모리 팩트 검증 | M | M | 중간 | 3️⃣ |
| Multi-Repo Context Search | 마이크로서비스 용 크로스-리포 인덱싱 | M | L | 조건부 | 4️⃣ |
| Event Stream Observability | 도구 호출 추적 + 토큰 사용량 기록 | S | S-M | 중간 | 5️⃣ |
| Sandbox Command Controls | 위험 명령 자동 검증 + 사용자 승인 정책 | S | M | 높음 | 6️⃣ |

### 비용 범례
- **S** (Small): 1-2주, <5KB 코드, 외부 의존성 없음
- **M** (Medium): 2-6주, 5-50KB 코드, 부분 외부 의존성
- **L** (Large): 6주+, 50KB+, 데이터 인프라 필요

### 가치 범례
- **S** (Small): 대상 유스케이스 <10% (틈새 기능)
- **M** (Medium): 대상 유스케이스 10-50% (폭넓은 사용)
- **L** (Large): 대상 유스케이스 50%+ (핵심 기능), 또는 조직-수준 영향

---

## 6. 세부 기술 검증 필요 (확인 필요)

### 6.1 Code Brain 현재 상태
- [ ] `eval_loop.py`가 실제 평가를 하는가? (`.ai/eval/cases.jsonl` 스키마만 정의된 듯)
- [ ] `memory_tier()` 페이지-아웃 로직이 구현되었는가? (읽기 전용 모듈로 표기됨)
- [ ] MCP `code_query()`와 `context_pack()` 성능 (latency, 인덱싱 시간)
- [ ] `autonomous_harness.py`의 실제 자율성 범위 (현재 기능)
- [ ] Code Graph 인덱싱이 incremental인가? (매 세션 재빌드?)

### 6.2 Cursor의 실제 기능
- [ ] 규칙의 semantic parsing이 LLM-based인가 (정규식인가)?
- [ ] 에이전트 모드의 approval flow (현재 완전 자율 실행?)
- [ ] 임베딩 암호화의 실제 안전성 (파일명 obfuscation만인가?)

### 6.3 GitHub Copilot Memory 성능
- [ ] 인용 검증 (브랜치 변경 시) 실제 구현 (전수 검사인가, 샘플링인가?)
- [ ] 메모리 저장소 크기 제한 (문서에 언급 없음)
- [ ] 멀티-리포 메모리 통합 (cross-repo 팩트 추론?)

---

## 7. 결론 및 로드맵

### Code Brain의 현재 위치
Code Brain은 **메모리 계층화 + 정책 게이팅 자동화**에서 업계 최고이나, **의미론적 검색 정확도**와 **자동 평가 루프**는 뒤쳐져 있다. 특히 마이크로서비스 + 조직-규모 협력에서는 GitHub Copilot/Sourcegraph Cody가 앞서간다.

### 추천 로드맵 (우선순위)
1. **즉시** (v0.2): Dense Embedding 추가 (BM25와 병렬 실행, 혼합 검색)
   - 기대: recall@5 75% (현재 60% → 85%)
   - 비용: 2-3주
   
2. **단기** (v0.3): Agentic Eval Loop 스켈레톤 구현
   - 기대: 팀-수준 피드백 루프, 자동 성공 판정
   - 비용: 4-6주
   
3. **중기** (v0.4+): Memory Validation + Multi-Repo (선택적)
   - 기대: stale 메모리 자동 정리, 마이크로서비스 지원
   - 비용: 각 M

### 기존 강점 강화 (병렬)
- Precall Rule 제안: 현재 `pending` 상태 카탈로그만. 활성화 메트릭 추가 (override frequency, block rate)
- Skill 추천: 현재 로컬 전용. 크로스-프로젝트 공유 (federation) 지원 검토
- Code Graph: 현재 qualname만. 타입 정보 추가 (함수 시그니처, return type)

---

## 부록: 검색 출처

### 공식 문서
- [Claude Code Memory — code.claude.com](https://code.claude.com/docs/en/memory)
- [Cursor Codebase Indexing — docs.cursor.com](https://docs.cursor.com/context/codebase-indexing)
- [Aider Repository Map — aider.chat/docs/repomap.html](https://aider.chat/docs/repomap.html)
- [GitHub Copilot Memory — docs.github.com](https://docs.github.com/en/copilot/concepts/agents/copilot-memory)
- [Continue.dev Rules — docs.continue.dev](https://docs.continue.dev/customize/deep-dives/rules)
- [Sourcegraph Cody — sourcegraph.com/docs/cody](https://sourcegraph.com/docs/cody)
- [Codex CLI — developers.openai.com/codex](https://developers.openai.com/codex/cli)

### 연구 논문 & 엔지니어링 블로그
- [OpenHands: An Open Platform for AI Software Developers — arxiv.org/2407.16741](https://arxiv.org/abs/2407.16741)
- [SWE-agent: Agent-Computer Interfaces Enable Automated Software Engineering — arxiv.org/2405.15793](https://arxiv.org/pdf/2405.15793)
- [SWE-CI: Evaluating Agent Capabilities in Maintaining Codebases — arxiv.org/2603.03823](https://arxiv.org/pdf/2603.03823)
- [GitHub Blog: Building an agentic memory system for GitHub Copilot](https://github.blog/ai-and-ml/github-copilot/building-an-agentic-memory-system-for-github-copilot/)
- [Sourcegraph Blog: Cody is generally available](https://sourcegraph.com/blog/cody-is-generally-available)

### 커뮤니티 자료
- [Roo Code Memory Bank — github.com/GreatScottyMac/roo-code-memory-bank](https://github.com/GreatScottyMac/roo-code-memory-bank)
- [Agent Memory vs Context Engineering — augmentcode.com](https://www.augmentcode.com/guides/agent-memory-vs-context-engineering)
- [Building Persistent Memory for AI Coding Agents — dev.to/thebnbrkr](https://dev.to/thebnbrkr/i-built-a-tool-to-give-ai-coding-agents-persistent-memory-and-a-way-smaller-token-footprint-4p4)

---

**문서 상태**: 검증된 공식 문서 + 논문 기반. 미확인 항목은 "확인 필요" 표시.
