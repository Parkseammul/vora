# VORA Dual Login: Cognito + Kakao OIDC

VORA는 하나의 Cognito User Pool을 인증 Source of Truth로 사용한다. 사용자는 VORA Managed Login 또는 Kakao Login을 선택하고, 두 경로 모두 Cognito가 발급한 Authorization Code를 `/auth/callback`으로 반환한다. Backend에는 Kakao Access Token을 전달하지 않으며, Cognito Access Token만 전달한다.

## AWS/Kakao 설정 절차

1. Kakao Developers에서 앱을 등록하고 Kakao Login 및 OpenID Connect(OIDC)를 활성화한다. 이메일 동의 항목과 검수 상태를 확인한다.
2. Cognito User Pool의 Identity providers에서 Kakao OIDC Provider를 등록한다. Kakao의 issuer/discovery, client ID, client secret, scopes와 attribute mapping을 실제 발급 값으로 입력한다.
3. Kakao Developers의 Redirect URI에는 Cognito 도메인의 다음 URI만 등록한다.

   `https://<cognito-domain>/oauth2/idpresponse`

4. Cognito App Client에서 Kakao IdP와 Cognito User Pool(Managed Login)을 허용한다. SPA Client는 client secret 없이 Authorization Code + PKCE를 사용한다.
5. Kakao의 `email`과 `email_verified`가 Cognito User Pool attributes로 정확히 매핑되는지 확인한다. 이메일 문자열 일치만으로 기존 VORA 계정과 자동 연결하지 않는다.
6. Cognito Managed Login Branding Editor에서 VORA 로고, 색상, 배경, 버튼 스타일을 설정한다. Kakao 버튼은 Kakao 공식 가이드의 노란색 배경(`#FEE500`)과 검정 텍스트 규칙을 유지한다.
7. Cognito App Client callback URL에 VORA 프론트엔드의 `/auth/callback`, sign-out URL에 `/login`을 등록한다. 실제 값은 `frontend/.env`에만 설정하고 저장소에 커밋하지 않는다.

## VORA 환경 계약

- `VITE_COGNITO_DOMAIN`: Managed Login authorize/token endpoint에 사용되는 Cognito domain
- `VITE_COGNITO_ISSUER`: User Pool issuer 및 JWKS issuer 검증에 사용되는 값
- `VITE_COGNITO_CLIENT_ID`: public SPA App Client ID
- `VITE_COGNITO_REDIRECT_URI`: `http://localhost:5173/auth/callback` 또는 배포 URL
- `COGNITO_USERINFO_URL`: `https://<cognito-domain>/oauth2/userInfo`; User Pool issuer URL이 아님

`identity_provider=Kakao`는 프론트엔드가 Cognito authorize 요청에만 전달한다. callback 이후의 code exchange, PKCE verifier, state/nonce 검증과 Access Token 발급은 Cognito/OIDC 라이브러리가 처리한다.

실제 AWS 또는 Kakao 리소스 생성·변경은 이 문서의 범위에 포함하지 않는다.

## Architect 실연동 체크리스트

### Cognito User Pool과 App Client

- Region과 User Pool ID를 기존 환경에서 확인한다. 코드나 보고서에는 실제 식별자와 secret을 기록하지 않는다.
- App Client가 client secret 없는 Public SPA Client인지 확인한다.
- OAuth grant는 Authorization Code만 허용하고 PKCE(S256)를 사용한다.
- Callback URL은 `<frontend-origin>/auth/callback`, logout URL은 `<frontend-origin>/login`으로 정확히 등록한다.
- User Pool Required Attributes에 email이 강제되어 있으면 Kakao 이메일 미동의 사용자를 받을 수 없다. 이번 Backend 계약은 email nullable이므로 실제 User Pool 정책과 맞는지 확인한다.
- 현재 Feature Plan(Lite/Essentials/Plus)과 Managed Login/Branding Editor 사용 가능 범위를 콘솔에서 확인한다.
- Managed Login에는 VORA의 승인된 로고, 배경, 색상 asset을 적용한다. Kakao 공식 버튼 asset은 Kakao Developers에서 내려받은 리소스만 사용한다.
- App Client의 Supported identity providers에 Cognito User Pool과 실제 Kakao Provider 이름을 모두 활성화한다.
- `VITE_COGNITO_NATIVE_PROVIDER`와 `VITE_COGNITO_KAKAO_PROVIDER`가 콘솔의 Provider 이름과 정확히 일치하는지 확인한다.

### Kakao OIDC Provider

- Kakao Login과 OpenID Connect를 활성화하고 REST API Key를 OIDC client ID로 사용한다.
- Client Secret을 활성화한 경우 Cognito OIDC Provider에만 저장하고 Frontend 환경변수나 저장소에 넣지 않는다.
- Redirect URI는 `https://<cognito-domain>/oauth2/idpresponse`로 등록한다.
- scope는 Kakao 문서와 Cognito 콘솔에서 `openid account_email` 호환 여부를 확인한다. 이메일은 사용자 동의와 Kakao 계정 상태에 따라 누락될 수 있다.
- OIDC discovery/issuer, authorization endpoint, token endpoint, userInfo endpoint, JWKS endpoint를 Kakao 공식 discovery 문서와 대조한다.
- Cognito가 token endpoint에서 요구하는 client authentication method가 Kakao의 `client_secret_post`와 호환되는지 실제 콘솔 구성 및 테스트 사용자로 검증한다.
- Cognito attribute mapping에서 OIDC `sub`는 외부 identity의 핵심이며, `email`과 `email_verified`는 선택적 profile data로 매핑한다.
- `email_verified`가 Kakao 응답과 Cognito UserInfo에서 기대한 boolean으로 전달되는지 확인한다. 미검증 email은 VORA에서 저장하지 않는다.

### 실제 연동 검증 순서

1. VORA 계정 로그인이 `identity_provider=COGNITO`로 시작되고 Cognito Access Token을 반환하는지 확인한다.
2. Kakao 로그인이 설정된 Provider 이름으로 시작되고 Cognito `/oauth2/idpresponse`를 거쳐 동일 `/auth/callback`으로 복귀하는지 확인한다.
3. 두 경로의 Access Token `iss`, `client_id`, `token_use=access`, `sub`와 JWKS 서명을 Backend에서 검증한다.
4. Cognito `/oauth2/userInfo`의 `sub`가 Access Token `sub`와 일치하는지 확인한다.
5. 이메일 미동의 Kakao 사용자, 동일 이메일의 서로 다른 Cognito identity, 로그아웃, 만료/갱신, callback 취소를 각각 검증한다.
6. 서로 다른 identity가 다른 `users.id`와 Workflow 소유권을 갖고 타 사용자의 Workflow/Asset/Publication에 접근할 수 없는지 확인한다.

실제 Kakao/Cognito 로그인 성공은 로컬 mock 테스트만으로 확정할 수 없다. 위 검증은 Architect가 승인한 dev 리소스와 테스트 계정에서 수행한다.

## 공식 참고 문서

- Kakao Login 설정: `https://developers.kakao.com/docs/ko/kakaologin/prerequisite`
- Kakao OIDC discovery 및 REST API: `https://developers.kakao.com/docs/en/kakaologin/rest-api`
- Kakao Login 디자인 가이드: `https://developers.kakao.com/docs/ko/kakaologin/design-guide`
- 공식 버튼 리소스: `https://developers.kakao.com/tool/resource/login`
- Frontend asset `kakao-login-complete-ko.svg`는 공식 리소스 페이지의 완성형 한국어 SVG를 변경 없이 저장한 것이다.
