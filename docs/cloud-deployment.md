# Screenscribe Cloud: deployment guide

Screenscribe Cloud is the hosted edition of screenscribe. A customer uploads a
narrated screen recording from the web portal, the CLI or the macOS app. The
server runs the full STT -> LLM -> VLM review pipeline and publishes an
interactive review at a shareable URL, together with machine-readable
`report.json` and an agent manifest that coding agents act on directly.

Two product rules shape the whole deployment:

- **Hard subscription.** Every processed minute belongs to an active plan.
  Plans are sold through Stripe (web) and the App Store (macOS). No plan, no
  processing.
- **ZERO BYOK.** Customers never enter, paste or hold an AI provider key. The
  pipeline runs on the server's own AI capacity, configured once by the
  operator through `SCREENSCRIBE_API_KEY` (or `LIBRAXIS_API_KEY`).

This guide is for the people who run the service. It covers the topology, a
single-host quickstart, every configuration variable, billing and identity
integrations, the security model, backups, upgrades and troubleshooting.

## 1. Topology

```text
                              Internet
          HTTPS 443/tcp, HTTP/3 443/udp, HTTP 80 (ACME + redirect)
                                 |
                      +----------v-----------+
                      |        caddy         |  TLS, HSTS, body cap,
                      |       (caddy:2)      |  SSE unbuffered,
                      +----------+-----------+  video passed byte-exact
                                 |
                                 |  http://web:8080  (private compose network)
                                 |
                      +----------v-----------+        +----------------------+
                      |         web          |        |        worker        |
                      |  screenscribe.cloud  |        |  screenscribe.cloud  |
                      |        serve         |        |        worker        |
                      |  API, portal, /r/*   |        |  ffmpeg + AI calls   |
                      +----------+-----------+        +----------+-----------+
                                 |                               |
                                 +---------------+---------------+
                                                 |
                                 +---------------v----------------+
                                 |   volume: screenscribe_data    |
                                 |   /var/lib/screenscribe/data   |
                                 |     cloud.sqlite3 (WAL)        |
                                 |     uploads/  reviews/         |
                                 |     glossary/                  |
                                 +---------------^----------------+
                                                 |
                                 init-db: one-shot schema migration,
                                 runs before web and worker start

   worker --HTTPS--> AI provider (server-owned key only)
   web    --HTTPS--> Stripe API, Apple identity keys
```

| File | Role |
|---|---|
| `deploy/Dockerfile` | One multi-stage image for every role. Python 3.11 slim, ffmpeg, tini, non-root uid 10001. |
| `deploy/docker-compose.prod.yml` | The production stack: `caddy`, `web`, `worker`, `init-db`, three named volumes. |
| `deploy/Caddyfile` | Edge configuration: TLS, headers, compression policy, proxy timeouts. |
| `.env.cloud` (repository root) | Server secrets and settings. Gitignored by the `.env.*` rule, never committed. |

| Service | Command | Network exposure | Purpose |
|---|---|---|---|
| `caddy` | Caddy default | Host ports 80, 443/tcp, 443/udp | Only public entry point. |
| `web` | `serve` | `expose: 8080` on the compose network only | API, portal pages (EN + PL), hosted reviews, SSE. |
| `worker` | `worker` | None | Polls SQLite for queued jobs, runs ffmpeg/ffprobe and the AI pipeline, writes reports. |
| `init-db` | `init-db` | None | Creates or migrates the schema, then exits 0. Idempotent. |

`web` and `worker` share one volume because the worker writes the review
files that `web` serves. SQLite in WAL mode lets `web` read while the worker
writes. The stack therefore runs on a single host. Scale it vertically: give
the host more CPU and raise `SCREENSCRIBE_CLOUD_WORKER_CONCURRENCY`.

## 2. Requirements

- A Linux host (x86_64 or arm64) with Docker Engine 24+ and the Compose v2
  plugin (`docker compose`).
- A dedicated public hostname, for example `cloud.example.com`. HSTS is sent
  with `includeSubDomains`, so use a hostname whose subdomains are all HTTPS.
- Inbound TCP 80 and 443, plus UDP 443 for HTTP/3. Port 80 stays open: Caddy
  answers ACME HTTP-01 challenges there and redirects everything else to HTTPS.
- Outbound HTTPS to your AI provider, `api.stripe.com`, `appleid.apple.com` and
  the ACME certificate authority.
- Baseline 2 vCPU and 4 GB RAM. Frame extraction with ffmpeg is the main local
  CPU cost; transcription and analysis run on remote AI capacity.
- SSD storage sized for uploads plus published reviews. A single upload may
  reach 2 GiB.
- An NTP-synchronised clock. Stripe signatures carry a 300 second tolerance and
  every token has an expiry.

## 3. Quickstart

All commands run from the repository root.

### 3.1 DNS

Point an `A` record (and an `AAAA` record if the host has IPv6) for your
hostname at the server, then confirm it resolves:

```sh
dig +short cloud.example.com
```

Caddy requests the certificate automatically once DNS resolves to the host and
ports 80/443 are reachable.

### 3.2 Create `.env.cloud`

