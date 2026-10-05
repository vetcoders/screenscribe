"""PKCE (RFC 7636) verifier/challenge helpers for the account OAuth flows.

Screenscribe is a public client -- it cannot keep a secret -- so an authorization
code is bound to a one-time verifier. Only the ``S256`` challenge ever travels
in a request URL; the verifier stays in memory until the token exchange.

Ported from Codescribe ``core/llm/account_auth/pkce.rs`` (read-only reference).
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from dataclasses import dataclass


@dataclass(frozen=True)
class PkceCodes:
    """One PKCE pair for a single authorization attempt. Never reuse."""

    code_verifier: str
    code_challenge: str


def _b64url_no_pad(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def challenge_for_verifier(code_verifier: str) -> str:
    """``S256`` transform: base64url-no-pad of the verifier's SHA-256 digest."""
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    return _b64url_no_pad(digest)


def generate_pkce() -> PkceCodes:
    """Fresh verifier from 64 bytes of OS entropy (86 base64url chars) + challenge."""
    code_verifier = _b64url_no_pad(secrets.token_bytes(64))
    return PkceCodes(
        code_verifier=code_verifier, code_challenge=challenge_for_verifier(code_verifier)
    )
