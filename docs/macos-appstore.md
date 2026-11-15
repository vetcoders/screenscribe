# Screenscribe for macOS — Mac App Store client

Screenscribe for macOS is a native Swift 6 / SwiftUI app that records a
narrated screen walkthrough, uploads it to Screenscribe Cloud and shows the
hosted review. Audio extraction, transcription, issue detection, screenshots,
analysis and report rendering all run in Screenscribe Cloud (Band 2). The app
ships no ffmpeg, ffprobe, Python, Node or private frameworks, and it runs
entirely inside the App Sandbox.

The app has four jobs:

1. **Recorder**: ScreenCaptureKit capture of a display, window or app with
   system audio and microphone narration.
2. **Uploader**: streams the file from disk with a resumable queue and
   idempotent retries.
3. **Subscription manager**: StoreKit 2, with the entitlement synced to the
   Screenscribe account.
4. **Review companion**: live pipeline progress and the hosted report at
   `/r/{share_id}`.

## Layout

```
macos/
├── Package.swift                  # SwiftPM: ScreenscribeCore (library) + ScreenscribeApp (executable)
├── Config/
│   ├── Info.plist                 # io.vetcoders.screenscribe, macOS 15+, usage strings, en/pl
│   ├── Screenscribe.entitlements  # sandbox entitlements (Debug / local signing)
│   ├── ScreenscribeAppStore.entitlements  # + Sign in with Apple (Release / App Store)
│   ├── Screenscribe.storekit      # local StoreKit testing: "Screenscribe Pro" group
│   └── project.yml                # XcodeGen spec for the signed .app
└── Sources/
    ├── ScreenscribeCore/          # no SwiftUI: models, networking, queue, billing, capture, state
    │   ├── Config/                # AppConfiguration (+ store), ReviewPreset, languages, visibility
    │   ├── Localization/          # L10n (Bundle.module lookup), DisplayFormat
    │   ├── Models/                # Auth, Billing, Job, Review wire models
    │   ├── Security/              # KeychainStore (tokens)
    │   ├── Networking/            # CloudEnvironment, ScreenscribeCloudClient, multipart, SSE, refresh
    │   ├── Queue/                 # UploadQueueStore (journal, recordings, imports, scratch)
    │   ├── Billing/               # StoreKitManager (StoreKit 2)
    │   ├── Capture/               # ScreenCaptureEngine, PermissionPreflight, VideoFileValidator
    │   ├── State/                 # AppModel + Auth/Paywall/Recorder/Workspace view models
    │   └── Resources/{en,pl}.lproj/Localizable.strings
    └── ScreenscribeApp/           # SwiftUI scenes and views
        ├── ScreenscribeMacApp.swift   # Window + MenuBarExtra(.window) + Settings + commands
        ├── Theme/ScreenscribeTheme.swift
        └── Views/                 # MainWorkspace, MenuBarHUD, PipelineProgress, ReviewWeb,
                                   # SignInSheet, PaywallSheet, Settings
```

`ScreenscribeCore` holds all behavior and is UI-free, so it can be tested
without a window server. `ScreenscribeApp` holds only views. All
user-visible strings live in Core's `Localizable.strings` and are read with
`L10n.string` / `L10n.format`.

## Build and run

The signed `.app` is generated with XcodeGen from `Config/project.yml`:

```bash
cd macos
xcodegen generate --spec Config/project.yml --project .
open Screenscribe.xcodeproj
```

- Set the development team once in Xcode (Signing & Capabilities). The spec
  leaves `DEVELOPMENT_TEAM` unset so that no team ID is committed.
- The **Debug** build uses `Config/Screenscribe.entitlements`, which can be
  signed locally with any certificate. The **Release** build uses
  `Config/ScreenscribeAppStore.entitlements`, which adds Sign in with Apple
  and needs an App ID with that capability.
- The Run action loads `Config/Screenscribe.storekit`, so purchases, renewals,
  refunds and Ask to Buy can be tested locally without App Store Connect.
- `swift build` builds the `ScreenscribeApp` executable for quick
  compile-checks of the package. Screen capture, Keychain, the sandbox and
  StoreKit only behave as in production when the app runs as the signed
  `.app` bundle.

The generated `Screenscribe.xcodeproj`, `.build/`, `.swiftpm/` and
`DerivedData/` are ignored by `macos/.gitignore`.

## App Sandbox and entitlements