`.env.cloud` holds every server-side secret. It lives in the repository root,
is matched by the `.env.*` rule in `.gitignore`, and is never committed.

```sh
install -m 600 /dev/null .env.cloud   # owner-only from the first byte
cat > .env.cloud <<'EOF'
# Edge
SCREENSCRIBE_DOMAIN=cloud.example.com
ACME_EMAIL=ops@example.com

# AI capacity (ZERO BYOK): the server's own key, never a customer key
SCREENSCRIBE_API_KEY=<libraxis-api-key>

# Stripe (web billing)
SCREENSCRIBE_CLOUD_STRIPE_SECRET_KEY=<stripe-secret>
SCREENSCRIBE_CLOUD_STRIPE_WEBHOOK_SECRET=<stripe-webhook-signing-secret>
SCREENSCRIBE_CLOUD_STRIPE_PRICE_STARTER=<stripe-price-id-starter>
SCREENSCRIBE_CLOUD_STRIPE_PRICE_PRO=<stripe-price-id-pro>
SCREENSCRIBE_CLOUD_STRIPE_PRICE_TEAM=<stripe-price-id-team>

# Apple (macOS app: Sign in with Apple + StoreKit 2)
SCREENSCRIBE_CLOUD_APPLE_BUNDLE_ID=com.example.screenscribe
SCREENSCRIBE_CLOUD_APPLE_CLIENT_IDS=com.example.screenscribe
SCREENSCRIBE_CLOUD_APPSTORE_PRODUCTS=com.example.starter=starter,com.example.pro=pro,com.example.team=team
EOF
```

Configure at least one billing rail (Stripe, App Store, or both). Every job
needs an active plan.

### 3.3 Launch

```sh
docker compose --env-file .env.cloud -f deploy/docker-compose.prod.yml up -d --build
```

The two env inputs do different jobs:

- `--env-file .env.cloud` feeds compose interpolation: `${SCREENSCRIBE_DOMAIN}`
  and `${ACME_EMAIL}` inside the compose file.
- `env_file: ../.env.cloud` inside the compose file feeds the `web`, `worker`
  and `init-db` containers.

The alternative is to export `SCREENSCRIBE_DOMAIN` (and optionally `ACME_EMAIL`)
in the shell and run
`docker compose -f deploy/docker-compose.prod.yml up -d --build`.

Compose refuses to start when `SCREENSCRIBE_DOMAIN` is missing. When
`ACME_EMAIL` is unset it falls back to `ops@<domain>`. Set a monitored mailbox
instead.

Start order is enforced by the compose file:

1. `init-db` migrates the schema and exits 0.
2. `web` and `worker` start (both wait for `init-db` to complete successfully).
3. `caddy` starts once `web` reports healthy.

The rest of this guide uses a shorthand:

```sh
alias dc='docker compose --env-file .env.cloud -f deploy/docker-compose.prod.yml'
```

### 3.4 Verify

```sh
dc ps -a
curl -fsS https://cloud.example.com/healthz   # {"status":"ok"}
curl -fsS https://cloud.example.com/readyz    # 200 when DB and storage are usable
```

`dc ps -a` shows `init-db` as exited (0), and `web`, `worker` and `caddy` as
running. `web` additionally shows `healthy`. Caddy starts after the first
passing health probe on `web` (10 second interval), and the first certificate
issuance can take a few more seconds after that. Run the `curl` checks once
`caddy` is up.

### 3.5 First account

Open `https://cloud.example.com/login`, register an account, and subscribe at
`/billing`. Upload a recording at `/upload`, or connect the CLI through the
device flow (section 8).

## 4. Configuration reference

### 4.1 Values pinned by the compose file

These values are set in `environment:` and override anything in `.env.cloud`:

| Service | Variable | Value | Why |
|---|---|---|---|
| `web` | `SCREENSCRIBE_CLOUD_HOST` | all interfaces | The application defaults to loopback. The widening applies inside the container network only; the port is never published on the host. |
| `web` | `SCREENSCRIBE_CLOUD_PORT` | `8080` | Caddy's upstream and the image healthcheck expect 8080. |
| `web`, `worker` | `SCREENSCRIBE_CLOUD_BASE_URL` | `https://${SCREENSCRIBE_DOMAIN}` | One public origin for links, cookies and Origin checks. |
| all app services | `SCREENSCRIBE_CLOUD_STORAGE_DIR` | `/var/lib/screenscribe/data` | Must be the shared volume. |
| `web` | `SCREENSCRIBE_CLOUD_EMBEDDED_WORKER` | `0` | The dedicated `worker` service owns the pipeline. |
| `web` | `FORWARDED_ALLOW_IPS` | `*` | uvicorn trusts `X-Forwarded-*` only from listed peers. `web` is reachable solely on the compose network, so Caddy is the only peer. |

### 4.2 Deployment variables

| Variable | Default | Consumer | Purpose |
|---|---|---|---|
| `SCREENSCRIBE_DOMAIN` | required | compose, Caddy | Public hostname without scheme. Becomes the Caddy site address and the base URL. |
| `ACME_EMAIL` | `ops@<domain>` | Caddy | ACME account contact for certificate notices and account recovery. |
| `SCREENSCRIBE_IMAGE` | `screenscribe-cloud:local` | compose | Image name and tag that compose builds and runs. |

