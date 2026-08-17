# Shared client contract

## Base protocol

- Base prefix: `/api/v1`
- JSON: UTF-8, snake_case
- Authenticated request: `Authorization: Bearer <access_token>`
- Preferred content language: `?locale=fr` or `Accept-Language: fr`
- Request tracing: clients may send `X-Request-ID`; the response always returns one.
- Interactive OpenAPI: `/api/v1/docs`
- Machine OpenAPI: `/api/v1/openapi.json`

Error responses have one shape:

```json
{
  "error": {
    "code": "menu_revision_conflict",
    "message": "The menu changed; refresh before continuing.",
    "details": {"current_revision": 3}
  },
  "request_id": "c029..."
}
```

Client code should branch on `error.code`, never a localized human message.

## Authentication storage

- iOS: refresh token in Keychain; Android: Keystore-backed encrypted storage. Keep the access token in
  memory. Refresh only when a 401 is one of the API's bearer/session rejection codes; endpoint-specific
  401s such as `invalid_google_token` must not rotate or delete the current Fofu credentials.
- Web: the API writes the refresh token as HttpOnly cookie and returns only the access token. Send
  credentialed same-origin requests and retain the access token in application memory.
- Registering while authenticated as a guest upgrades the same user so cart/passport state is retained.

## Google authentication

The supported social provider is Google; Kakao authentication is not supported. On iOS, configure
Google Sign-In with these build settings and reference them from the app `Info.plist`:

| Build setting | `Info.plist` use | Value |
|---|---|---|
| `FOFU_API_BASE_URL` | `FofuAPIBaseURL` | Environment API URL ending in `/api/v1`; Release must use an owned HTTPS origin |
| `GOOGLE_SIGN_IN_IOS_CLIENT_ID` | `GIDClientID` | Full iOS application OAuth client ID |
| `GOOGLE_SIGN_IN_SERVER_CLIENT_ID` | `GIDServerClientID` | Full Web application OAuth client ID used as the backend audience |
| `GOOGLE_SIGN_IN_REVERSED_CLIENT_ID` | `CFBundleURLSchemes` | Reversed iOS client ID shown as the iOS URL scheme in Google Cloud |

These client IDs identify OAuth audiences; they are not client secrets. Never bundle a Google OAuth
client secret in the app or add one to this ID-token exchange flow. The existing Google Maps API key
is separate and cannot replace any of these values.

Do not ship a Release build pointed at a development tunnel. The checked-in Release API value is an
invalid placeholder by design and must be replaced through a protected environment-specific build
configuration before distribution.

After an interactive Google sign-in, send the returned ID token directly to the API:

```http
POST /api/v1/auth/google
Content-Type: application/json

{
  "id_token": "<short-lived Google ID token>",
  "replaced_refresh_token": "<optional previous full-session refresh token>",
  "locale": "en",
  "client_type": "ios"
}
```

The endpoint returns the normal `AuthResponse`; native clients install its Fofu access/refresh tokens
using the same in-memory/Keychain rules as password login. Do not persist or log the Google ID token.
The server verifies the token and uses the Google `sub`, not an unverified client-supplied email, as the
provider identity.

When Google sign-in starts from a QR-scoped session that temporarily replaced a native full session,
send that previous session's refresh token as `replaced_refresh_token`. With an authenticated current
principal, the API revokes both the current session and that same-user backup in the login transaction;
an unknown, already-revoked, or different-user token is a no-op. Omit the field when no backup exists,
and treat it as a secret exactly like every other refresh token.

Send the current Fofu bearer token when a first-time Google identity should upgrade an authenticated
guest. This keeps that guest's passport/cart state and revokes every session issued before the guest
became a member. Every session is also bound to the user's guest/member state at issue time, so a
late or previously issued guest refresh token cannot become a member credential after an upgrade.
The migration conservatively marks historical `guest` and `qr_guest` sessions as guest-issued;
registered users with an older QR session may need to scan the QR code again after deployment.
An existing Google identity instead signs in to its already linked Fofu user; guest state is not merged across two
existing users. A matching email that has not been securely linked is a conflict, not an automatic
account merge. To link it, first authenticate as that same full Fofu user and then call this endpoint
with its bearer token. The iOS Settings screen exposes this authenticated flow as **Connect Google
account**. One Fofu account may link one Google subject. A password-backed account keeps its original
password-login email even if the linked Google profile uses or later changes to another email; the
latest provider email remains on the provider identity record.

