# Install-free web and QR flow

## Printed QR payload

Use only a stable HTTPS URL:

```text
https://api.fofu.example/q/<opaque-random-code>
```

Do not print a custom app scheme, JWT, user ID, restaurant database ID, table ID, or personal data.
The same URL may be configured as an iOS Universal Link and Android App Link later, but a browser
fallback must always remain available.

## Request sequence

```text
phone camera
  -> GET /q/{code}
  -> 307 responsive web route /r/{restaurant-slug}#qr={code}
  -> POST /api/v1/guest-sessions/qr {code, locale, client_type:"web"}
  <- short access token + HttpOnly rotating refresh cookie + restaurant/table bootstrap
  -> GET /api/v1/sessions/current/bootstrap
  -> cart changes and server-generated Korean order card
```

The fragment is not sent to the web server or in the HTTP Referer. The web client reads it, exchanges
the code, and immediately calls `history.replaceState` to remove it from the long-lived browser
location. If the web frontend uses its own camera (for example, scanning a code shown on another
screen), it exchanges the scanned code through the same guest-session endpoint. Camera capture belongs
in the HTTPS web frontend; uploading camera frames to this backend is not necessary for QR decoding.

## Session rules

- Every scan receives an independent guest user/session/cart. A table QR does not silently merge carts
  between strangers.
- The optional `table_label` is context for an order card, not proof that a person is physically at the
  table.
- Static printed codes can be revoked or replaced. External responses intentionally do not reveal
  whether an invalid code once existed.
- A QR session can read public menus, persist its own preferences/cart, and prepare a staff-facing order
  card. It cannot send a kitchen order, pay, approve a merchant, or modify a restaurant.
- Logging stores a secret-keyed IP digest rather than raw IP and never stores the raw QR secret.

## Demo flow

The idempotent local seed installs one intentionally public development code:

```text
halmoni-table-demo
```

It exists only to make local and automated testing reproducible. Production QR creation returns a new
random code once; only its digest remains in the database.

Set both URLs before printing a production code:

```dotenv
FOFU_PUBLIC_API_BASE_URL=https://api.fofu.example
FOFU_WEB_APP_BASE_URL=https://fofu.example
```

## Web security checklist

- Serve both API and web only over HTTPS and set HSTS at the ingress.
- Prefer a same-origin `/api` reverse proxy; otherwise allow exactly the deployed web origin with
  credentials.
- Keep refresh cookies `Secure; HttpOnly; SameSite=Lax`; keep access tokens in memory rather than
  localStorage or a URL.
- Keep `Referrer-Policy: no-referrer` on QR/auth responses.
- Apply a shared rate limiter at the gateway and alert on scan/session anomalies.
- Redact `/q/*` and `/api/v1/qr/*` path segments in CDN/load-balancer access logs. The application
  installs the same redaction for Uvicorn, but an upstream proxy sees the request first.
- Rotate/revoke a code when a printed sign is lost or copied maliciously.
- Add a separate onsite proof before any future POS submission, shared cart, or payment action.