### 4.3 Application variables

The full set read by `CloudSettings.from_env` in `screenscribe/cloud/config.py`.

Parsing rules apply to every variable:

- Flags are true for `1`, `true`, `yes` or `on` (any case). Anything else, or
  empty, is false.
- An unparsable number falls back to the default. A number below the stated
  minimum is clamped to that minimum.
- Lists are comma-separated, and blank entries are ignored.
- Maps use `key=value,key2=value2`.

**Network, origin and portal**

| Variable | Default | Purpose |
|---|---|---|
| `SCREENSCRIBE_CLOUD_BASE_URL` | `http://127.0.0.1:8080` | Public origin. Builds every absolute URL the server hands out (hosted reviews, device-flow verification, billing returns) and anchors the Origin allow-list. An `https://` value turns on `Secure` cookies. |
| `SCREENSCRIBE_CLOUD_HOST` | `127.0.0.1` | Bind address for `serve`. Loopback by default; compose widens it on `web` only. |
| `SCREENSCRIBE_CLOUD_PORT` | `8080` (min 1) | Bind port for `serve`. |
| `SCREENSCRIBE_CLOUD_CORS_ORIGINS` | empty | Comma-separated explicit origins allowed to call the API cross-origin. Their hosts also join the Origin allow-list. Empty means same-origin only. There is no wildcard mode. |
| `SCREENSCRIBE_CLOUD_TRUSTED_HOSTS` | empty | Comma-separated extra `host[:port]` values accepted in `Origin`/`Referer` on cookie-authenticated writes, for example a `www.` alias. |
| `SCREENSCRIBE_CLOUD_COOKIE_SECURE` | `false` | Forces the `Secure` cookie flag. Already automatic whenever the base URL is `https://`. |
| `SCREENSCRIBE_CLOUD_DEFAULT_LANG` | `en` | Default portal language. `pl` selects Polish; any other value means English. |

**Storage**

| Variable | Default | Purpose |
|---|---|---|
| `SCREENSCRIBE_CLOUD_STORAGE_DIR` | `.screenscribe-cloud` (relative to the working directory). The image sets `/var/lib/screenscribe/data`. | Root of all state: the database, `uploads/`, `reviews/` and `glossary/`. `web` and `worker` must see the same directory. |
| `SCREENSCRIBE_CLOUD_DATABASE` | `<storage dir>/cloud.sqlite3` | SQLite database path override. Keep it on the shared volume. |

**AI capacity (ZERO BYOK)**

| Variable | Default | Purpose |
|---|---|---|
| `SCREENSCRIBE_API_KEY` | empty | Server-owned AI key used by the hosted pipeline. Takes precedence over `LIBRAXIS_API_KEY`. Without a key the server has no AI capacity and cannot process jobs. |
| `LIBRAXIS_API_KEY` | empty | Used when `SCREENSCRIBE_API_KEY` is empty. |
| `SCREENSCRIBE_CLOUD_API_BASE` | unset | One OpenAI-compatible base URL for the hosted pipeline. Unset keeps the built-in LibraxisAI endpoints. |
| `SCREENSCRIBE_CLOUD_STT_ENDPOINT` | unset | Explicit speech-to-text endpoint. |
| `SCREENSCRIBE_CLOUD_LLM_ENDPOINT` | unset | Explicit LLM endpoint (Responses API). |
| `SCREENSCRIBE_CLOUD_VISION_ENDPOINT` | unset | Explicit vision endpoint (Responses API). |
| `SCREENSCRIBE_CLOUD_LLM_MODEL` | unset | LLM model override. |
| `SCREENSCRIBE_CLOUD_VISION_MODEL` | unset | Vision model override. |

**Limits**

| Variable | Default | Purpose |
|---|---|---|
| `SCREENSCRIBE_CLOUD_MAX_UPLOAD_BYTES` | `2147483648` (2 GiB, min 1) | Largest job upload, enforced while the multipart stream is read. Keep Caddy's `request_body max_size` slightly above this value. |
| `SCREENSCRIBE_CLOUD_MAX_BUNDLE_BYTES` | `536870912` (512 MiB, min 1) | Largest review bundle accepted by `/api/v1/reviews/publish`. |
| `SCREENSCRIBE_CLOUD_MAX_ARCHIVE_MEMBERS` | `500` (min 1) | Most entries allowed in a published archive. |
| `SCREENSCRIBE_CLOUD_MAX_ARCHIVE_TOTAL_BYTES` | `1073741824` (1 GiB, min 1) | Largest total uncompressed archive size (zip-bomb guard). |

**Sessions and authentication**

| Variable | Default | Purpose |
|---|---|---|
| `SCREENSCRIBE_CLOUD_WEB_SESSION_TTL_SECONDS` | `604800` (7 days, min 60) | Lifetime of the portal session cookie `ss_session`. |
| `SCREENSCRIBE_CLOUD_REFRESH_TTL_SECONDS` | `2592000` (30 days, min 60) | Lifetime of a CLI/macOS refresh token. |
| `SCREENSCRIBE_CLOUD_DEVICE_CODE_TTL_SECONDS` | `600` (min 30) | Validity window of a device-flow code. |
| `SCREENSCRIBE_CLOUD_DEVICE_POLL_INTERVAL` | `5` | Seconds a CLI waits between device-token polls. |