The backend must set `FOFU_GOOGLE_OAUTH_CLIENT_IDS` to a JSON array containing the exact audience used
by `GIDServerClientID`. An empty array leaves Google login disabled. See Google's
[iOS integration guide](https://developers.google.com/identity/sign-in/ios/start-integrating) and
[backend authentication guide](https://developers.google.com/identity/sign-in/ios/backend-auth).

## Catalog and compatibility

The catalog remains public. When a user token/passport is present, item responses can add personalized
compatibility. Treat the status values as follows:

- `conflict`: at least one declared `contains`/relevant claim contradicts the passport.
- `compatible`: available merchant evidence has no known conflict, but the disclaimer still applies.
- `unknown`: evidence is missing, only `may_contain` is known, or no passport exists.

Never replace these meanings with a green "safe" assertion. Always expose evidence and let a diner ask
the restaurant, especially for allergies and cross-contact.

## Cart consistency

Clients send menu item IDs and quantities, not prices or totals. The server snapshots current prices,
checks availability and restaurant/menu revision, and generates Korean/localized order-card text.
On HTTP 409, refetch bootstrap/menu/cart and show the concrete changed item to the user.

Use a new `idempotency_key` for each prepare action. Retrying the same request with the same key returns
the exact stored order-card response; reusing that key for a different cart/version/locale returns 409.

## iOS push notifications

Request notification authorization from a user-facing app context, then convert the APNs token bytes
to lowercase hexadecimal without assuming a fixed byte length. Persist a random installation UUID in
Keychain and use it as the path identifier. Debug builds send `sandbox`; TestFlight and App Store builds
send `production`.

Only a registered `full` iOS session may create or refresh a binding. Do not call PUT while the foreground
Bearer is `guest` or `qr_guest`; a member entering QR mode keeps the device's existing full-session binding
and syncs again after restoring the full credential.

```http
PUT /api/v1/push/devices/5B3EC8CB-3DC7-49D7-AF4C-C17AEEEC98A1
Authorization: Bearer <full iOS access token>
Content-Type: application/json

{
  "token": "<even-length APNs token hex>",
  "environment": "sandbox",
  "locale": "ko-KR"
}
```

The token is variable-length: the API accepts an even number of hexadecimal characters from 2 through
512 and never returns or logs it. A successful idempotent PUT returns:

```json
{
  "installation_id": "5B3EC8CB-3DC7-49D7-AF4C-C17AEEEC98A1",
  "platform": "ios",
  "environment": "sandbox",
  "topic": "im.fofu.fofu",
  "locale": "ko-KR",
  "is_active": true,
  "last_registered_at": "2026-08-14T12:00:00Z"
}
```

Call bodyless `DELETE /api/v1/push/devices/{installation_id}` before logout when possible; success and
already-absent both return 204. Keep a pending deletion tombstone across offline logout. If credentials
have already changed, the ownership-scoped DELETE can be a no-op, but the next full-session PUT atomically
rebinds that installation/token and cancels old-user pending deliveries. Local notification opt-out must
not depend on DELETE succeeding.

Message alerts intentionally contain only generic localized text. Custom keys are
`fofu_type: "message"`, `conversation_id`, and `message_id`. Route to the conversation after unlocking,
restore/refresh authentication, and fetch the current message from the API. Never expect message content
or sender display name in the notification payload.

## Generated clients

Run the API, then export its schema:

```bash
curl -sS http://127.0.0.1:8000/api/v1/openapi.json -o openapi.json
```

Use that one schema to generate Swift, Kotlin, and TypeScript clients. Keep generated code out of the
domain/view layer and wrap it in repositories so a future transport change does not leak into UI state.