| Entitlement | Debug | App Store | Why |
|---|---|---|---|
| `com.apple.security.app-sandbox` | ✓ | ✓ | Required for the Mac App Store |
| `com.apple.security.network.client` | ✓ | ✓ | HTTPS to Screenscribe Cloud |
| `com.apple.security.files.user-selected.read-only` | ✓ | ✓ | Import of a recording chosen in the open panel or dropped on the window |
| `com.apple.security.device.audio-input` | ✓ | ✓ | Microphone narration |
| `com.apple.developer.applesignin` | — | ✓ (`Default`) | Sign in with Apple |

The entitlements contain no temporary exceptions, no `network.server` and
no read-write file access. Screen recording is a TCC permission, not an
entitlement. Imported files are copied into the container
(`Application Support/Screenscribe/Imports`) while the security scope is
open, so the queue never needs bookmarks to survive a relaunch.

`Info.plist` declares:

- `NSMicrophoneUsageDescription` and `NSScreenCaptureUsageDescription`;
- `ITSAppUsesNonExemptEncryption = false` (HTTPS only, through the system
  TLS stack);
- `LSMinimumSystemVersion = 15.0`;
- `CFBundleLocalizations = [en, pl]` with `CFBundleAllowMixedLocalizations`,
  so the Core resource bundle follows the user's language;
- `NSAllowsLocalNetworking`, so a developer can point the app at a local
  Band 2 on `http://127.0.0.1` or `http://localhost`.

## Privacy

- **No AI provider keys.** The app has no OpenAI, Anthropic, Gemini, Groq,
  xAI or Libraxis key fields anywhere, including Settings (decyzja Macieja,
  2026-11-15). Every model call runs on the server.
- **One origin.** Recordings and tokens go only to the Screenscribe Cloud URL
  set in Settings › Cloud. That URL must be HTTPS. Plain HTTP is accepted
  only for loopback hosts. URLs with embedded credentials are rejected.
- **Origin-bound tokens.** `Authorization: Bearer` is attached only to
  requests whose scheme, host and port match the configured origin.
  Server-provided links (`status_url`, `events_url`, `share_url`) are
  resolved and checked before use. Changing the Cloud URL signs out first,
  so a token is never sent to a new host.
- **Local storage:**
  - Tokens: in the Keychain.
  - Recording and review defaults: in `UserDefaults`.
  - Upload journal, recordings, imports, recent-review cache and multipart
    scratch files: in `~/Library/Containers/io.vetcoders.screenscribe/Data/Library/Application Support/Screenscribe/`.
- **Sign-out** always purges the Keychain session, even when the server
  logout call fails.
- **The review web view** uses a non-persistent `WKWebsiteDataStore`. Pages
  stay in the app on the configured origin and on the share link's own
  origin; every other link opens in the default browser. The Bearer goes to
  the configured origin only.

## Band 2 wire

`ScreenscribeCloudClient` (an actor) implements the client side of the
shared wire contract (`SHARED_WIRE_CONTRACT.md`):

| Purpose | Method and path |
|---|---|
| Sign in with Apple | `POST /api/v1/auth/apple` |
| Email register / login | `POST /api/v1/auth/register`, `POST /api/v1/auth/login` |
| Session refresh (single-flight) | `POST /api/v1/auth/token/refresh` |
| Profile + subscription | `GET /api/v1/auth/me` |
| Logout | `POST /api/v1/auth/logout` |
| Device flow (RFC 8628) | `POST /api/v1/auth/device/code`, `POST /api/v1/auth/device/token` |
| App Store entitlement sync | `POST /api/v1/billing/appstore/sync` |
| Create job (multipart) | `POST /api/v1/jobs` → `202` |
| Job status | `GET /api/v1/jobs/{job_id}` |
| Job progress (SSE) | `GET /api/v1/jobs/{job_id}/events` |
| Review history | `GET /api/v1/reviews` |
| Report JSON | `GET /api/v1/reviews/{share_id}/report.json` |
| Hosted review | `/r/{share_id}` (web) |
| Account management | `/account` (web) |

### Authentication

- **Sign in with Apple:** `SignInWithAppleButton` requests the email and
  full name, and the identity token goes to `/auth/apple`. Apple shares the
  email and name only on the first authorization; the server keeps them.
- **Email:** register or sign in with email and password. Registration
  requires at least 8 characters.