**Stripe**

| Variable | Default | Purpose |
|---|---|---|
| `SCREENSCRIBE_CLOUD_STRIPE_SECRET_KEY` | empty | Stripe secret API key. Setting it enables Stripe billing. |
| `STRIPE_SECRET_KEY` | empty | Used when the namespaced variable above is empty. |
| `SCREENSCRIBE_CLOUD_STRIPE_WEBHOOK_SECRET` | empty | Signing secret of the webhook endpoint. |
| `STRIPE_WEBHOOK_SECRET` | empty | Used when the namespaced variable above is empty. |
| `SCREENSCRIBE_CLOUD_STRIPE_PRICE_STARTER` | empty | Stripe price id sold as the `starter` plan. |
| `SCREENSCRIBE_CLOUD_STRIPE_PRICE_PRO` | empty | Stripe price id sold as the `pro` plan. |
| `SCREENSCRIBE_CLOUD_STRIPE_PRICE_TEAM` | empty | Stripe price id sold as the `team` plan. |
| `SCREENSCRIBE_CLOUD_STRIPE_API_BASE` | `https://api.stripe.com` | Stripe API origin. Point it at stripe-mock in test environments. |

**Apple**

| Variable | Default | Purpose |
|---|---|---|
| `SCREENSCRIBE_CLOUD_APPLE_BUNDLE_ID` | empty | macOS app bundle id. Required `bundleId` on StoreKit transactions, and the Sign in with Apple audience when no client ids are set. |
| `SCREENSCRIBE_CLOUD_APPLE_CLIENT_IDS` | empty (falls back to the bundle id) | Comma-separated `aud` values accepted on Sign in with Apple identity tokens. |
| `SCREENSCRIBE_CLOUD_APPLE_JWKS_URL` | `https://appleid.apple.com/auth/keys` | Apple's public keys for identity-token signatures. |
| `SCREENSCRIBE_CLOUD_APPLE_ROOT_SHA256` | empty (built-in Apple Root CA G3 pin) | Comma-separated SHA-256 fingerprints that replace the pinned root for StoreKit 2 chain validation. |
| `SCREENSCRIBE_CLOUD_APPSTORE_PRODUCTS` | empty | App Store product id to plan map, for example `com.example.pro=pro,com.example.team=team`. |
| `SCREENSCRIBE_CLOUD_APPSTORE_ALLOW_XCODE` | `false` | Accepts Xcode-environment transactions (local StoreKit testing). Never enable it in production. |

**Plans, worker and live progress**

| Variable | Default | Purpose |
|---|---|---|
| `SCREENSCRIBE_CLOUD_PLAN_MINUTES` | `starter=60,pro=300,team=1000` | Minutes included per plan. Entries merge over the defaults, so `pro=450` changes only `pro`. |
| `SCREENSCRIBE_CLOUD_MIN_JOB_MINUTES` | `0.25` | Floor on the minutes a single job reserves. |
| `SCREENSCRIBE_CLOUD_WORKER_CONCURRENCY` | `1` (min 1) | Jobs one worker processes in parallel. |
| `SCREENSCRIBE_CLOUD_WORKER_POLL_SECONDS` | `2.0` (min 0.05) | Queue poll interval of the worker. |
| `SCREENSCRIBE_CLOUD_EMBEDDED_WORKER` | `false` | Runs the worker loop inside `serve`, for single-process local dev. Compose pins `0` on `web`. |
| `SCREENSCRIBE_CLOUD_SSE_POLL_SECONDS` | `1.0` (min 0.05) | How often an open job event stream checks job state. |
| `SCREENSCRIBE_CLOUD_SSE_KEEPALIVE_SECONDS` | `10.0` (min 0.5) | Keepalive interval on idle event streams. Keep it far below every proxy idle timeout. |

## 5. Stripe

Stripe is the web billing rail: Checkout for new subscriptions and the Stripe
Customer Portal for plan changes, payment methods and cancellation.

1. **Products and prices.** In the Stripe Dashboard, create one product per
   plan (Starter, Pro, Team), each with a recurring price. Copy the price ids
   into `SCREENSCRIBE_CLOUD_STRIPE_PRICE_STARTER`, `..._PRO` and `..._TEAM`. The
   server maps an incoming price id back to its plan, so every price id must be
   unique to one plan.
2. **API key.** Put the secret key in `SCREENSCRIBE_CLOUD_STRIPE_SECRET_KEY`.
   Use a restricted key scoped to Checkout, Customer Portal, Customers and
   Subscriptions if your Stripe account policy requires it.
3. **Customer Portal.** Enable it under Settings -> Billing -> Customer portal.
   `/api/v1/billing/stripe/portal` opens a portal session for the signed-in
   customer.
