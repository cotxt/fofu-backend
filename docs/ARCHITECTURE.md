# Backend architecture

## Why a modular monolith

Fofu is still one product domain, while iOS, Android, and the QR web client need the same rules.
A versioned modular monolith keeps one transactional source of truth without creating premature
network boundaries. FastAPI routers are transport adapters, services own use-case rules, and
SQLAlchemy models own persistence constraints.

```text
iOS / Android ── Bearer access + rotating refresh ─┐
                                                   ├── /api/v1
QR web ── HTTPS /q + scoped guest session ─────────┘
                         │
                 FastAPI routers
                         │
        auth / catalog / cart / profile / messages / push / owner
                         │
                SQLAlchemy transaction
                         │
           SQLite (local) / PostgreSQL (deployed)
```

Google Sign-In is an additional authentication adapter: clients exchange a Google ID token at
`/api/v1/auth/google`, then use the same Fofu bearer/refresh session shown above.

## Module boundaries

- `api/`: HTTP validation, response models, status codes, auth dependencies.
- `services/`: catalog filtering/localization, passport compatibility, token rotation, cart price
  validation, messaging authorization, APNs outbox delivery, owner membership checks, and audit writes.
- `google_identity.py`: Google certificate/claim verification adapter; returns only trusted identity
  claims to the auth service.
- `schemas/`: strict Pydantic contracts. UI colors, SF Symbols, and formatted-only prices are not
  persisted domain values.
- `models.py`: relational constraints and cross-client state.
- `migrations/`: Alembic is authoritative in staging/production. `create_all` exists only for local
  bootstrap and tests.
- `seed.py`: idempotent demo data derived from the SwiftUI prototype.

## Core relational groups

- Identity: `users`, provider-scoped `auth_identities`, `auth_sessions`, `food_passports`.
- Catalog: `restaurants`, translations, opening hours, categories, items, ingredients, allergens,
  dietary claims, reviews, explore videos.
- User state: saved restaurants, carts/items, prepared order cards/history.
- Messaging: conversations, participants, messages, APNs device bindings, durable push deliveries.
- Merchant: memberships, private media assets, owner applications, audit events.
- Install-free entry: hashed QR access points and privacy-minimized scan events.

## Multi-client invariants

- Public identifiers are opaque strings; database row order is never an API contract.
- Times are UTC RFC 3339; restaurant hours carry `Asia/Seoul` separately.
- Money uses integer amount plus ISO 4217 currency. KRW therefore uses values such as `16000`, not
  the display string `"₩16,000"`.
- Locale is BCP-47. Missing translations fall back to English while the original Korean name remains
  available where useful.
- Pagination cursors are opaque to clients.
- Client-computed totals, compatibility labels, owner status, and menu availability are never trusted.
- Mutating a cart against an old menu revision or unavailable item returns a conflict for the client
  to refresh.

## Scaling seams

The checked-in implementation runs without external services, but the contracts do not depend on
that choice.

| Current adapter | Production-scale replacement |
|---|---|
| SQLite | PostgreSQL; add PostGIS index/query when venue count requires it |
| Python/Haversine filtering | PostGIS `ST_DWithin` with the same response contract |
| Local private upload directory | S3-compatible private bucket + presigned upload/finalize |
| Single-process QR/Google-auth limiter | API gateway or Redis sliding-window limiter |
| Database-backed message polling | Redis/NATS fan-out plus WebSocket/SSE workers |
| PostgreSQL push outbox + per-API lease worker | Dedicated outbox workers with the same lease/event contract |
| Simple text matching | PostgreSQL FTS or a search service behind the catalog service |
| In-process demo seed | Controlled admin/import pipeline |

The database and OpenAPI contract can stay stable while these adapters change.

## Security boundaries

- QR codes are high-entropy public locators, not credentials. Only their SHA-256 digest is stored.
- Web refresh credentials use HttpOnly, SameSite cookies; native refresh credentials belong in
  Keychain/Keystore. Access tokens are short-lived and refresh tokens rotate.
- Each authentication session records whether its user was a guest when the session was issued.
  Access and refresh both reject a mismatch with the token claims or current user state, while guest
  upgrades atomically claim the user row so concurrent Google/password upgrades cannot both win.
- Google ID tokens are short-lived exchange credentials. The API verifies their signature, issuer,
  expiry, verified email, and exact configured audience, then keys the identity by `(provider, sub)`;
  it never stores the raw token or auto-links an unrelated account by email. Google certificate
  responses are cached behind a bounded-time transport, and the public exchange is locally
  rate-limited. Kakao is not supported.
- A QR guest session is scoped to the resolved restaurant/table context and cannot grant merchant
  rights, payment authority, or a shared table order.
- Push registration requires a registered full iOS session and is capped per user. Device tokens and
  message contents are excluded from responses/logs; the durable payload contains only generic alert
  text and opaque routing IDs. Logout/session revocation deactivates the associated binding.
- Merchant authorization checks an active membership for every restaurant mutation.
- Business documents are private, size-limited, MIME- and magic-signature checked, hashed, and never
  served from a public static directory.
- Allergen results distinguish `contains`, `may_contain`, and uncertain/missing evidence. The response
  includes a restaurant-confirmation disclaimer.
- Exact credentialed CORS origins are required; staging/production validation rejects wildcard CORS,
  a default JWT secret, and automatic schema creation.