- **Browser (device flow):** for accounts created on the web. The client ID
  is `screenscribe-macos`. The app shows the user code, opens
  `verification_uri_complete` (or `verification_uri`) in the browser, and
  polls at the server interval, never faster than 5 s. It handles
  `authorization_pending`, `slow_down` (+5 s), `access_denied` and
  `expired_token`. A cancelled flow can never overwrite a newer one.
- **Refresh:** `TokenRefreshCoordinator` runs one refresh at a time, and
  concurrent `401`s share it. A terminal refresh failure clears the session
  and presents the sign-in sheet with an explanation.

### Billing (StoreKit 2)

| Item | Value |
|---|---|
| Subscription group | `Screenscribe Pro` |
| Monthly | `io.vetcoders.screenscribe.pro.monthly` (`P1M`) |
| Annual | `io.vetcoders.screenscribe.pro.annual` (`P1Y`) |

1. Only verified transactions (`VerificationResult.verified`) are accepted.
2. Each transaction's JWS (`signed_transaction_info`, plus
   `signed_renewal_info` when present) is sent to
   `POST /api/v1/billing/appstore/sync` **before** `Transaction.finish()`. A
   failed sync leaves the transaction unfinished. It is retried at launch
   (the syncer is installed before the `Transaction.updates` listener
   starts, then `retryPendingSync()` runs), on sign-in, and on each
   `Transaction.updates` delivery. The server is the entitlement authority.
3. Restore runs `AppStore.sync()` and then re-syncs
   `Transaction.currentEntitlements`.
4. A purchase is attached to the Screenscribe account, so a signed-out user
   is asked to sign in first. The paywall reopens after sign-in.
5. `402 Payment Required` stops the upload queue. The item becomes
   *Subscription required* and the paywall is presented. The item is
   released automatically once a fresh profile reports `can_create_jobs`.

App Review alignment:

- **3.1.1:** the app never opens the server's `upgrade_url`. All purchases
  go through StoreKit.
- **3.1.2:** the paywall shows each plan's name, price and period, the
  auto-renewal terms, and links to the Terms of Use (Apple standard EULA)
  and the Privacy Policy. *Manage Subscription* opens the App Store
  subscriptions page.
- **5.1.1(v):** Settings › Account links to the Band 2 account page for data
  export and account deletion.

### Upload

1. `MultipartStreamBuilder` copies the recording in **256 KB chunks** into a
   scratch file together with the form fields: `file`, `language`, `preset`,
   `prompt` and `visibility`. Memory stays flat for multi-GB recordings.
2. `URLSession.uploadTask(fromFile:)` sends the body with an
   `Idempotency-Key` that is stable for the queue item. Retries can never
   create a duplicate job.
3. The server answers `202` with the job ID and the status, events and
   share links.
4. Progress updates reach the UI at most every 0.5 % or 0.5 s, so a
   multi-GB upload does not flood the main actor. An upload interrupted by a
   quit or crash goes back to *Waiting* on the next launch and starts again
   with the same `Idempotency-Key`. A job that was already created resumes
   tracking.
5. Retryable failures back off exponentially: 5 s, 10 s, 20 s and so on,
   capped at 5 minutes, for up to 6 attempts. User cancellation returns the
   item to *Ready to upload* without stopping the rest of the queue. Auth
   failures pause the queue until the user signs in again.

### Progress

`GET /api/v1/jobs/{id}/events` is read with `URLSession.bytes(for:)` and
parsed by `SSEEventParser`. When the stream fails or the server does not
offer it, the client falls back to polling `GET /api/v1/jobs/{id}` every 3 s,
with linear backoff on errors.

The pipeline view shows: upload → audio → transcribe → detect → screenshots
→ analyze → report. When a job completes, the app refreshes the profile
(minutes used) and the review history.

## Capture

`ScreenCaptureEngine` wraps ScreenCaptureKit:

- **Sources:** displays, windows and applications from
  `SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: true)`.
- **Self-exclusion:** every window of the Screenscribe process (the
  workspace, the HUD and sheets) is excluded from the display filter, and the
  app's own audio is excluded with `excludesCurrentProcessAudio`.
- **Encoding:** H.264 by default, HEVC as an option. The bitrate scales with
  resolution and frame rate, and the color space is Rec. 709. The output is
  MP4 or MOV.
- **Audio:** system audio (48 kHz stereo AAC) and the microphone, using the
  chosen device or the system default, in separate tracks.