4. **Webhook endpoint.** Add the endpoint
   `https://<your-domain>/api/v1/billing/stripe/webhook` and subscribe it to the
   five events the server handles:
   - `checkout.session.completed`
   - `customer.subscription.created`
   - `customer.subscription.updated`
   - `customer.subscription.deleted`
   - `invoice.payment_failed` (marks the subscription `past_due` and pauses
     processing until Stripe reports a recovered payment)

   Copy the endpoint's signing secret into
   `SCREENSCRIBE_CLOUD_STRIPE_WEBHOOK_SECRET`.
5. **Signature verification.** Every delivery is verified against the
   `Stripe-Signature` header (HMAC-SHA256 over the raw body) with a 300 second
   timestamp tolerance. Keep the host clock NTP-synchronised. Caddy passes the
   body through untouched and the endpoint needs no extra authentication.
6. **Idempotency.** Event ids are recorded. Stripe retries and replays are
   acknowledged without applying the same event twice.

Test mode uses the same flow with test keys, a test-mode webhook endpoint and
its own signing secret. Never mix test and live values in one `.env.cloud`.

## 6. App Store (StoreKit 2)

The macOS app sells the same plans through auto-renewable subscriptions and
syncs each signed transaction to `/api/v1/billing/appstore/sync`.

1. **Bundle id.** Set `SCREENSCRIBE_CLOUD_APPLE_BUNDLE_ID` to the app's bundle
   id. Transactions for any other bundle are rejected.
2. **Products.** In App Store Connect, create one auto-renewable subscription
   per plan, then map product ids to plans:

   ```dotenv
   SCREENSCRIBE_CLOUD_APPSTORE_PRODUCTS=com.example.starter=starter,com.example.pro=pro,com.example.team=team
   ```

   A product id missing from the map grants nothing.
3. **Chain of trust.** StoreKit 2 transactions are JWS documents with the
   signing chain in the `x5c` header. The server verifies the chain up to
   **Apple Root CA G3**, which is pinned by its SHA-256 fingerprint, and then
   verifies the JWS signature with the leaf key.
4. **Root override.** `SCREENSCRIBE_CLOUD_APPLE_ROOT_SHA256` replaces the
   built-in pin. It takes comma-separated hex SHA-256 fingerprints of DER
   certificates and is meant for an Apple root rotation. Compute the value from
   the certificate Apple publishes:

   ```sh
   curl -fsSLO https://www.apple.com/certificateauthority/AppleRootCA-G3.cer
   openssl x509 -inform der -in AppleRootCA-G3.cer -noout -fingerprint -sha256 \
     | cut -d= -f2 | tr -d ':' | tr 'A-F' 'a-f'
   ```

5. **Xcode environment.** Transactions from local StoreKit configuration files
   (environment `Xcode`) are refused unless
   `SCREENSCRIBE_CLOUD_APPSTORE_ALLOW_XCODE=1`. That switch exists for local
   development only.

## 7. Sign in with Apple

The macOS app (and any web flow you add) signs users in with
`POST /api/v1/auth/apple`, which carries an Apple identity token. The server
verifies the token signature against Apple's keys at
`SCREENSCRIBE_CLOUD_APPLE_JWKS_URL` and checks that the token's `aud` is an
accepted audience.

- Native app tokens carry the app's bundle id as `aud`.
- Web tokens carry a Services ID as `aud`.
- `SCREENSCRIBE_CLOUD_APPLE_CLIENT_IDS` lists every accepted audience, for
  example `com.example.screenscribe,com.example.screenscribe.web`. When it is
  empty, the bundle id is the only audience.

## 8. Device flow for the CLI

The CLI signs in without ever handling a password. It uses the device
authorization flow:

1. The CLI calls `POST /api/v1/auth/device/code` and receives a device code, a
   short user code, a poll interval (`SCREENSCRIBE_CLOUD_DEVICE_POLL_INTERVAL`)
   and an expiry (`SCREENSCRIBE_CLOUD_DEVICE_CODE_TTL_SECONDS`).
2. The user opens `https://<your-domain>/activate`, signs in if needed, and
   confirms the code. The link `https://<your-domain>/activate?user_code=<code>`
   pre-fills the code.
3. The CLI polls `POST /api/v1/auth/device/token` at the given interval. Once
   the user approves, it receives an access token and a refresh token.
4. The CLI then authenticates with `Authorization: Bearer <access-token>` and
   renews through `POST /api/v1/auth/token/refresh`.

The macOS app uses the same Bearer token model after Sign in with Apple.

## 9. Quota model

- Each plan includes a number of processing minutes:
  `starter` 60, `pro` 300 and `team` 1000 by default. Change them with
  `SCREENSCRIBE_CLOUD_PLAN_MINUTES`.
- Unreadable or corrupt media is rejected with **422** at upload time, before
  anything is reserved.
- When a job is accepted, its minutes are **reserved** atomically against the
  account. A job reserves its media duration (ffprobe), and never less than
  `SCREENSCRIBE_CLOUD_MIN_JOB_MINUTES`.
- If a job fails before a transcript exists (audio extraction or speech-to-text
  errors), or a worker restart interrupts it, the reservation is **refunded** in
  full. Once a transcript exists, the reserved minutes are consumed, even when
  a later stage fails.
