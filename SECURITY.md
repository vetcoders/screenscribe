# Security Policy

## Supported versions

screenscribe is pre-1.0. Security fixes are applied to the latest released
version on `main`.

| Version          | Supported |
| ---------------- | --------- |
| latest (`main`)  | ✅        |
| older            | ❌        |

## Reporting a vulnerability

Please **do not** open a public issue for security problems.

Report privately via GitHub's
[private vulnerability reporting](https://github.com/vetcoders/screenscribe/security/advisories/new),
or email **security@vetcoders.io**.

Include the affected version or commit, a description, reproduction steps, and
the impact. We aim to acknowledge within 3 business days and to share a
remediation timeline after triage.

## Scope and data handling

screenscribe processes two sensitive classes of data:

- **API keys** for the configured STT / LLM / vision provider. Keys are read
  from environment variables or `~/.config/screenscribe/config.env`, are never
  logged and are never written into generated reports. `.env` files are
  gitignored — never commit real keys.
- **User audio and video.** Media is extracted locally with FFmpeg and sent to
  the configured provider for transcription and analysis. screenscribe does not
  retain media beyond the local working / cache directory.

In scope: any way screenscribe leaks an API key, exfiltrates user media to an
unintended destination, or writes secrets into an artifact (report, log, ZIP).

## Local server hardening

The review and analyze servers bind only to `127.0.0.1` and reject any request
whose Host header is non-local (a DNS-rebinding guard on every path); `/api/*`
routes additionally require a per-process session token and a local Origin. The
source-video endpoints (`/video`, and on the review server its by-filename twin)
are token-guarded too: a `<video src>` element cannot attach a custom header, so
each video URL instead carries an HMAC-SHA256 signature — keyed by the session
token — in an `st` query parameter, and a GET without a valid signature (or the
header, for non-browser clients) is rejected with `403`. The analyze dashboard
signs these URLs as it renders; the review server injects the signature into the
report bytes it serves, so the report saved on disk stays token-free and remains
shareable (opened via `file://` its bare-filename `<video src>` still loads the
sibling video).

## Accepted risks

These are known, deliberate trade-offs — not oversights. They are revisited if
the threat model changes (e.g. the review server ever binds beyond loopback).

- **Static file mount is not token-guarded (P3-10).** The local servers protect
  their `/api/*` routes and the source-video endpoints behind the per-process
  session token (see *Local server hardening*), but the `StaticFiles` mount at
  `/` (the report bundle) is served without it. This is **risk-accepted**: the
  server binds only to `127.0.0.1`, keeps the DNS-rebinding Host guard on every
  path, is
  short-lived (it runs only during an interactive review on the user's own
  machine), and serves the same report files the user already has on disk. The
  accepted exposure is limited to other local processes on the loopback
  interface for the lifetime of the review session. **Condition:** this
  acceptance holds only while the server is loopback-bound and ephemeral; if it
  is ever exposed on a non-loopback interface or made long-running, the static
  mount must move behind the session-token guard.
