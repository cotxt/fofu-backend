# iOS 앱 분석과 백엔드 계약

## 결론

현재 iOS 프로젝트는 UI와 함께 `URLSession`/`Codable` API 계층, Keychain refresh-token
저장소, 세션 복원 및 Google 로그인을 포함합니다. 일부 화면에는 아직 프로토타입용 로컬
상태와 샘플 데이터가 남아 있습니다. 백엔드는 화면 문자열과 `@State`를 그대로 저장하는
방식이 아니라 iOS·Android·웹이 공통으로 해석할 수 있는 의미 기반 계약으로 설계했습니다.

## 화면에서 확인한 도메인

| 기능 | iOS 근거 | 서버 계약 |
|---|---|---|
| 주변 식당/지도 | `DiscoveryScreens.swift`의 catalog API 기반 목록·지도·커서 페이지 | 좌표, 거리(m), 커서, 식단·알레르겐 필터가 있는 식당 목록 |
| 검색 | `MergedSearchScreen.swift`의 가격·맛·조건·인기 검색 UI | 실제 식당/메뉴 검색, facet과 trending 데이터 |
| 식당/메뉴 | `FoodScreens.swift`, `StoreV4Screens.swift`, `StoreV5Screens.swift` | 정수 가격+통화, 영업시간+timezone, 메뉴 revision, 재고 |
| 음식 상세 | `FoodScreens.swift`의 재료·맛·현지 팁·국가별 리뷰 | 정규화된 재료/알레르겐/식단 claim, 리뷰와 집계 |
| 음식 여권 | `ProfileOwnerScreens.swift`의 pescatarian, 회피 항목, spice tolerance | 사용자별 versioned passport와 `compatible/conflict/unknown` 판정 |
| 한국어 주문표 | `StoreV4Screens.swift`의 장바구니 및 음성 주문 카드 | 현재 가격을 다시 확인해 서버가 만드는 주문표; 결제/POS 주문 아님 |
| Explore | `ProfileOwnerScreens.swift`의 YouTube short 목록 | provider-neutral 커서 feed |
| 메시지 | `MessageScreens.swift`와 `FofuSessionStore`의 API 기반 대화·메시지 상태 | 참여자 권한, 멱등 client message ID, 커서 메시지 목록 |
| 점주 가입/관리 | `ProfileOwnerScreens.swift`의 사업자 서류, 상태, 메뉴 토글 | 비공개 문서, 심사 상태, membership 기반 식당 관리와 audit |
| 언어 | `LanguageSettings.swift`의 20개 BCP-47 언어 | locale-aware 번역 테이블과 영어 fallback |
| Lens | `LensScreen.swift`의 Vision/Translation/QR | iOS OCR/번역은 온디바이스 유지; 자사 HTTPS QR만 서버가 resolve |

## 현재 연동 상태와 서버 결정

1. Discovery는 실제 catalog API에 좌표·거리·필터를 보내고 서버 cursor로 다음 페이지를
   불러옵니다. 화면 모델은 API DTO를 표시 모델로 변환합니다.
2. Home 필터는 main ingredient·price·taste·dish type·영업/거리 조건으로 정규화되어 catalog
   요청에 포함됩니다. 알레르겐·식단 판정의 최종 기준은 여전히 서버 evidence입니다.
3. 즐겨찾기는 `FofuSessionStore.savedRestaurants`와 사용자별 saved API relation을 단일 기준으로
   사용하며, 로그인하지 않은 사용자는 저장 대신 로그인 화면으로 이동합니다.
4. profile은 pork를 피한다고 표시하지만 기본 장바구니에는 삼겹살이 들어 있고 UI가
   "passport checked"라고 단정합니다. 서버는 이를 `conflict`로 반환하며, 알 수 없는
   항목은 절대 안전하다고 추론하지 않습니다.
5. 현재 최종 iOS 흐름은 직원에게 한국어 주문표를 보여 주는 기능입니다. 과거 HTML 시안의
   "주방 전송"은 현재 범위가 아니므로 실제 주문/결제 상태로 가장하지 않습니다.
6. 점주 화면에는 단계별 UI 상태가 남아 있지만 신청·문서 업로드·대시보드용 API 경로가
   연결돼 있습니다. 서버는 업로드만으로 승인하지 않으며 모든 신청을 심사 상태로 둡니다.
7. 일부 Store 화면의 preview/fallback 데이터는 디자인·오프라인 표시용으로 남아 있습니다.
   연결된 흐름의 메뉴·가격·합계 기준은 서버 DB와 cart 응답입니다.

## 외부 통신과 보안 점검

- Google Maps SDK 키가 루트의 `Fofu-Info.plist`에 포함돼 있습니다. 모바일 SDK 키는 번들에서
  완전히 숨길 수 없으므로 Google Cloud에서 iOS bundle ID와 허용 API를 제한하고, 웹/서버용
  키와 분리해야 합니다.
- YouTube 썸네일과 iframe은 현재 클라이언트가 YouTube에 직접 접근합니다. 백엔드는 video ID와
  metadata만 반환합니다.
- Lens는 임의의 HTTP(S) URL을 열 수 있습니다. 향후 iOS 연동 시 자사 HTTPS host는 내부
  Universal Link로 처리하고 외부 host는 사용자에게 명확히 표시해야 합니다.
- Apple Translation/Vision 기반 Lens 데이터는 기기 안에서 처리되므로 이 백엔드에 카메라
  이미지나 OCR 텍스트가 자동 업로드되지 않습니다.

## 현재 구현과 남은 전환

1. Swift DTO와 catalog/account API client가 구현돼 있으며 OpenAPI가 계약의 기준입니다.
2. 앱 시작 시 세션을 복원하고 access token은 메모리, 회전형 refresh token은 Keychain에
   보관합니다. 비밀번호와 Google 로그인 모두 같은 Fofu credential 경로를 사용합니다.
3. Discovery, saved restaurants, profile/passport, cart/order card, message, owner 기능은 API 경로를
   사용합니다. 남아 있는 preview/fallback 상수는 화면별로 계속 축소해야 합니다.
4. 로컬 상태는 표시/optimistic 상태로만 사용하고 저장 성공 응답 또는 재조회 결과로 확정합니다.
5. 공통 네트워크 계층은 bearer/session 거부 코드에만 refresh rotation을 수행하고 cursor를
   검증합니다. cart의 expected version/menu revision을 전송하며, 각 화면은 409 응답 시 최신
   bootstrap/menu/cart를 다시 불러와 구체적인 변경 내용을 보여 줘야 합니다.