- Publishing a locally produced review (`/api/v1/reviews/publish`) needs an
  active plan with minutes left, but consumes no minutes.
- Without an active plan, or without enough remaining minutes, job creation and
  publishing answer **HTTP 402 Payment Required** with this exact body (`code`
  is `no_active_subscription` or `quota_exceeded`):

  ```json
  {
    "error": "subscription_required",
    "code": "no_active_subscription",
    "message": "An active Screenscribe subscription is required.",
    "upgrade_url": "https://cloud.example.com/billing"
  }
  ```

  402 is reserved for entitlement problems. It never signals a server fault, so
  the CLI, the macOS app and the portal treat it as "subscribe or upgrade at
  `/billing`".

## 10. Uploads, live progress and video

**Uploads.** `POST /api/v1/jobs` accepts a streaming multipart upload of up to
`SCREENSCRIBE_CLOUD_MAX_UPLOAD_BYTES` (2 GiB by default). The application
enforces the exact limit while it reads the stream. The largest body is a review
publish: the source video plus the review bundle
(`SCREENSCRIBE_CLOUD_MAX_BUNDLE_BYTES`, 512 MiB by default). Caddy enforces an
outer cap (`request_body max_size 2600MiB`) set slightly above that sum, so the
application, not the proxy, produces the error response. If you raise either
application limit, raise the Caddy cap with it.

Caddy has no total body-read or write deadline. The proxy transport uses
per-read and per-write idle timeouts of 15 minutes. A multi-gigabyte upload over
a slow uplink completes as long as bytes keep moving.

A CDN or load balancer in front of Caddy brings its own limits. Proxied plans on
Cloudflare, for example, cap request bodies at 100 MB. Run such a layer in
DNS-only mode for this hostname, or raise its limits to match.

**Live progress (SSE).** `GET /api/v1/jobs/{id}/events` is a Server-Sent Events
stream. The server checks job state every `SCREENSCRIBE_CLOUD_SSE_POLL_SECONDS`
and sends a keepalive every `SCREENSCRIBE_CLOUD_SSE_KEEPALIVE_SECONDS` when the
stream is idle. Caddy proxies it with `flush_interval -1`, so every event is
flushed immediately, and excludes it from compression. Any additional proxy you
add must not buffer this path either. For example, nginx needs
`proxy_buffering off`.

**Video.** `/r/{share_id}/video` and `/api/v1/reviews/{share_id}/video` serve
HTTP Range requests (`206 Partial Content`), so the review player seeks
instantly inside long recordings. Caddy passes `Range` through and never
compresses video. Caching layers must forward `Range` and must not cache
partial responses as full ones.

## 11. Hosted review visibility

A published review lives at `/r/{share_id}`. The `share_id` is an unguessable
random capability, and the review's own data API is scoped under
`/r/{share_id}/api/*`.

| Visibility | Who can open `/r/{share_id}` |
|---|---|
| `private` | Only the signed-in owner. Anonymous visitors get 401, other accounts 403. |
| `unlisted` | Anyone holding the link. The link is the credential. |

`team` is accepted on upload and publish but is stored as `private` until team
workspaces ship.

Unlisted visitors can review (verdicts, notes, manual frames). Resetting a
review and deleting it stay owner-only. Paid AI features inside the review
(voice notes, manual-frame analysis, the review agent) run on the owner's
subscription and stop with 402 when it lapses.

Reviews are published from the CLI or the macOS app through
`/api/v1/reviews/publish` and listed at `/api/v1/reviews`. Each review also
serves `report.json` and an `agent-manifest` under
`/api/v1/reviews/{share_id}/` for coding agents.

Treat unlisted links like passwords. Caddy keeps access logging off so that
capability URLs never land in proxy logs.

## 12. Security model

**Edge and binding**

- Caddy is the only public listener. It terminates TLS, sends HSTS,
  `X-Content-Type-Options: nosniff` and a default
  `Referrer-Policy: strict-origin-when-cross-origin`, and strips the `Server`
  and `Via` headers. Content-Security-Policy is set per route by the
  application.
- The application binds loopback by default. Only the `web` container widens
  the bind, and its port is reachable solely from the compose network.

**Browser sessions (portal)**

- Sign-in sets `ss_session`, an HttpOnly cookie. It is `Secure` whenever the
  base URL is HTTPS.
- Every cookie-authenticated write must pass two independent checks:
  1. The `ss_csrf` double-submit token: the cookie value echoed back in the
     `X-CSRF-Token` header (scripts) or the `_csrf_token` form field (HTML
     forms).
  2. An `Origin`/`Referer` whose host is in the allow-list: the base-URL host,
     `SCREENSCRIBE_CLOUD_TRUSTED_HOSTS`, and the hosts of
     `SCREENSCRIBE_CLOUD_CORS_ORIGINS`.

Anonymous writes to an unlisted review's `/r/{share_id}/api/*` pass the same
two checks. The hosted review page sets `ss_csrf`, and a small script injected
ahead of the review bundle adds the header.

**Native clients (CLI, macOS)**

- They authenticate with `Authorization: Bearer <access-token>`. They use no
  cookies, so there is no CSRF surface.
