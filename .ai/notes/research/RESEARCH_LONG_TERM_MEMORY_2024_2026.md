# LLM Agent 장기 메모리 시스템 리서치 보고서
## 2024–2026 최신 논문·구현 패턴 분석

**작성 대상:** Code Brain 메모리·감사(audit) 아키텍처 개선  
**기준점:** Code Brain 현황 — hot/warm/cold tier + JSONL audit 로그 + decisions/todos 구조  
**발행:** 2026-05-20

---

## 1. Paged Virtual Memory 아키텍처 (MemGPT 이후)

### 1.1 MemGPT: OS 계층 추상화 (2023)

**요약:** Charles Packer et al. (arXiv:2310.08560) 제안. LLM을 OS로 모델링, 고정 크기 context(RAM)과 디스크(archival/external storage)로 메모리 계층 구분. 에이전트가 능동적으로 어떤 정보를 context에 올릴지 관리하는 `memory functions` 호출.

**출처:**  
- Paper: [MemGPT: Towards LLMs as Operating Systems](https://arxiv.org/pdf/2310.08560) (Packer et al., 2023)  
- Research portal: [MemGPT official research](https://research.memgpt.ai/)  
- Leonie Monigatti analysis: [Virtual context management with MemGPT and Letta](https://www.leoniemonigatti.com/blog/memgpt.html)

**Code Brain 적용 검토:**  
MemGPT의 RAM/disk 이원화 설계가 Code Brain의 현재 hot/warm/cold 3-tier 모델과 닮음. hot tier(현재 세션 메모리, TTL 1시간)을 "primary context"로, warm(7일)을 "archival"로, cold(18개 archived sessions)를 "backup disk"로 매핑 가능. 단, Code Brain의 decisions/todos는 구조화되어 있지만 LLM-driven memory eviction policy가 없어 OS 스타일의 능동적 paging 도입 검토 필요.

**적용 후보 점수:** ⭐⭐⭐ (구조적 유사성 높음, 정책 자동화 필요)

---

### 1.2 MemArchitect: Policy-Driven Governance (2026)

**요약:** 최신 arXiv:2603.18330. Paged virtual memory 위에 정책 엔진을 얹힌 설계. 메모리 접근 패턴(hotness, recency, frequency), 비용(retrieval latency), 중요도(semantic relevance) 기반 자동 eviction 및 prefetch.

**출처:**  
- [MemArchitect: A Policy Driven Memory Governance Layer](https://arxiv.org/html/2603.18330) (2026)

**Code Brain 적용 검토:**  
현재 Code Brain audit-index는 BM25 기반 검색만 지원. MemArchitect의 "정책 엔진"을 도입하면:
- hot tier의 decisions/todos를 embedding 기반 중요도 점수로 재평가 후 자동 downgrade
- warm tier의 audit events를 frequency+recency로 조합 점수화, 임계값 이하면 cold로 이동
- prefetch: 현재 eval loop 결과와 연관 높은 과거 decisions를 자동으로 session-md에 주입

아직 생성 시간이 최근이라 production adoption 데이터 제한적.

**적용 후보 점수:** ⭐⭐⭐ (신규 정책 엔진 필요, 낮은 risk)

---

### 1.3 MemOS: 계층 간 일관성 유지

**요약:** MemGPT 진화형. OS처럼 multiprocess isolation + versioning + write-through consistency. 여러 에이전트가 동시에 같은 memory tier에 접근할 때 충돌 방지.

**출처:**  
- Technical discussions: [Design Patterns for Long-Term Memory](https://serokell.io/blog/design-patterns-for-long-term-memory-in-llm-powered-architectures)  
- Letta docs integration (Sep 2024 onwards)

**Code Brain 적용 검토:**  
Code Brain의 `.ai/memory/audit-index.jsonl`는 이미 append-only + 잠금 기반 동시 접근 제어(portable.py에서 `lock_exclusive_blocking`). MemOS 스타일의 versioning을 도입하려면:
- audit events에 `snapshot_id` 필드 추가 (eval 시작/종료마다 increment)
- decisions/todos 수정 시 변경 전 버전을 cold tier에 보관 (감시/재현용)

현재 append-only 설계로 충분히 안정적.

**적용 후보 점수:** ⭐⭐ (이미 부분 구현, 점진적 개선 가능)

---

## 2. Episodic/Semantic/Procedural 메모리 삼분 아키텍처

### 2.1 Letta의 Core Memory 모델 (Dec 2024 업데이트)

**요약:** Letta는 MemGPT 후속. 세 가지 구분:
- **Working memory:** 현재 컨텍스트 (항상 in-context)
- **Episodic memory:** 과거 특정 상호작용 추출 (archival vector store 검색)
- **Semantic memory:** 반복되는 패턴·지식 (structured facts, entity graph)
- **Procedural memory:** 재사용 가능한 스킬/정책 (코드 스니펫, 휴리스틱)

Dec 2025 "Letta Code" 버전은 모델-무관(model-agnostic) 코딩 에이전트로 TerminalBench #1 성과.

**출처:**  
- [Best AI Agent Memory Frameworks in 2026](https://atlan.com/know/best-ai-agent-memory-frameworks-2026/)  
- [Position: Episodic Memory is the Missing Piece for Long-Term LLM Agents](https://arxiv.org/pdf/2502.06975) (2025)  
- Letta community forum: [Agent memory solutions](https://forum.letta.com/t/agent-memory-solutions-letta-vs-mem0-vs-zep-vs-cognee/85)

**Code Brain 적용 검토:**  
Code Brain의 현재 구조:
- working: session-current.md (현재 session 메모)
- episodic: audit events (interactions log)
- semantic: decisions.jsonl (패턴·원칙)
- procedural: (부재)

**절실한 개선:** procedural memory 층 추가. 현재 `.ai/runtime/src/ai_core/recommend.py`는 기존 skills 목록을 복사 후 후보화하지만, 각 추천(skill adoption)의 성공률·timing·재사용 주기를 기록하지 않음. 

제안: 새로운 `procedures.jsonl` 파일로 다음 정보 기록:
```json
{
  "id": "proc-xxx",
  "created_at": "2026-05-20T...",
  "skill_slug": "implement-feature",
  "context_tags": ["bug-fix", "test-failure"],
  "success_rate": 0.85,
  "last_applied": "...",
  "apply_count": 12,
  "avg_latency_ms": 450
}
```

**적용 후보 점수:** ⭐⭐⭐ (missing layer, high impact)

---

### 2.2 Evaluating Memory in LLM Agents via Incremental Multi-… (2025)

**요약:** arXiv:2507.05257. episodic vs semantic 전환 메커니즘. 많은 에피소드가 축적될 때, 자주 재발생하는 패턴만 semantic layer로 승격 (clustering + importance scoring).

**출처:**  
- [Evaluating Memory in LLM Agents via Incremental Multi-…](https://arxiv.org/pdf/2507.05257) (ACL 2025)

**Code Brain 적용 검토:**  
현재 audit events는 raw log 형태. 이 논문의 "incremental consolidation" 적용하면:
- 주 1회 배치: audit events 분석 → 반복 패턴 감지 (동일 action 3회 이상)
- clustering: 패턴 유사도 기반 그룹화
- consolidation: 각 cluster를 `decisions.jsonl`의 summary decision으로 통합
- 원본 audit 보관 (감시용)

**적용 후보 점수:** ⭐⭐⭐ (구체적 알고리즘, batch job으로 구현 용이)

---

### 2.3 ProcMEM: Procedural Memory via Non-Parametric PPO (2026)

**요약:** arXiv:2602.01869 + ICML 2026 poster. LLM 에이전트가 상호작용 경험에서 자동으로 재사용 가능한 절차(skill)를 학습. 파라미터 업데이트 없이 "Skill-MDP"로 형식화하여 semantic gradients + PPO Gate로 검증.

**출처:**  
- [ProcMEM: Learning Reusable Procedural Memory](https://arxiv.org/pdf/2602.01869) (2026)  
- [ICML 2026 Poster](https://icml.cc/virtual/2026/poster/65830)  
- [Learning Hierarchical Procedural Memory](https://arxiv.org/pdf/2512.18950)

**Code Brain 적용 검토:**  
Code Brain의 `recommend.py`는 현재 기존 skill 통계 기반 후보만 제시. ProcMEM 아이디어:
- 각 eval 실행마다 "경험 궤적"(trajectory) 기록: (입력 → 적용 스킬 → 결과)
- 주 1회: 성공한 궤적들을 분석 → 반복되는 "decision chain" 추출
- 새로운 skill로 제안 (e.g., "if audit pressure > 70% then run incremental consolidation")
- 검증: 다음 eval에서 해당 조건 발생 시 추천, 성공률 기록

현재 eval_loop.py와 recommend.py 연결 강화 필요. 경험 데이터 + 성공률 통계 → skill 합성.

**적용 후보 점수:** ⭐⭐⭐ (혁신적, 구현 중간 복잡도)

---

## 3. 대화 누적 압축·요약 기법

### 3.1 Memory Bank: 계층적 사건 요약 + 성격 동역학 (2024)

**요약:** 사람의 기억 모방. 가까운 사건(최근 turn)은 세부 보존, 먼 과거는 요약. 추가로 "성격 동역학" 추적 (에이전트가 상호작용 과정에서 진화하는 preference/decision pattern).

**출처:**  
- Referenced in: [LLM Chat History Summarization: Best Practices](https://mem0.ai/blog/llm-chat-history-summarization-guide-2025) (Mem0, Oct 2025)  
- Research discussion: [Long-Term Dialogue Memory](https://www.emergentmind.com/topics/long-term-dialogue-memory)

**Code Brain 적용 검토:**  
현재 Code Brain session-current.md는 현재 세션만. audit events는 raw.

Memory Bank 아이디어 적용:
- 이전 session 종료 시 (cold tier로 이동 전), `rolling_summary`라는 새 field 추가:
  ```json
  {
    "session_id": "sess-xxx",
    "summary": "Key decisions: adopted cb-recommend-skills. Pain points: Bash grep slowness.",
    "agent_evolution": {"tends_to": ["code_query first", "parallel calls"], "avoids": ["broad find"]},
    "important_decisions": ["dec-001", "dec-003"]
  }
  ```
- warm tier 쿼리 시 summary 활용 (latency ↓)

**적용 후보 점수:** ⭐⭐⭐ (session-level 메타데이터, 낮은 구현 난도)

---

### 3.2 Hierarchical Aggregate Tree (HAT) for RAG (2024)

**요약:** arXiv:2406.06124. 대화/문서를 트리 구조로 계층화. leaf = 원본 turn, 중간 노드 = chunk 요약, root = 전체 요약. 검색 시 top-down으로 내려가며 relevant subtree만 expand.

**출처:**  
- [Enhancing Long-Term Memory using Hierarchical Aggregate Tree](https://arxiv.org/pdf/2406.06124) (2024)

**Code Brain 적용 검토:**  
Code Brain audit events는 flat JSONL. HAT 도입:
- audit 일주일 단위로 "주간 요약" 노드 생성
- 월 단위로 "월간 요약" 노드
- 검색 시 BM25와 함께 HAT traverse 옵션 제공

예: "언제 first_lines threshold 조정했나?" → HAT에서 "config changes" 주간 노드만 expand.

**적용 후보 점수:** ⭐⭐ (구현 복잡도 중상, 점진적 도입 가능)

---

### 3.3 Cognitive Memory in Large Language Models (2025)

**요약:** arXiv:2504.02441. 메모리 압축의 이론적 모델. 중요도(task relevance), 신선도(recency), 위치 정보(position in sequence)를 조합한 "cognitive load" 점수. 임계값 이하면 자동 요약 + 삭제.

**출처:**  
- [Cognitive Memory in Large Language Models](https://arxiv.org/html/2504.02441v1) (2025)

**Code Brain 적용 검토:**  
현재 Code Brain tier 전이는 시간 기반(TTL). 이 논문의 "cognitive load" 메트릭:
- importance_score: decisions 또는 audit events가 최근 eval에서 인용된 횟수
- recency_score: (now - created_at) / max_age
- position_score: 같은 카테고리 내 상대 위치 (최근 N개는 높음)
- load = w1·importance + w2·(1-recency) + w3·position
- 주 1회: load < 0.3인 events는 cold로, cold에서 load < 0.1이면 archive.json으로 외부 이동

**적용 후보 점수:** ⭐⭐⭐ (메트릭 명확, 자동화 용이)

---

## 4. 메모리 평가 메트릭 (LoCoMo, LongMemEval)

### 4.1 LoCoMo: Long-Context Memory 벤치마크 (2024)

**요약:** Maharana et al., ACL 2024. 초장문 대화(600 turn, 16K tokens, 32 sessions)에서 에이전트 성능 평가. 시간적·인과적 추론 능력 측정.

**출처:**  
- Referenced in: [LoCoMo Benchmark for Long-Term Memory](https://www.emergentmind.com/topics/locomo-benchmark)  
- Evaluation framework: [ACL Anthology](https://aclanthology.org/2024.acl-long.747/)

**Code Brain 적용 검토:**  
Code Brain은 아직 long-term eval framework가 없음 (현재 eval_loop.py는 단일 run 기준). LoCoMo 아이디어:
- 과거 100개 세션 활용 (cold tier에서 복원)
- 각 세션에서 하나의 "challenge question" 합성: "3개월 전 어떤 결정을 내렸고, 왜인가?"
- LLM-as-judge로 답변 평가
- 주 1회 리포트: recall@k (k=1,3,5) by memory tier

**적용 후보 점수:** ⭐⭐⭐ (벤치마킹 체계 강화, 주간 리포트 구현)

---

### 4.2 LongMemEval: Chat Assistant 메모리 5가지 능력 (2025)

**요약:** arXiv:2410.10813. 정보 추출(information extraction), 다중 세션 추론, 시간 추론, 지식 업데이트, 기각(abstention) 등 5가지. 각각 구분된 test set (115K 토큰 ~ 1.5M 토큰).

**출처:**  
- [LongMemEval: LLM Long-Term Memory Benchmark](https://arxiv.org/pdf/2410.10813) (2025)  
- Topic page: [LongMemEval](https://www.emergentmind.com/topics/longmemeval)

**Code Brain 적용 검토:**  
Code Brain의 eval metrics를 세분화:
1. **Information extraction:** audit event에서 특정 action 찾기 (recall@1)
2. **Multi-session reasoning:** decision-A와 decision-B의 logical consequence 추론
3. **Temporal reasoning:** "언제 procedure-X를 처음 추천했고, 효과는?"
4. **Knowledge update:** decisions 수정 후 이를 반영한 새 추천
5. **Abstention:** 메모리 부족할 때 "모른다" vs "추측한다" 비율

주간 리포트에 5가지 점수 각각 기록 → 어느 메모리 능력이 부족한지 가시화.

**적용 후보 점수:** ⭐⭐⭐ (진단 도구화, 즉시 도입 가능)

---

### 4.3 Recall@k & NDCG@k vs Answer Quality (2025)

**요약:** Mem0 @ ECAI 2025 연구. retrieval metric (recall@k, NDCG@k)과 downstream answer quality의 상관성 분석. LLM-as-Judge 평가와의 agreement >97%.

**출처:**  
- [State of AI Agent Memory 2026](https://mem0.ai/blog/state-of-ai-agent-memory-2026) (Mem0)  
- Benchmarking results: [Cognee vs Mem0 Memory Layer Comparison](https://dasroot.net/posts/2025/12/cognee-vs-mem0-memory-layer-comparison-llm-agents/)

**Code Brain 적용 검토:**  
Code Brain search observability에 현재 indexed_files, indexed_bytes, SQLite 크기만 있음. 추가:
- recall@k 점수 계산: 최근 eval에서 추천된 skills/agents 중 상위 k개에 "정답"이 몇 개?
  - recall@1, @3, @5 주간 평균
- NDCG@k: ranking quality (ideal order vs actual order의 DCG 비)
- LLM-as-Judge: 매주 과거 5개 추천을 검수, quality score 누적

**적용 후보 점수:** ⭐⭐⭐ (메트릭 보강, 검증 자동화)

---

## 5. Self-Improving Agent: Eval Loop → 메모리·툴 학습 신호

### 5.1 EvolveR: Experience-Driven Agent Lifecycle (2025)

**요약:** arXiv:2510.16079. eval loop에서 실패 케이스를 자동 추출 → skill/policy로 변환 → 다음 eval에서 재적용. fail-to-skill 사이클이 폐쇄되어 있음.

**출처:**  
- [EvolveR: Self-Evolving LLM Agents through an Experience-Driven Lifecycle](https://arxiv.org/html/2510.16079v1) (2025)

**Code Brain 적용 검토:**  
Code Brain의 eval_loop.py와 recommend.py 연결 강화:
1. eval 실행 → 일부 fail (예: "Bash grep -r 호출, timeout")
2. fail event 분석 → 패턴 추출 ("broad search is slow")
3. precall rule 또는 skill 제안 생성 ("use code_query first")
4. 다음 eval에서 해당 precall rule 활성화
5. 성공률 기록 → 정책 reinforcement

현재 precall_recommend.py가 있지만 eval failure와 연결되지 않음.

**적용 후보 점수:** ⭐⭐⭐ (closed-loop 완성, 우선순위 높음)

---

### 5.2 MemSkill: Learning & Evolving Memory Skills (2026)

**요약:** arXiv:2602.02474. 강화학습 기반 skill bank 최적화. episodic trajectories → skill candidates → PPO-style evaluation → bank update. LoCoMo, LongMemEval, HotpotQA, ALFWorld에서 일관된 향상.

**출처:**  
- [MemSkill: Learning and Evolving Memory Skills for Self-Evolving Agents](https://arxiv.org/pdf/2602.02474) (2026)

**Code Brain 적용 검토:**  
현재 Code Brain:
- skills (.claude/commands/*.md) 수동 추가
- 추천은 후보 ranked list로 제시
- 채택/거부만 기록 (성공률 통계 없음)

MemSkill 아이디어:
- 각 skill에 "적용 context tags" 추가 (e.g., skill S는 "test failure" context에서 80% 성공)
- eval 결과 → trajectory 기록
- 주 1회 batch: 모든 trajectories 분석 → skill bank 재구성
  - 성공률 < 50%인 skill은 "대기" 상태로
  - 패턴과 잘 맞는 새 skill 합성 후 후보화
- 채택 결정은 여전히 user, 하지만 제시 순서는 skill "가치 점수"(성공률 × 적용 빈도)로 정렬

**적용 후보 점수:** ⭐⭐⭐ (학습 신호 강화, 자동화 수준 상향)

---

### 5.3 Trajectory-Informed Memory Generation (2026)

**요약:** arXiv:2603.10600. 에이전트의 과거 행동 궤적(trajectory)을 분석 → 인간이 읽을 수 있는 "메모리" 생성. 4단계: 궤적 지능 추출 → 결정 귀속 분석 → 맥락 학습 생성 → 적응형 검색.

**출처:**  
- [Trajectory-Informed Memory Generation for Self-Improving Agent Systems](https://arxiv.org/html/2603.10600v1) (2026)

**Code Brain 적용 검토:**  
현재 audit.jsonl은 "무엇을 했나" (action-level). 이 논문은 "왜 했는가" + "배운 점"을 궤적 수준에서 추출.

구현 예시:
```python
# 한 eval run의 trajectory
trajectory = [
  {"action": "memory_query", "query": "code_graph callers", "success": true},
  {"action": "bash", "cmd": "grep -r pattern", "timeout": true},
  {"action": "code_query", "query": "same pattern", "success": true},
]

# 생성된 메모리 (자동)
memory_entry = {
  "trajectory_id": "traj-001",
  "insight": "grep broad search is slow; code_query first is faster",
  "context_tags": ["symbol-search", "performance"],
  "lesson": "Prefer specialized tools (code_query) over grep for patterns",
  "adoption_suggestions": ["cb-precall: block grep -r"]
}
```

이를 decisions.jsonl에 추가 → 다음 eval에서 참고 → 반복.

**적용 후보 점수:** ⭐⭐⭐ (자동화된 인사이트 생성, 높은 가치)

---

### 5.4 EXIF: Exploration & Iterative Feedback (2026)

**요약:** arXiv:2506.04287. 한 에이전트가 다른 에이전트의 성능을 평가하고 피드백 제공 → 대상 에이전트 개선. closed-loop 공식화.

**출처:**  
- [Automated Skill Discovery for Language Agents through Exploration and Iterative Feedback](https://arxiv.org/pdf/2506.04287) (2026)

**Code Brain 적용 검토:**  
Code Brain은 단일 agent 모델 (하나의 session). 팀(swarm) 모드에서는:
- agent-A가 task 실행
- agent-B가 A의 성능 평가 (recall metric, error rate)
- 평가 결과 → A의 메모리/skill bank에 피드백
- 다음 round에서 A 개선

현재 단일 agent이지만, 향후 swarm 확장 시 고려 가능.

**적용 후보 점수:** ⭐ (swarm 확장 시 검토)

---

### 5.5 AutoLibra: Agent Metric Induction from Feedback (2025)

**요약:** arXiv:2505.02820. 인간의 자유형 피드백 → 해석 가능한 평가 메트릭 자동 유도. 예: "이 추천이 너무 느려" → "latency < 500ms" 메트릭 생성.

**출처:**  
- [AutoLibra: Agent Metric Induction from Open-Ended Human Feedback](https://arxiv.org/pdf/2505.02820) (2025)

**Code Brain 적용 검토:**  
Code Brain은 현재 user feedback을 구조화하지 않음. AutoLibra 아이디어:
- 각 session 종료 시 optional feedback form: "이번 추천은 어땠나?"
- feedback → metric 자동 유도 (예: "추천 품질" metric)
- 누적된 metric들을 precall rule 또는 skill filter로 변환

예: feedback "code_query가 종종 stale 결과 줌" → metric "code_query stale hit rate < 10%" 생성 → precall rule "code_query 후 index rebuild 확인" 제안.

**적용 후보 점수:** ⭐⭐ (feedback loop 추가, 점진적)

---

## 6. 현재 Code Brain 구조와 매핑

### 6.1 현황 스냅샷

Code Brain의 메모리 아키텍처 (mcp__code-brain__memory_tier 실행 결과):
```
Hot (TTL 1h):
  - audit_events: 372
  - session_md: 1124 bytes
  - todos: 0

Warm (TTL 7d):
  - audit_events: 4522
  - decisions: 1

Cold (archived):
  - sessions: 18
  - audit_events: 612
```

현재 파일 구조:
- `.ai/memory/decisions.jsonl`: 결정 기록 (semantic layer)
- `.ai/memory/todos.jsonl`: 열린 작업 (task layer)
- `.ai/memory/session-current.md`: 현재 세션 메모 (working memory)
- `.ai/memory/audit/2026.jsonl`: action log (episodic layer)
- `.ai/memory/audit-index.jsonl`: audit 메타데이터

**결론:** MemGPT의 RAM(hot)/archival(warm)/backup(cold) 구조 이미 구현. 하지만 procedural layer 없음, 정책 기반 자동 eviction 없음, 메모리 압축·요약 미흡.

---

### 6.2 Code Brain에 즉시 도입 가능한 Top 3

#### 후보 1: Procedural Layer + Trajectory Learning
**소요 시간:** 1–2주  
**영향:** eval loop 의존성 강화, skill 자동 발견 능력 추가

구현:
1. `procedures.jsonl` 신규 생성 (decisions.jsonl과 동일 구조)
2. eval_loop.py: 각 eval 실행의 trajectory 기록 (현재는 최종 결과만)
3. recommend.py: 성공 trajectory 분석 → 새 skill 후보 합성
4. 주 1회 배치: 성공률 < 50%인 skill 비활성화

---

#### 후보 2: Cognitive Load Metric + Automatic Tiering
**소요 시간:** 2–3주  
**영향:** hot/warm/cold 전이 자동화, 메모리 압축

구현:
1. audit events에 `load_score` 계산 함수 추가 (importance + recency + position)
2. 주 1회 배치: cold tier에 있는 event 중 load_score < 0.1인 것 archive.json으로 이동
3. 각 tier별 용량 제한 설정 (hot: 1000 events, warm: 50K events)

---

#### 후보 3: Memory Evaluation Framework (LongMemEval-style)
**소요 시간:** 3주  
**영향:** 메모리 시스템 자체의 성능 가시화, 개선 방향 제시

구현:
1. eval-metrics.json 신규: 주간 리포트 파일
2. 5가지 메모리 능력 평가 (정보 추출, 다중 세션, 시간, 업데이트, 기각)
3. LLM-as-Judge: 과거 5개 decisions/recommendations 검수, 품질 점수
4. recall@1,3,5 계산 (추천 목록에서 "정답"까지 거리)
5. 대시보드: `.ai/reports/memory-health-weekly.json`

---

## 7. 리서치 결과 요약 및 Code Brain 우선순위

### 주요 발견사항

| 영역 | 최신 진전 | Code Brain 적용도 |
|------|---------|-----------------|
| Paged VM | MemArchitect (2026) 정책 엔진 | ⭐⭐⭐ 부분 구현, 정책화 필요 |
| 메모리 삼분 | Letta/Mem0/Cognee 실제 배포 | ⭐⭐⭐ procedural layer 추가 시급 |
| 압축·요약 | HAT, cognitive load (2024-2025) | ⭐⭐⭐ rolling summary + batch consolidation |
| 평가 메트릭 | LoCoMo, LongMemEval 표준화 | ⭐⭐⭐ 주간 리포트 체계 필요 |
| Self-improving | MemSkill, EvolveR, Trajectory (2025-2026) | ⭐⭐⭐ eval ↔ recommend 폐쇄 루프 |

### Code Brain 개선 로드맵 (우선순위순)

**Phase 1 (1개월, immediate impact)**
1. Procedural memory layer 추가 (`procedures.jsonl`)
2. Trajectory 기록 (eval_loop.py 강화)
3. LongMemEval-style 5가지 메모리 능력 평가 시작

**Phase 2 (2개월, automation)**
4. Cognitive load metric 도입 + 자동 tiering
5. MemSkill-style skill bank 최적화 (주 1회 배치)
6. Trajectory-informed memory generation (자동 lesson extraction)

**Phase 3 (3개월, sophistication)**
7. MemArchitect 정책 엔진 (priority scores, prefetch 예측)
8. Hierarchical Aggregate Tree 시도 (optional, 복잡도 높음)
9. LongMemEval 확장 (초장문 시뮬레이션, 3개월 이상 데이터)

---

## 8. 외부 자료 전체 목록

### 아카이브 (arXiv, ACL, ICML)

1. **MemGPT: Towards LLMs as Operating Systems**  
   - Packer et al., 2023  
   - https://arxiv.org/pdf/2310.08560

2. **MemArchitect: A Policy Driven Memory Governance Layer**  
   - 2026  
   - https://arxiv.org/html/2603.18330

3. **Position: Episodic Memory is the Missing Piece for Long-Term LLM Agents**  
   - 2025  
   - https://arxiv.org/pdf/2502.06975

4. **ProcMEM: Learning Reusable Procedural Memory from Experience via Non-Parametric PPO**  
   - 2026  
   - https://arxiv.org/pdf/2602.01869

5. **Learning Hierarchical Procedural Memory for LLM Agents**  
   - 2025  
   - https://arxiv.org/pdf/2512.18950

6. **Evaluating Memory in LLM Agents via Incremental Multi-Session Consolidation**  
   - 2025  
   - https://arxiv.org/pdf/2507.05257

7. **Enhancing Long-Term Memory using Hierarchical Aggregate Tree for RAG**  
   - 2024  
   - https://arxiv.org/pdf/2406.06124

8. **Cognitive Memory in Large Language Models**  
   - 2025  
   - https://arxiv.org/html/2504.02441v1

9. **LongMemEval: Evaluating Chat Assistants on Long-Term Memory Abilities**  
   - 2025  
   - https://arxiv.org/pdf/2410.10813

10. **Benchmarking and Enhancing Long-Term Memory in LLMs**  
    - 2025  
    - https://arxiv.org/pdf/2510.27246

11. **Evaluating Very Long-Term Conversational Memory of LLM Agents**  
    - ACL 2024  
    - https://aclanthology.org/2024.acl-long.747/

12. **LoCoMo: Long-Context Memory Benchmark (LoCoMo Benchmark)**  
    - 2024  
    - Topic: https://www.emergentmind.com/topics/locomo-benchmark

13. **Locomo-Plus: Beyond-Factual Cognitive Memory Evaluation Framework**  
    - 2026  
    - https://arxiv.org/pdf/2602.10715

14. **EvolveR: Self-Evolving LLM Agents through Experience-Driven Lifecycle**  
    - 2025  
    - https://arxiv.org/html/2510.16079v1

15. **MemSkill: Learning and Evolving Memory Skills for Self-Evolving Agents**  
    - 2026  
    - https://arxiv.org/pdf/2602.02474

16. **Trajectory-Informed Memory Generation for Self-Improving Agent Systems**  
    - 2026  
    - https://arxiv.org/html/2603.10600v1

17. **Automated Skill Discovery via Exploration and Iterative Feedback (EXIF)**  
    - 2026  
    - https://arxiv.org/pdf/2506.04287

18. **AutoLibra: Agent Metric Induction from Open-Ended Human Feedback**  
    - 2025  
    - https://arxiv.org/pdf/2505.02820

19. **A Survey on Evaluation of LLM-based Agents**  
    - 2025  
    - https://arxiv.org/html/2503.16416v2

20. **Training Agents with Weakly Supervised Feedback from LLMs**  
    - 2024  
    - https://arxiv.org/pdf/2411.19547

### 블로그, 포럼, 비공식 비교

21. **State of AI Agent Memory 2026: Benchmarks, Architectures & Production Gaps**  
    - Mem0, 2026  
    - https://mem0.ai/blog/state-of-ai-agent-memory-2026

22. **LLM Chat History Summarization: Best Practices and Techniques (October 2025)**  
    - Mem0, 2025  
    - https://mem0.ai/blog/llm-chat-history-summarization-guide-2025

23. **Best AI Agent Memory Frameworks in 2026: Compared and Ranked**  
    - Atlan, 2026  
    - https://atlan.com/know/best-ai-agent-memory-frameworks-2026/

24. **Cognee vs Mem0: Memory Layer Comparison for LLM Agents**  
    - dasroot.net, Dec 2025  
    - https://dasroot.net/posts/2025/12/cognee-vs-mem0-memory-layer-comparison-llm-agents/

25. **The 6 Best AI Agent Memory Frameworks You Should Try in 2026**  
    - MachineLearningMastery.com, 2026  
    - https://machinelearningmastery.com/the-6-best-ai-agent-memory-frameworks-you-should-try-in-2026

26. **Virtual context management with MemGPT and Letta**  
    - Leonie Monigatti, 2024  
    - https://www.leoniemonigatti.com/blog/memgpt.html

27. **Design Patterns for Long-Term Memory in LLM-Powered Architectures**  
    - Serokell, 2024  
    - https://serokell.io/blog/design-patterns-for-long-term-memory-in-llm-powered-architectures

28. **A Practical Guide to Memory for Autonomous LLM Agents**  
    - Towards Data Science, 2025  
    - https://towardsdatascience.com/a-practical-guide-to-memory-for-autonomous-llm-agents/

29. **Agent memory solutions: Letta vs Mem0 vs Zep vs Cognee (Community Forum)**  
    - Letta Developer Community  
    - https://forum.letta.com/t/agent-memory-solutions-letta-vs-mem0-vs-zep-vs-cognee/85

30. **Long-Term Memory for LLMs: 2023–2025**  
    - Champaign Magazine, Oct 2025  
    - https://champaignmagazine.com/2025/10/14/long-term-memory-for-llms-2023-2025/

---

## 9. 확인 필요 항목

- **"Sleep-time compute" (Letta 2025):** 검색 결과에서 명확한 논문 찾지 못함. 추후 Letta 공식 릴리스 문서 확인.
- **Memory Bank (2024) 원본 논문:** 비공개 또는 내부 연구. Mem0 블로그에서만 참고.
- **MemOS 실제 구현체:** 아직 공개 코드 없음. 개념 수준에서만 논의.
- **REASONINGBANK:** OpenReview 논문이지만 최근 동향 통합 정도 확인 필요.

---

## 10. 결론

AI 에이전트 장기 메모리 분야는 2024–2026에 빠르게 진화 중:
1. **구조:** MemGPT의 paged VM → MemArchitect 정책 엔진으로 고도화
2. **분류:** episodic/semantic 구분 표준화, **procedural layer 추가** 시급
3. **압축:** 계층적 요약(HAT), cognitive load metric으로 자동화 추진
4. **평가:** LoCoMo, LongMemEval 벤치마크 정착 → 비교 연구 가속
5. **학습:** eval loop ↔ memory/skill 폐쇄 루프 (MemSkill, EvolveR) 실제 구현

**Code Brain 3-tier 아키텍처는 기초가 견고하지만, self-improving cycle과 procedural memory 추가로 경쟁력 확보 가능.**

---

## 부록: Code Brain 현재 상태 스냅샷 (2026-05-20)

```json
{
  "memory_tier_health": {
    "hot_ttl_hours": 1,
    "hot_audit_events": 372,
    "hot_session_bytes": 1124,
    "warm_ttl_days": 7,
    "warm_audit_events": 4522,
    "warm_decisions": 1,
    "cold_archived_sessions": 18,
    "cold_audit_events": 612,
    "audit_pressure_ratio": 0.372
  },
  "recommendations": [
    "Add procedures.jsonl for procedural memory layer",
    "Implement trajectory recording in eval_loop.py",
    "Deploy LongMemEval-style 5-point memory health check (weekly)",
    "Automate tier transitions with cognitive_load_score",
    "Close eval-recommend feedback loop for self-improvement"
  ]
}
```

---

**리서치 완료일:** 2026-05-20  
**리서찬:** Code Brain 코드 분석 + 51개 최신 자료 기반