- **Countdown:** 0, 3, 5 or 10 s, and it can be cancelled.
- **Pause and resume:** sample buffers are re-timed by the paused interval,
  so the file has no gap.
- **Crash safety:** the movie is written as a fragmented file (10 s
  fragments), so a crash or a stream interruption keeps everything up to the
  last fragment. On launch, recordings that no queue item references are
  re-adopted as *Ready to upload* drafts.
- **Permissions:** `PermissionPreflight` reports Screen Recording and
  Microphone access and deep-links into System Settings › Privacy &
  Security.

`VideoFileValidator` checks imported files inside their security scope. It
checks the type (MP4, MOV, M4V) and requires a non-empty file with a video
track, an audio track and a duration of at least 1 s. The checks use the
async `loadTracks` API.

## UI

| Scene | Content |
|---|---|
| Workspace window | `NavigationSplitView`. **Sidebar:** Record, Uploads (queue with state and progress), Recent Reviews, account footer. **Detail:** the record pane (permissions, recorder, source picker, review options), `PipelineProgressView` or `ReviewWebView`. |
| Menu bar (`MenuBarExtra`, `.window`) | Start / pause / stop with a timer, upload queue, recent reviews with Copy Share Link and Open in Browser, quit. The status item shows the running timecode. |
| Settings (⌘,) | **General:** review defaults, auto-upload, privacy note. **Recording:** codec, container, fps, countdown, pointer, audio, permissions. **Account:** plan, usage, manage or delete account, sign out. **Cloud:** server URL. |
| Sheets | Sign in (Apple, email, browser) and the Screenscribe Pro paywall. |

**Keyboard shortcuts:** ⇧⌘R starts a recording, ⌘. stops it, ⌘O imports a
video.

**Visual design:**

- Dark tokens: `#0a0b0d` canvas, `#101216` surface, `#15181d` elevated,
  `#f2f4f7` primary text, `#c8ccd2` secondary text.
- Monospaced type for timecodes, sizes and badges.
- On macOS 26 (built with the Swift 6.2 toolchain), cards and buttons use
  Liquid Glass (`glassEffect`, `.glass`, `.glassProminent`) inside a shared
  `GlassEffectContainer`. macOS 15 gets the token surfaces. The
  `#if compiler(>=6.2)` gate assumes the macOS 26 SDK (Xcode 26); a Swift
  6.2 toolchain paired with the macOS 15 SDK would not find those APIs.
- System chrome (sidebar, toolbar, sheets) keeps the native material.

## Localization

English and Polish ship in
`Sources/ScreenscribeCore/Resources/{en,pl}.lproj/Localizable.strings`. The
two tables have **identical key sets**, and format specifiers match per key.
Two-argument strings use positional specifiers (`%1$@`, `%2$@`), so
translations can reorder them. `L10n.keys(localization:)` exposes the key
sets for parity checks.

## Open items for integration

- **Multipart field name:** the client sends `file`, while
  `SHARED_WIRE_CONTRACT.md` names the field `video`. The name is one
  constructor parameter (`ScreenscribeCloudClient(uploadFieldName:)`) and
  must be aligned with Band 2.
- **Device client ID:** the app uses `screenscribe-macos`. The contract
  example uses `screenscribe-cli`. Band 2 must accept this client ID.
- **`GET /api/v1/reviews`:** not part of the contract yet. The client parses
  it tolerantly. `404`/`405` is treated as "no history", and completed local
  uploads still fill Recent Reviews.
- **Private reviews in the web view:** the Bearer is sent with the main
  document request only. Private reviews whose subresources need auth need a
  same-origin session cookie from Band 2.
- **Production host and privacy URL:** `https://screenscribe.vetcoders.io`
  and `https://vetcoders.io/privacy` must be live before submission.
- **Account deletion:** the `/account` page must offer deletion without
  extra steps (App Review 5.1.1(v)).
- **StoreKit prices:** the prices in `Screenscribe.storekit` (9.99 / 99.99)
  are for local testing only. App Store Connect is the source of truth for
  price tiers and any introductory offer.
- **Polish usage descriptions:** the permission prompts are English until
  `pl.lproj/InfoPlist.strings` is added next to `Info.plist`.

---

_𝚅𝚒𝚋𝚎𝚌𝚛𝚊𝚏𝚝𝚎𝚍. with AI Agents by Vetcoders (c)2024-2026 LibraxisAI_