- Refresh tokens rotate on every use. Presenting an already-rotated refresh
  token revokes the whole token family, which ends a stolen session at its
  first replay.

**Credentials, CORS and AI keys**

- Passwords are stored as scrypt hashes.
- CORS is an explicit allow-list (`SCREENSCRIBE_CLOUD_CORS_ORIGINS`). There is
  no wildcard origin.
- **ZERO BYOK:** the AI key exists only in the server environment. No endpoint
  accepts a customer AI key, and the server never returns its own.

**Billing integrity**

- Stripe webhooks are verified by HMAC with a 300 second tolerance and are
  idempotent per event id.
- App Store transactions are verified up a chain pinned to Apple Root CA G3,
  with the bundle id enforced.

**Containers and secrets**

- Containers run as uid 10001 with `cap_drop: [ALL]` and `no-new-privileges`.
  Caddy keeps only `NET_BIND_SERVICE`.
- `.env.cloud` stays mode 600 and is never committed. The Dockerfile copies
  explicit paths only, so `.env.cloud` never enters an image layer. The Caddy
  container receives only the domain and the ACME contact.

## 13. Backups and restore

State lives in two volumes:

- `screenscribe-cloud_screenscribe_data` holds the database and the review files.
- `screenscribe-cloud_caddy_data` holds the certificates and the ACME account.
  Back it up so that a rebuild does not hit certificate-issuance rate limits.

The order of a backup matters. Snapshot the database first, then archive the
files. Every file the snapshot references then exists in the archive.

**1. Online database snapshot.** SQLite's online backup API is the mechanism
behind the `sqlite3` CLI's `.backup` command. It copies a consistent image of
the WAL database while web and worker keep running. The image ships Python, so
the snapshot runs inside the `web` container:

```sh
mkdir -p backups
dc exec -T web python -c "import sqlite3; src = sqlite3.connect('/var/lib/screenscribe/data/cloud.sqlite3'); dst = sqlite3.connect('/tmp/cloud-backup.sqlite3'); src.backup(dst); dst.close(); src.close()"
dc cp web:/tmp/cloud-backup.sqlite3 "backups/cloud-$(date +%Y%m%d-%H%M).sqlite3"
dc exec -T web rm -f /tmp/cloud-backup.sqlite3
```

With the `sqlite3` CLI on the host, the equivalent command runs as uid 10001,
so SQLite never creates root-owned `-wal`/`-shm` files. The volume path below is
the default Docker data root, and the destination must be writable by uid 10001:

```sh
sudo -u '#10001' sqlite3 /var/lib/docker/volumes/screenscribe-cloud_screenscribe_data/_data/cloud.sqlite3 \
  ".backup '/var/backups/screenscribe/cloud.sqlite3'"
```

**2. Review files.**

```sh
docker run --rm \
  -v screenscribe-cloud_screenscribe_data:/data:ro \
  -v "$PWD/backups":/backups \
  debian:bookworm-slim \
  tar --exclude='./cloud.sqlite3*' -czf "/backups/screenscribe-files-$(date +%Y%m%d-%H%M).tar.gz" -C /data .
```

Schedule both steps (cron or a systemd timer), copy the results off the host,
and encrypt them at rest.

**Restore** into an empty volume, either on a fresh host or after
`docker volume rm screenscribe-cloud_screenscribe_data`:

```sh
dc down
docker run --rm \
  -v screenscribe-cloud_screenscribe_data:/data \
  -v "$PWD/backups":/backups \
  debian:bookworm-slim \
  sh -c 'tar -xzf /backups/screenscribe-files-<stamp>.tar.gz -C /data \
    && cp /backups/cloud-<stamp>.sqlite3 /data/cloud.sqlite3 \
    && rm -f /data/cloud.sqlite3-wal /data/cloud.sqlite3-shm \
    && chown -R 10001:10001 /data'
dc up -d
```

## 14. Upgrades and rollback

```sh
# 1. Back up first (section 13).
git fetch --tags
git checkout <release-tag>
dc build --pull        # --pull refreshes the Python base image
dc up -d               # init-db migrates, then web and worker are recreated
dc ps
```

`init-db` is idempotent and runs on every `up`, before `web` and `worker`. The
job queue lives in SQLite, so queued jobs survive the restart. The worker gets a
60 second stop grace period. During the short `web` restart, Caddy retries the
upstream for up to 15 seconds instead of returning 502, and browsers' EventSource
reconnects on its own. Uploads in flight at the moment of the restart are cut,
so upgrade in a quiet window.

To update Caddy, run `dc pull caddy && dc up -d caddy`.

To roll back, check out the previous tag, rebuild, and restore the pre-upgrade
backup. An older build is not guaranteed to read a newer schema.

## 15. Observability

| Signal | Where | Meaning |
|---|---|---|
| `GET /healthz` | Docker healthcheck on `web`, `curl` | Liveness: 200 `{"status":"ok"}` while the process serves. |
| `GET /readyz` | External uptime monitor | Readiness: 200 when the database opens and storage is writable, otherwise 503. Point monitoring here. |
| `dc ps` | Host | Container state; `web` shows `healthy`. |
| `dc logs -f web worker` | Host | Application logs. Unbuffered stdout, json-file driver, 10 MB x 5 files per container. |
| `dc logs caddy` | Host | Certificate issuance and proxy errors. Access logging is off by design (section 11). |
| `dc exec web du -sh /var/lib/screenscribe/data/uploads /var/lib/screenscribe/data/reviews` | Host | Storage growth. |

