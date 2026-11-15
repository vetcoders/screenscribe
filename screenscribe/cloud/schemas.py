"""Pydantic v2 wire models and constants for the shared cloud contract (v1).

Response bodies are assembled as plain dicts by the routers so the exact key
set of ``SHARED_WIRE_CONTRACT.md`` is emitted (no extra ``null`` members);
the models here validate inbound JSON and document the shapes.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

BEARER_SCHEME = "Bearer"
ACCESS_EXPIRES_IN = 3600
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
REFRESH_GRANT = "refresh_token"

SUBSCRIPTION_REQUIRED_MESSAGE = "An active Screenscribe subscription is required."

JOB_LANGUAGES: tuple[str, ...] = ("auto", "en", "pl")
JOB_PRESETS: tuple[str, ...] = ("programming", "casual", "medical", "veterinary")
JOB_VISIBILITIES: tuple[str, ...] = ("private", "unlisted", "team")
JOB_STATUSES: tuple[str, ...] = ("queued", "processing", "completed", "failed")
JOB_STAGES: tuple[str, ...] = (
    "queued",
    "audio",
    "transcribe",
    "detect",
    "screenshots",
    "analyze",
    "report",
    "done",
    "error",
)
REVIEW_VISIBILITIES: tuple[str, ...] = ("private", "unlisted")

GateCode = Literal["no_active_subscription", "quota_exceeded"]
DeviceErrorCode = Literal[
    "authorization_pending",
    "slow_down",
    "expired_token",
    "access_denied",
    "invalid_grant",
    "invalid_request",
    "unsupported_grant_type",
]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)


class RegisterRequest(_Strict):
    email: str = Field(..., min_length=3, max_length=320)
    password: str = Field(..., min_length=8, max_length=1024)
    display_name: str | None = Field(default=None, max_length=120)
    preferred_lang: str | None = Field(default=None, max_length=8)
    cookie: bool = False


class LoginRequest(_Strict):
    email: str = Field(..., min_length=3, max_length=320)
    password: str = Field(..., min_length=1, max_length=1024)
    cookie: bool = False


class AppleSignInRequest(_Strict):
    identity_token: str = Field(..., min_length=10, max_length=16384)
    email: str | None = Field(default=None, max_length=320)
    display_name: str | None = Field(default=None, max_length=120)


class CheckoutRequest(_Strict):
    plan: str = Field(default="pro", max_length=32)
    success_path: str | None = Field(default=None, max_length=512)
    cancel_path: str | None = Field(default=None, max_length=512)


class AppStoreSyncRequest(_Strict):
    signed_transaction_info: str = Field(..., min_length=10, max_length=65536)
    signed_renewal_info: str | None = Field(default=None, max_length=65536)
    environment: str = Field(default="Production", max_length=32)


class SubscriptionOut(BaseModel):
    active: bool
    plan: str
    source: str
    period_end: str | None
    minutes_limit: int | float
    minutes_used: float
    minutes_remaining: float


class MeOut(BaseModel):
    user_id: str
    email: str
    subscription: SubscriptionOut


class UserOut(BaseModel):
    user_id: str
    email: str
    display_name: str
    preferred_lang: str
    apple_linked: bool
    created_at: str


def subscription_required_body(code: GateCode, upgrade_url: str) -> dict[str, Any]:
    """Exact ``402 Payment Required`` body from the shared wire contract."""
    return {
        "error": "subscription_required",
        "code": code,
        "message": SUBSCRIPTION_REQUIRED_MESSAGE,
        "upgrade_url": upgrade_url,
    }


def inactive_subscription() -> dict[str, Any]:
    """``/me`` subscription block for users without an active subscription."""
    return {
        "active": False,
        "plan": "none",
        "source": "none",
        "period_end": None,
        "minutes_limit": 0,
        "minutes_used": 0,
        "minutes_remaining": 0,
    }


__all__ = [
    "ACCESS_EXPIRES_IN",
    "BEARER_SCHEME",
    "DEVICE_GRANT",
    "JOB_LANGUAGES",
    "JOB_PRESETS",
    "JOB_STAGES",
    "JOB_STATUSES",
    "JOB_VISIBILITIES",
    "REFRESH_GRANT",
    "REVIEW_VISIBILITIES",
    "SUBSCRIPTION_REQUIRED_MESSAGE",
    "AppStoreSyncRequest",
    "AppleSignInRequest",
    "CheckoutRequest",
    "DeviceErrorCode",
    "GateCode",
    "LoginRequest",
    "MeOut",
    "RegisterRequest",
    "SubscriptionOut",
    "UserOut",
    "inactive_subscription",
    "subscription_required_body",
]
