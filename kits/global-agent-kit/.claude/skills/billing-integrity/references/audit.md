# 기존 결제 시스템 감사 절차

코드만 보면 절반만 본 것이다. **코드 · 인프라 설정 · 운영 데이터** 셋을 다 봐야 한다.
실제로 가장 비쌌던 사고는 코드가 완벽한데 인프라 설정이 죽어 있던 경우였다.

## 0. 감사 전 원칙

- **추측으로 단정하지 않는다.** "정상으로 보인다"와 "정상임을 확인했다"를 구분해 보고한다.
- 읽기 전용 조회와 dry-run은 진행하되, **운영 DB 쓰기·시크릿 변경·배포는 승인 후에** 한다.
- 사용자가 증상을 제보했다면 **증상에서 역산**한다. 코드부터 뒤지면 시간을 버린다.

## 1. 코드 축 — 무엇을 grep 하나

```
# 결제 진입점
verifyStorePurchase|applyStoreEvent|store-purchase|storeWebhook

# 판정 로직
isEntitlementRowCurrentlyActive|resolveTier|grantedGates|hasPlusAccess

# 리컨사일·크론
reconcile|setInterval|CronJob|acquireLock

# 멱등
storeEventId|purchaseTokenHash|uniqueIndex|onConflictDoNothing
```

확인 항목:

- [ ] 구독/소모성이 서로 다른 스토어 엔드포인트로 가는가 (§5)
- [ ] 응답 검증이 실제로 존재하는 필드를 보는가 (§5)
- [ ] 만료 앵커 없는 active 행이 활성으로 판정되지 않는가 (§6)
- [ ] 조회 실패 경로가 권한 회수로 이어지지 않는가 (§2)
- [ ] 멱등키에 userId가 포함되는가 (§4)
- [ ] 회수 이벤트가 별도 키 네임스페이스를 쓰는가 (§4)

## 2. 인프라 축 — 코드로는 절대 못 잡는 것

- [ ] 웹훅 push 엔드포인트가 **지금** 운영 도메인을 가리키는가
      (개발용 터널 주소가 박혀 있던 사고가 실재한다)
- [ ] 최근 전달 성공률 / 실패 큐 적재량
- [ ] 결제 관련 환경변수가 실제 컨테이너에 주입돼 있는가
- [ ] topic과 push subscription이 모두 존재하는가(topic만 있으면 전달되지 않음)
- [ ] push endpoint, explicit OIDC audience, service-account email이 운영 설정과 정확히 일치하는가
- [ ] Pub/Sub 서비스 에이전트의 token creator와 스토어 게시자의 publisher 권한이 있는가
- [ ] 스토어 테스트 알림이 운영 인박스에 실제 도착했는가
- [ ] 상태 API의 HTTP 200이 아니라 billing readiness 세부 항목이 모두 정상인가
- [ ] App Store 신규 SKU의 판매 지역·심사 스크린샷·제출/승인 상태가 모두 갖춰졌는가
- [ ] Google Play 신규 SKU의 base plan이 대상 국가에서 `활성`인가
- [ ] 스토어 설치 실기기에서 productId 조회와 결제 확인창 진입이 되는가

```bash
# 컨테이너에 주입된 결제 env "이름만" 확인 (값 출력 금지)
docker service inspect <svc> \
  --format '{{range .Spec.TaskTemplate.ContainerSpec.Env}}{{println (index (split . "=") 0)}}{{end}}' \
  | grep -iE 'STORE|BILLING|GOOGLE_PLAY|APP_STORE|ALERT'
```

## 3. 데이터 축 — 조회 쿼리

테이블/컬럼명은 프로젝트에 맞게 바꾼다. 전부 읽기 전용이다.

```sql
-- (a) 웹훅 생사 판별. 0이면 인프라 설정을 즉시 의심한다.
SELECT event_type, count(*), max(created_at)
  FROM entitlement_events
 WHERE created_at > now() - interval '7 days'
 GROUP BY event_type ORDER BY 2 DESC;

-- (b) 무음 강등 피해 규모 — 구독 행이 있는데 기간이 과거인 사용자
SELECT count(DISTINCT user_id)
  FROM subscriptions
 WHERE platform = 'google_play'
   AND status IN ('active','canceled')
   AND current_period_ends_at < now();

-- (c) 영구 권한 위험 — 만료 앵커 없는 활성 행
SELECT count(*) FROM subscriptions
 WHERE status = 'active' AND current_period_ends_at IS NULL;

-- (d) 재검증 가능성 — 원문 토큰 보유율
SELECT platform,
       count(*) AS total,
       count(*) FILTER (WHERE purchase_token_enc IS NOT NULL) AS recoverable,
       round(100.0 * count(*) FILTER (WHERE purchase_token_enc IS NOT NULL)
             / NULLIF(count(*), 0), 1) AS recoverable_pct
  FROM subscriptions GROUP BY platform;

-- (e) 이중 지급 흔적 — 같은 주문에 지급 이벤트가 2건 이상
SELECT store_event_id, count(*)
  FROM entitlement_events
 WHERE event_type IN ('initial_purchase','renewal')
GROUP BY store_event_id HAVING count(*) > 1;

-- (f) 전송 폐쇄루프 증거. 테스트 알림은 transport 증거이며 renewal 처리 증거와 구분한다.
SELECT source, status, result, max(received_at)
  FROM store_webhook_inbox
 WHERE received_at > now() - interval '24 hours'
 GROUP BY source, status, result;
```

## 4. 증상 → 원인 역추적표

사용자 제보에서 시작할 때 이 표로 후보를 좁힌다.

| 증상 | 1순위 후보 | 확인 방법 |
| --- | --- | --- |
| "구독했는데 광고가 나온다" / "무료로 보인다" | 웹훅 유실로 인한 무음 강등 | 쿼리 (a) (b) |
| "결제했는데 재화가 안 들어온다" | 검증 응답 오독, 시크릿 누락 | 부팅 로그, §5 |
| 특정 계정만 오류 | 컬럼 길이/정수 범위 초과 | 서버 에러 로그 원문 |
| 차감이 실제보다 많다 | 누적 스냅샷을 per-run 합산 | 원장(charges) 직접 집계 |
| 결제 이벤트가 0건 적재 | 삽입 실패가 조용히 넘어감 | 스키마 타입, 삽입 에러 로그 |
| 해지했는데 계속 쓴다 | 만료 앵커 NULL | 쿼리 (c) |
| "업그레이드 실패" + StoreKit/상품 조회 오류 | 신규 SKU 판매 지역·심사 자료·승인 누락 | 양 스토어 콘솔 상태를 각각 확인(§15) |
| 사이드로드 Android에서 Play 결제 불가 | 설치 출처·서명 검증의 정상 차단 | `pm list packages -i`, 스토어 설치본 재검증 |

## 5. 감사 보고 형식

```
[확인된 사실]  - 코드/쿼리 근거를 파일:줄 또는 쿼리 결과로 명시
[미확인]      - 권한이 없거나 접근 못 한 것을 그대로 적는다
[위험]        - 심각도 + 영향 범위(추정이면 추정이라고 명시)
[조치안]      - 승인 필요 여부를 항목마다 표시
[완료 판정]   - HTTP, readiness, 테스트 전달, 실제 이벤트, 토큰 보유율을 각각 표시
```

"확인했다"와 "그래 보인다"를 섞지 않는다. 결제에서 잘못된 확신은 돈으로 청구된다.
`unsupported_notification` 테스트 수신은 전송·인증·인박스 증거이지 실제 갱신 처리 증거가 아니다.