The worker has no HTTP port and its image healthcheck is disabled. Its logs and
the job states in the portal show its progress, and `restart: unless-stopped`
brings it back after a crash.

## 16. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `docker compose` aborts with `set SCREENSCRIBE_DOMAIN to the public hostname` | The domain is missing from the interpolation environment. | Pass `--env-file .env.cloud` or export `SCREENSCRIBE_DOMAIN`. |
| `env file ... .env.cloud not found` | `.env.cloud` is not in the repository root. | Create it there (section 3.2). |
| Caddy logs ACME or challenge errors | DNS does not point at the host, ports 80/443 are blocked, or a CA rate limit was hit. | Fix DNS or the firewall, keep the `caddy_data` volume, and wait out the rate limit. |
| 502 Bad Gateway | `web` is not running or not healthy. | Check `dc ps`, `dc logs web` and `dc logs init-db`. |
| `web` never starts and `init-db` exited non-zero | The migration failed, or the volume is not writable. | Read `dc logs init-db` and check volume ownership (uid 10001). |
| `/readyz` returns 503 | The database cannot be opened, the storage is not writable, or the disk is full. | Use a named volume. A bind mount needs `chown -R 10001:10001` on its host directory. Free disk space. |
| Portal login loops or form posts return 403 | Users open a hostname other than the base-URL host, so the Origin check fails. | Serve one canonical hostname, or add the alias to `SCREENSCRIBE_CLOUD_TRUSTED_HOSTS`. |
| 413 on large uploads | The proxy cap and the application limit disagree, or a CDN caps bodies. | Keep Caddy's `max_size` slightly above `SCREENSCRIBE_CLOUD_MAX_UPLOAD_BYTES` + `SCREENSCRIBE_CLOUD_MAX_BUNDLE_BYTES`, and switch the CDN to DNS-only. |
| Job progress appears only at the end | An extra proxy or CDN buffers the SSE stream. | Disable buffering for `/api/v1/jobs/*/events`. |
| Video does not seek | A caching layer strips `Range`. | Bypass caching for `/r/*/video` and `/api/v1/reviews/*/video`. |
| Jobs stay queued | The worker is down, or the server has no AI key. | Check `dc logs worker` and set `SCREENSCRIBE_API_KEY`. |
| Every job creation returns 402 | The account has no active plan or no minutes left. | Check the subscription. In the Stripe Dashboard, confirm that webhook deliveries succeed. |
| Stripe webhook deliveries fail signature checks | The signing secret is wrong (test vs live), or the clock is skewed by more than 300 seconds. | Copy the endpoint's secret again and enable NTP. |
| Sign in with Apple is rejected | The token `aud` is not accepted. | Add the bundle id and Services ID to `SCREENSCRIBE_CLOUD_APPLE_CLIENT_IDS`. |
| App Store sync is rejected | The bundle id does not match, the product is not mapped, or the transaction comes from the Xcode environment. | Fix `SCREENSCRIBE_CLOUD_APPLE_BUNDLE_ID` and `SCREENSCRIBE_CLOUD_APPSTORE_PRODUCTS`, and test with real sandbox purchases. |

## 17. Local single-process dev

For development, one process serves the API and runs the pipeline. Set
`SCREENSCRIBE_CLOUD_EMBEDDED_WORKER=1`, keep the loopback default, and keep
state outside the repository. `ffmpeg` and `ffprobe` must be on `PATH`.

```sh
export SCREENSCRIBE_API_KEY=<libraxis-api-key>
export SCREENSCRIBE_CLOUD_EMBEDDED_WORKER=1
export SCREENSCRIBE_CLOUD_STORAGE_DIR="${TMPDIR:-/tmp}/screenscribe-cloud"
uv run python -m screenscribe.cloud init-db
uv run python -m screenscribe.cloud serve            # http://127.0.0.1:8080
```

- `serve` accepts `--host` and `--port`. The defaults come from
  `SCREENSCRIBE_CLOUD_HOST` and `SCREENSCRIBE_CLOUD_PORT`.
- The base URL defaults to `http://127.0.0.1:8080`. Cookies are therefore not
  marked `Secure`, and the portal works over plain HTTP on loopback.
- For Stripe test mode, forward webhooks with the Stripe CLI and export the
  signing secret it prints:

  ```sh
  stripe listen --forward-to http://127.0.0.1:8080/api/v1/billing/stripe/webhook
  export SCREENSCRIBE_CLOUD_STRIPE_WEBHOOK_SECRET=<signing-secret-from-stripe-listen>
  ```

- For local StoreKit testing, set `SCREENSCRIBE_CLOUD_APPSTORE_ALLOW_XCODE=1`.

_𝚅𝚒𝚋𝚎𝚌𝚛𝚊𝚏𝚝𝚎𝚍. with AI Agents by Vetcoders (c)2024-2026 LibraxisAI_
