"""Stdlib-only JWS and X.509 verification for Screenscribe Cloud.

Two Apple-signed token families reach the cloud service:

* App Store Server payloads (StoreKit 2 transactions and App Store Server
  Notifications) are compact JWS objects signed with ES256. The signing key is
  the leaf certificate of the ``x5c`` header chain, and that chain must end at a
  pinned Apple Root CA - G3 certificate.
* Sign in with Apple identity tokens are compact JWS objects signed with a key
  published at Apple's JWKS endpoint (RS256, with ES256 also accepted).

The cryptography is built on the standard library alone (``hashlib``, ``hmac``
and Python integers), so the service carries no native crypto dependency. Only
signature *verification* is implemented. Verification handles public data, so
the elliptic-curve arithmetic is not constant-time; digest and encoded-message
comparisons still go through ``hmac.compare_digest``.

Every failure is raised as :class:`JwsError`. Its message is safe to log and to
return to a client, because it never echoes token or certificate contents.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import string
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, NoReturn, Protocol

import httpx

__all__ = [
    "APPLE_INTERMEDIATE_OID",
    "APPLE_ISSUER",
    "APPLE_LEAF_OID",
    "APPLE_ROOT_CA_G3_SHA256",
    "DEFAULT_APPLE_ROOT_FINGERPRINTS",
    "MAX_JWS_LENGTH",
    "AppleJwksVerifier",
    "AppleJwsVerifier",
    "AppleStoreKitVerifier",
    "Certificate",
    "EcPublicKey",
    "HmacStoreKitVerifier",
    "JwsError",
    "JwsParts",
    "RsaPublicKey",
    "StaticJwksVerifier",
    "StaticKeyStoreKitVerifier",
    "StoreKitVerifier",
    "b64url_decode",
    "b64url_encode",
    "ec_public_key_from_jwk",
    "normalize_fingerprint",
    "parse_certificate",
    "parse_compact",
    "rsa_public_key_from_jwk",
    "validate_identity_claims",
    "verify_certificate_signature",
    "verify_ecdsa",
    "verify_es256",
    "verify_hs256",
    "verify_rs256",
    "verify_x5c_chain",
]

# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------

MAX_JWS_LENGTH = 64 * 1024
"""Upper bound for a compact JWS, in characters. Apple payloads are a few KiB."""

# SHA-256 over the DER of "Apple Root CA - G3" (CN=Apple Root CA - G3,
# OU=Apple Certification Authority, O=Apple Inc., C=US; valid 2014-04-30 to
# 2039-04-30, P-384, ecdsa-with-SHA384). Confirmed by hashing the certificate
# served at https://www.apple.com/certificateauthority/AppleRootCA-G3.cer.
APPLE_ROOT_CA_G3_SHA256 = (
    "63:34:3A:BF:B8:9A:6A:03:EB:B5:7E:9B:3F:5F:A7:BE:"
    "7C:4F:5C:75:6F:30:17:B3:A8:C4:88:C3:65:3E:91:79"
)
DEFAULT_APPLE_ROOT_FINGERPRINTS: tuple[str, ...] = (APPLE_ROOT_CA_G3_SHA256,)

# Marker extension on App Store receipt / StoreKit signing leaf certificates.
APPLE_LEAF_OID = "1.2.840.113635.100.6.11.1"
# Marker extension on Apple Worldwide Developer Relations intermediate CAs.
APPLE_INTERMEDIATE_OID = "1.2.840.113635.100.6.2.1"

APPLE_ISSUER = "https://appleid.apple.com"

# ---------------------------------------------------------------------------
# Private limits and identifiers
# ---------------------------------------------------------------------------

_JWKS_MIN_REFETCH_SECONDS = 60.0
_MAX_JWKS_BYTES = 256 * 1024
_MAX_CERTIFICATE_BYTES = 64 * 1024
_MIN_CHAIN_LENGTH = 2
_MAX_CHAIN_LENGTH = 4
_CLOCK_SKEW_SECONDS = 60.0
_MAX_IAT_FUTURE_SECONDS = 300.0
_MIN_SANE_SIGNED_DATE = 1_388_534_400.0  # 2014-01-01T00:00:00Z, before Apple Root CA - G3
_MAX_SIGNED_DATE_FUTURE_SECONDS = 300.0
_RSA_MIN_BITS = 2048
_RSA_MAX_BITS = 8192
_RSA_MAX_EXPONENT_BITS = 64

_IDENTITY_ALGORITHMS = frozenset({"RS256", "ES256"})
_HEX_DIGITS = frozenset(string.hexdigits.upper())

_OID_EC_PUBLIC_KEY = "1.2.840.10045.2.1"
_OID_PRIME256V1 = "1.2.840.10045.3.1.7"
_OID_SECP384R1 = "1.3.132.0.34"
_OID_RSA_ENCRYPTION = "1.2.840.113549.1.1.1"
_OID_ECDSA_WITH_SHA256 = "1.2.840.10045.4.3.2"
_OID_ECDSA_WITH_SHA384 = "1.2.840.10045.4.3.3"
_OID_SHA256_WITH_RSA = "1.2.840.113549.1.1.11"
_OID_BASIC_CONSTRAINTS = "2.5.29.19"

_ECDSA_SIGNATURE_HASHES = {
    _OID_ECDSA_WITH_SHA256: "sha256",
    _OID_ECDSA_WITH_SHA384: "sha384",
}

# DigestInfo DER prefix for SHA-256 in EMSA-PKCS1-v1_5 (RFC 8017, section 9.2, note 1).
_SHA256_DIGEST_INFO_PREFIX = (
    b"\x30\x31\x30\x0d\x06\x09\x60\x86\x48\x01\x65\x03\x04\x02\x01\x05\x00\x04\x20"
)

# DER tags (universal class unless noted).
_TAG_BOOLEAN = 0x01
_TAG_INTEGER = 0x02
_TAG_BIT_STRING = 0x03
_TAG_OCTET_STRING = 0x04
_TAG_NULL = 0x05
_TAG_OID = 0x06
_TAG_UTC_TIME = 0x17
_TAG_GENERALIZED_TIME = 0x18
_TAG_SEQUENCE = 0x30
_TAG_EXPLICIT_0 = 0xA0  # TBSCertificate.version
_TAG_IMPLICIT_1 = 0x81  # TBSCertificate.issuerUniqueID
_TAG_IMPLICIT_2 = 0x82  # TBSCertificate.subjectUniqueID
_TAG_EXPLICIT_3 = 0xA3  # TBSCertificate.extensions


class JwsError(ValueError):
    """A JWS, JWK or certificate failed parsing or verification.

    The message is generic on purpose: it names what failed, never the data.
    """


# ---------------------------------------------------------------------------
# Base64url and compact JWS
# ---------------------------------------------------------------------------


def b64url_decode(data: str) -> bytes:
    """Decode base64url (RFC 4648, section 5). Padding is optional."""
    if not isinstance(data, str):
        raise JwsError("base64url data must be a string")
    if "+" in data or "/" in data:
        raise JwsError("data is not base64url encoded")
    standard = data.replace("-", "+").replace("_", "/")
    if "=" not in standard:
        if len(standard) % 4 == 1:
            raise JwsError("base64url data has an invalid length")
        standard += "=" * (-len(standard) % 4)
    try:
        return base64.b64decode(standard, validate=True)
    except ValueError as exc:
        raise JwsError("data is not valid base64url") from exc


def b64url_encode(data: bytes) -> str:
    """Encode bytes as base64url without padding."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64_standard_decode(data: object) -> bytes:
    """Decode standard (padded) base64, as used by the JWS ``x5c`` header."""
    if not isinstance(data, str) or not data:
        raise JwsError("x5c entry must be a non-empty base64 string")
    try:
        return base64.b64decode(data, validate=True)
    except ValueError as exc:
        raise JwsError("x5c entry is not valid base64") from exc


def _reject_json_constant(name: str) -> NoReturn:
    raise JwsError("non-finite JSON numbers are not allowed")


def _decode_json_object(segment: str, label: str) -> dict[str, Any]:
    raw = b64url_decode(segment)
    try:
        value = json.loads(raw.decode("utf-8"), parse_constant=_reject_json_constant)
    except (ValueError, RecursionError) as exc:
        raise JwsError(f"JWS {label} is not valid JSON") from exc
    if not isinstance(value, dict):
        raise JwsError(f"JWS {label} must be a JSON object")
    return value


@dataclass(frozen=True)
class JwsParts:
    """A structurally valid compact JWS whose signature is NOT yet verified."""

    header: dict[str, Any]
    payload: dict[str, Any]
    signing_input: bytes
    signature: bytes


def parse_compact(token: str) -> JwsParts:
    """Split and decode a compact JWS without verifying its signature.

    Rejects anything that is not exactly three non-empty segments, headers and
    payloads that are not JSON objects, a missing or ``none`` algorithm, and any
    ``crit`` header (no JWS extensions are understood here).
    """
    if not isinstance(token, str):
        raise JwsError("JWS must be a string")
    if len(token) > MAX_JWS_LENGTH:
        raise JwsError("JWS is too large")
    segments = token.split(".")
    if len(segments) != 3 or not all(segments):
        raise JwsError("JWS must have three non-empty segments")
    header_b64, payload_b64, signature_b64 = segments
    header = _decode_json_object(header_b64, "header")
    alg = header.get("alg")
    if not isinstance(alg, str) or not alg.strip() or alg.strip().lower() == "none":
        raise JwsError("JWS algorithm is missing or not allowed")
    if "crit" in header:
        raise JwsError("JWS critical header extensions are not supported")
    payload = _decode_json_object(payload_b64, "payload")
    signature = b64url_decode(signature_b64)
    if not signature:
        raise JwsError("JWS signature is empty")
    signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
    return JwsParts(
        header=header, payload=payload, signing_input=signing_input, signature=signature
    )


# ---------------------------------------------------------------------------
# Elliptic curves (short Weierstrass, a = -3)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _CurveParams:
    name: str
    p: int
    a: int
    b: int
    gx: int
    gy: int
    n: int
    bits: int

    @property
    def byte_len(self) -> int:
        return (self.bits + 7) // 8


# Domain parameters copied from RFC 5903, section 3 (cross-checked against
# RFC 5114, sections 2.6 and 2.7). Grouped in 32-bit words exactly as printed
# there. _ensure_curve_self_check() proves every constant consistent at first use.
_P256 = _CurveParams(
    name="P-256",
    p=0xFFFFFFFF_00000001_00000000_00000000_00000000_FFFFFFFF_FFFFFFFF_FFFFFFFF,
    a=0xFFFFFFFF_00000001_00000000_00000000_00000000_FFFFFFFF_FFFFFFFF_FFFFFFFC,
    b=0x5AC635D8_AA3A93E7_B3EBBD55_769886BC_651D06B0_CC53B0F6_3BCE3C3E_27D2604B,
    gx=0x6B17D1F2_E12C4247_F8BCE6E5_63A440F2_77037D81_2DEB33A0_F4A13945_D898C296,
    gy=0x4FE342E2_FE1A7F9B_8EE7EB4A_7C0F9E16_2BCE3357_6B315ECE_CBB64068_37BF51F5,
    n=0xFFFFFFFF_00000000_FFFFFFFF_FFFFFFFF_BCE6FAAD_A7179E84_F3B9CAC2_FC632551,
    bits=256,
)

_P384 = _CurveParams(
    name="P-384",
    p=0xFFFFFFFF_FFFFFFFF_FFFFFFFF_FFFFFFFF_FFFFFFFF_FFFFFFFF_FFFFFFFF_FFFFFFFE_FFFFFFFF_00000000_00000000_FFFFFFFF,
    a=0xFFFFFFFF_FFFFFFFF_FFFFFFFF_FFFFFFFF_FFFFFFFF_FFFFFFFF_FFFFFFFF_FFFFFFFE_FFFFFFFF_00000000_00000000_FFFFFFFC,
    b=0xB3312FA7_E23EE7E4_988E056B_E3F82D19_181D9C6E_FE814112_0314088F_5013875A_C656398D_8A2ED19D_2A85C8ED_D3EC2AEF,
    gx=0xAA87CA22_BE8B0537_8EB1C71E_F320AD74_6E1D3B62_8BA79B98_59F741E0_82542A38_5502F25D_BF55296C_3A545E38_72760AB7,
    gy=0x3617DE4A_96262C6F_5D9E98BF_9292DC29_F8F41DBD_289A147C_E9DA3113_B5F0B8C0_0A60B1CE_1D7E819D_7A431D7C_90EA0E5F,
    n=0xFFFFFFFF_FFFFFFFF_FFFFFFFF_FFFFFFFF_FFFFFFFF_FFFFFFFF_C7634D81_F4372DDF_581A0DB2_48B0A77A_ECEC196A_CCC52973,
    bits=384,
)

_CURVES: dict[str, _CurveParams] = {_P256.name: _P256, _P384.name: _P384}
_CURVE_BY_OID: dict[str, str] = {_OID_PRIME256V1: _P256.name, _OID_SECP384R1: _P384.name}

# The field primes are generalized Mersenne primes; their closed forms are
# printed in RFC 5903 and give an independent check on the hex literals.
_FIELD_PRIME_FORMULAS: dict[str, int] = {
    _P256.name: 2**256 - 2**224 + 2**192 + 2**96 - 1,
    _P384.name: 2**384 - 2**128 - 2**96 + 2**32 - 1,
}

# An affine point, or None for the point at infinity.
_Point = tuple[int, int] | None

_curve_self_check_passed = False


def _on_curve(curve: _CurveParams, x: int, y: int) -> bool:
    p = curve.p
    if not (0 <= x < p and 0 <= y < p):
        return False
    return (y * y - (x * x * x + curve.a * x + curve.b)) % p == 0


def _point_add(curve: _CurveParams, first: _Point, second: _Point) -> _Point:
    """Add two affine points. Handles infinity, doubling and P + (-P)."""
    if first is None:
        return second
    if second is None:
        return first
    p = curve.p
    x1, y1 = first
    x2, y2 = second
    if x1 == x2:
        if (y1 + y2) % p == 0:
            # P + (-P), which also covers doubling a point with y == 0.
            return None
        slope = (3 * x1 * x1 + curve.a) * pow((2 * y1) % p, -1, p) % p
    else:
        slope = (y2 - y1) * pow((x2 - x1) % p, -1, p) % p
    x3 = (slope * slope - x1 - x2) % p
    y3 = (slope * (x1 - x3) - y1) % p
    return (x3, y3)


def _double_scalar_mult(curve: _CurveParams, k1: int, p1: _Point, k2: int, p2: _Point) -> _Point:
    """Compute k1*P1 + k2*P2 with Shamir's trick (left-to-right double-and-add).

    Scalars are used as given, not reduced mod n, so the self-check can prove
    that n*G is the point at infinity.
    """
    both = _point_add(curve, p1, p2)
    result: _Point = None
    for bit in range(max(k1.bit_length(), k2.bit_length()) - 1, -1, -1):
        result = _point_add(curve, result, result)
        first_set = (k1 >> bit) & 1
        second_set = (k2 >> bit) & 1
        if first_set and second_set:
            result = _point_add(curve, result, both)
        elif first_set:
            result = _point_add(curve, result, p1)
        elif second_set:
            result = _point_add(curve, result, p2)
    return result


def _ensure_curve_self_check() -> None:
    """Prove the curve constants are mutually consistent, once per process.

    Each constant is covered: p by its closed form, a == p - 3, b/Gx/Gy by
    G lying on the curve, and n by n*G being the point at infinity. A typo in
    any literal raises RuntimeError at first use instead of silently accepting
    or rejecting signatures.
    """
    global _curve_self_check_passed
    if _curve_self_check_passed:
        return
    for curve in (_P256, _P384):
        if curve.p != _FIELD_PRIME_FORMULAS[curve.name]:
            raise RuntimeError(f"{curve.name}: field prime constant is corrupt")
        if curve.a != curve.p - 3:
            raise RuntimeError(f"{curve.name}: coefficient a constant is corrupt")
        if curve.p.bit_length() != curve.bits or curve.n.bit_length() != curve.bits:
            raise RuntimeError(f"{curve.name}: constant has the wrong bit length")
        if not _on_curve(curve, curve.gx, curve.gy):
            raise RuntimeError(f"{curve.name}: generator is not on the curve")
        generator = (curve.gx, curve.gy)
        if _double_scalar_mult(curve, curve.n, generator, 0, generator) is not None:
            raise RuntimeError(f"{curve.name}: group order constant is corrupt")
    _curve_self_check_passed = True


def _curve(name: object) -> _CurveParams:
    curve = _CURVES.get(name) if isinstance(name, str) else None
    if curve is None:
        raise JwsError("unsupported elliptic curve")
    _ensure_curve_self_check()
    return curve


def _digest(hash_name: str, data: bytes) -> bytes:
    if hash_name == "sha256":
        return hashlib.sha256(data).digest()
    if hash_name == "sha384":
        return hashlib.sha384(data).digest()
    raise JwsError("unsupported hash algorithm")


# ---------------------------------------------------------------------------
# Public keys
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EcPublicKey:
    """An EC public key on P-256 or P-384. Construction validates the point."""

    curve: str
    x: int
    y: int

    def __post_init__(self) -> None:
        params = _curve(self.curve)
        if not _on_curve(params, self.x, self.y):
            raise JwsError("EC public key is not a point on its curve")


@dataclass(frozen=True)
class RsaPublicKey:
    """An RSA public key. Construction enforces size and exponent bounds."""

    n: int
    e: int

    def __post_init__(self) -> None:
        bits = self.n.bit_length()
        if bits < _RSA_MIN_BITS:
            raise JwsError("RSA modulus must be at least 2048 bits")
        if bits > _RSA_MAX_BITS or self.n <= 0 or self.n % 2 == 0:
            raise JwsError("RSA modulus is not supported")
        if (
            self.e < 3
            or self.e % 2 == 0
            or self.e.bit_length() > _RSA_MAX_EXPONENT_BITS
            or self.e >= self.n
        ):
            raise JwsError("RSA public exponent is not supported")


def _jwk_bytes(jwk: Mapping[str, Any], member: str) -> bytes:
    value = jwk.get(member)
    if not isinstance(value, str) or not value:
        raise JwsError(f"JWK member {member!r} is missing")
    decoded = b64url_decode(value)
    if not decoded:
        raise JwsError(f"JWK member {member!r} is empty")
    return decoded


def ec_public_key_from_jwk(jwk: Mapping[str, Any]) -> EcPublicKey:
    """Build an EC public key from a JWK (RFC 7518, section 6.2)."""
    if not isinstance(jwk, Mapping) or jwk.get("kty") != "EC":
        raise JwsError("JWK is not an EC key")
    if "d" in jwk:
        raise JwsError("JWK contains private key material")
    crv = jwk.get("crv")
    params = _curve(crv)
    x_raw = _jwk_bytes(jwk, "x")
    y_raw = _jwk_bytes(jwk, "y")
    if len(x_raw) != params.byte_len or len(y_raw) != params.byte_len:
        raise JwsError("JWK EC coordinates have the wrong length")
    return EcPublicKey(
        curve=params.name,
        x=int.from_bytes(x_raw, "big"),
        y=int.from_bytes(y_raw, "big"),
    )


def rsa_public_key_from_jwk(jwk: Mapping[str, Any]) -> RsaPublicKey:
    """Build an RSA public key from a JWK (RFC 7518, section 6.3)."""
    if not isinstance(jwk, Mapping) or jwk.get("kty") != "RSA":
        raise JwsError("JWK is not an RSA key")
    if "d" in jwk:
        raise JwsError("JWK contains private key material")
    return RsaPublicKey(
        n=int.from_bytes(_jwk_bytes(jwk, "n"), "big"),
        e=int.from_bytes(_jwk_bytes(jwk, "e"), "big"),
    )


# ---------------------------------------------------------------------------
# Signature verification primitives
# ---------------------------------------------------------------------------


def verify_ecdsa(key: EcPublicKey, message: bytes, r: int, s: int, *, hash_name: str) -> bool:
    """Verify an ECDSA signature (SEC 1 v2, section 4.1.4) over ``message``.

    ``hash_name`` is ``"sha256"`` or ``"sha384"`` and is independent of the
    curve. The digest is truncated to the bit length of n when it is longer.
    """
    curve = _curve(key.curve)
    digest = _digest(hash_name, message)
    n = curve.n
    if not (1 <= r < n and 1 <= s < n):
        return False
    if not _on_curve(curve, key.x, key.y):
        return False
    e = int.from_bytes(digest, "big")
    excess_bits = len(digest) * 8 - n.bit_length()
    if excess_bits > 0:
        e >>= excess_bits
    w = pow(s, -1, n)
    u1 = (e * w) % n
    u2 = (r * w) % n
    point = _double_scalar_mult(curve, u1, (curve.gx, curve.gy), u2, (key.x, key.y))
    if point is None:
        return False
    return point[0] % n == r


def verify_es256(key: EcPublicKey, signing_input: bytes, raw_signature: bytes) -> bool:
    """Verify a JWS ES256 signature: raw 64-byte r||s, P-256, SHA-256."""
    if key.curve != _P256.name or len(raw_signature) != 2 * _P256.byte_len:
        return False
    r = int.from_bytes(raw_signature[: _P256.byte_len], "big")
    s = int.from_bytes(raw_signature[_P256.byte_len :], "big")
    return verify_ecdsa(key, signing_input, r, s, hash_name="sha256")


def _verify_rsa_pkcs1_v15_sha256(key: RsaPublicKey, message: bytes, signature: bytes) -> bool:
    """RSASSA-PKCS1-v1_5 verification with SHA-256 (RFC 8017, section 8.2.2).

    The expected encoded message EM is rebuilt and compared in full, instead of
    parsing the decrypted block, which avoids the classic lax-parser forgeries.
    """
    k = (key.n.bit_length() + 7) // 8
    if len(signature) != k:
        return False
    s = int.from_bytes(signature, "big")
    if s >= key.n:
        return False
    encoded = pow(s, key.e, key.n).to_bytes(k, "big")
    digest_info = _SHA256_DIGEST_INFO_PREFIX + hashlib.sha256(message).digest()
    padding_len = k - len(digest_info) - 3
    if padding_len < 8:
        return False
    expected = b"\x00\x01" + b"\xff" * padding_len + b"\x00" + digest_info
    return hmac.compare_digest(encoded, expected)


def verify_rs256(key: RsaPublicKey, signing_input: bytes, signature: bytes) -> bool:
    """Verify a JWS RS256 signature (RSASSA-PKCS1-v1_5 with SHA-256)."""
    return _verify_rsa_pkcs1_v15_sha256(key, signing_input, signature)


def verify_hs256(key: bytes, signing_input: bytes, signature: bytes) -> bool:
    """Verify a JWS HS256 signature (HMAC-SHA-256). An empty key never verifies."""
    if not key:
        return False
    expected = hmac.new(key, signing_input, hashlib.sha256).digest()
    return hmac.compare_digest(expected, signature)


# ---------------------------------------------------------------------------
# Minimal DER reader
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _DerElement:
    buf: bytes
    tag: int
    start: int
    content_start: int
    end: int

    @property
    def content(self) -> bytes:
        return self.buf[self.content_start : self.end]

    @property
    def raw(self) -> bytes:
        """The full TLV encoding: tag, length and contents."""
        return self.buf[self.start : self.end]

    def reader(self) -> _DerReader:
        return _DerReader(self.buf, self.content_start, self.end)


class _DerReader:
    """Sequential, bounds-checked DER reader (definite lengths only)."""

    def __init__(self, buf: bytes, start: int = 0, end: int | None = None) -> None:
        self._buf = buf
        self._pos = start
        self._end = len(buf) if end is None else end

    def at_end(self) -> bool:
        return self._pos >= self._end

    def peek_tag(self) -> int | None:
        return None if self.at_end() else self._buf[self._pos]

    def read(self, expected_tag: int | None = None) -> _DerElement:
        buf = self._buf
        start = self._pos
        end = self._end
        if start + 2 > end:
            raise JwsError("DER: truncated element")
        tag = buf[start]
        if tag & 0x1F == 0x1F:
            raise JwsError("DER: multi-byte tags are not supported")
        if expected_tag is not None and tag != expected_tag:
            raise JwsError("DER: unexpected element")
        first = buf[start + 1]
        cursor = start + 2
        if first < 0x80:
            length = first
        elif first == 0x80:
            raise JwsError("DER: indefinite lengths are not allowed")
        else:
            count = first & 0x7F
            if count > 4:
                raise JwsError("DER: length field is too long")
            if cursor + count > end:
                raise JwsError("DER: truncated length")
            length_bytes = buf[cursor : cursor + count]
            if length_bytes[0] == 0:
                raise JwsError("DER: length is not minimally encoded")
            length = int.from_bytes(length_bytes, "big")
            if length < 0x80:
                raise JwsError("DER: length is not minimally encoded")
            cursor += count
        if cursor + length > end:
            raise JwsError("DER: element overruns its container")
        self._pos = cursor + length
        return _DerElement(buf=buf, tag=tag, start=start, content_start=cursor, end=cursor + length)

    def finish(self) -> None:
        if not self.at_end():
            raise JwsError("DER: unexpected trailing data")


def _der_single(data: bytes, tag: int) -> _DerElement:
    """Read exactly one element of ``tag`` spanning all of ``data``."""
    reader = _DerReader(data)
    element = reader.read(tag)
    reader.finish()
    return element


def _der_integer(element: _DerElement) -> int:
    content = element.content
    if not content:
        raise JwsError("DER: empty INTEGER")
    if len(content) > 1 and (
        (content[0] == 0x00 and content[1] < 0x80) or (content[0] == 0xFF and content[1] >= 0x80)
    ):
        raise JwsError("DER: INTEGER is not minimally encoded")
    return int.from_bytes(content, "big", signed=True)


def _der_bit_string_bytes(element: _DerElement) -> bytes:
    content = element.content
    if not content or content[0] != 0:
        raise JwsError("DER: BIT STRING with unused bits is not supported")
    return content[1:]


def _der_oid(element: _DerElement) -> str:
    content = element.content
    if not content or content[-1] & 0x80:
        raise JwsError("DER: malformed OBJECT IDENTIFIER")
    arcs: list[int] = []
    value = 0
    at_arc_start = True
    for byte in content:
        if at_arc_start and byte == 0x80:
            raise JwsError("DER: OBJECT IDENTIFIER arc is not minimally encoded")
        value = (value << 7) | (byte & 0x7F)
        at_arc_start = not (byte & 0x80)
        if at_arc_start:
            arcs.append(value)
            value = 0
    first = arcs[0]
    if first < 40:
        head = [0, first]
    elif first < 80:
        head = [1, first - 40]
    else:
        head = [2, first - 80]
    return ".".join(str(arc) for arc in [*head, *arcs[1:]])


def _der_time(element: _DerElement) -> datetime:
    try:
        text = element.content.decode("ascii")
    except UnicodeDecodeError as exc:
        raise JwsError("DER: time is not ASCII") from exc
    if element.tag == _TAG_UTC_TIME:
        # YYMMDDHHMMSSZ; RFC 5280 section 4.1.2.5.1: YY < 50 means 20YY.
        if len(text) != 13 or text[12] != "Z" or not text[:12].isdigit():
            raise JwsError("DER: malformed UTCTime")
        short_year = int(text[0:2])
        year = 2000 + short_year if short_year < 50 else 1900 + short_year
        rest = text[2:12]
    elif element.tag == _TAG_GENERALIZED_TIME:
        # YYYYMMDDHHMMSSZ; RFC 5280 section 4.1.2.5.2 forbids fractions.
        if len(text) != 15 or text[14] != "Z" or not text[:14].isdigit():
            raise JwsError("DER: malformed GeneralizedTime")
        year = int(text[0:4])
        rest = text[4:14]
    else:
        raise JwsError("DER: unexpected time type")
    try:
        return datetime(
            year,
            int(rest[0:2]),
            int(rest[2:4]),
            int(rest[4:6]),
            int(rest[6:8]),
            int(rest[8:10]),
            tzinfo=UTC,
        )
    except ValueError as exc:
        raise JwsError("DER: time is out of range") from exc


def _der_algorithm_oid(element: _DerElement) -> str:
    """Return the OID of an AlgorithmIdentifier, allowing one parameters element."""
    reader = element.reader()
    oid = _der_oid(reader.read(_TAG_OID))
    if not reader.at_end():
        reader.read()
    reader.finish()
    return oid


def _ec_key_from_point(curve_name: str, point: bytes) -> EcPublicKey:
    params = _curve(curve_name)
    size = params.byte_len
    if len(point) != 1 + 2 * size or point[0] != 0x04:
        raise JwsError("EC public key must be an uncompressed point")
    return EcPublicKey(
        curve=params.name,
        x=int.from_bytes(point[1 : 1 + size], "big"),
        y=int.from_bytes(point[1 + size :], "big"),
    )


def _parse_spki(element: _DerElement) -> EcPublicKey | RsaPublicKey:
    reader = element.reader()
    algorithm = reader.read(_TAG_SEQUENCE).reader()
    key_bits = _der_bit_string_bytes(reader.read(_TAG_BIT_STRING))
    reader.finish()
    algorithm_oid = _der_oid(algorithm.read(_TAG_OID))
    if algorithm_oid == _OID_EC_PUBLIC_KEY:
        curve_name = _CURVE_BY_OID.get(_der_oid(algorithm.read(_TAG_OID)))
        algorithm.finish()
        if curve_name is None:
            raise JwsError("certificate EC curve is not supported")
        return _ec_key_from_point(curve_name, key_bits)
    if algorithm_oid == _OID_RSA_ENCRYPTION:
        if not algorithm.at_end() and algorithm.read(_TAG_NULL).content:
            raise JwsError("DER: malformed RSA algorithm parameters")
        algorithm.finish()
        fields = _der_single(key_bits, _TAG_SEQUENCE).reader()
        modulus = _der_integer(fields.read(_TAG_INTEGER))
        exponent = _der_integer(fields.read(_TAG_INTEGER))
        fields.finish()
        return RsaPublicKey(n=modulus, e=exponent)
    raise JwsError("certificate public key algorithm is not supported")


def _parse_basic_constraints(value: bytes) -> bool:
    """Return the cA flag of a BasicConstraints extension value."""
    fields = _der_single(value, _TAG_SEQUENCE).reader()
    is_ca = False
    if fields.peek_tag() == _TAG_BOOLEAN:
        flag = fields.read(_TAG_BOOLEAN).content
        if len(flag) != 1:
            raise JwsError("DER: malformed BOOLEAN")
        is_ca = flag[0] != 0
    if fields.peek_tag() == _TAG_INTEGER:
        fields.read(_TAG_INTEGER)  # pathLenConstraint, not enforced
    fields.finish()
    return is_ca


def _parse_extensions(element: _DerElement) -> tuple[frozenset[str], bool]:
    outer = element.reader()
    extensions = outer.read(_TAG_SEQUENCE).reader()
    outer.finish()
    oids: set[str] = set()
    is_ca = False
    while not extensions.at_end():
        extension = extensions.read(_TAG_SEQUENCE).reader()
        oid = _der_oid(extension.read(_TAG_OID))
        if extension.peek_tag() == _TAG_BOOLEAN:
            extension.read(_TAG_BOOLEAN)  # critical flag
        value = extension.read(_TAG_OCTET_STRING).content
        extension.finish()
        if oid in oids:
            raise JwsError("certificate repeats an extension")
        oids.add(oid)
        if oid == _OID_BASIC_CONSTRAINTS:
            is_ca = _parse_basic_constraints(value)
    return frozenset(oids), is_ca


# ---------------------------------------------------------------------------
# X.509 certificates
# ---------------------------------------------------------------------------


def _colon_hex(data: bytes) -> str:
    return ":".join(f"{byte:02X}" for byte in data)


@dataclass(frozen=True)
class Certificate:
    """The parts of an X.509 v3 certificate needed for chain verification.

    ``tbs_der``, ``issuer_der`` and ``subject_der`` are full DER TLVs.
    ``is_ca`` is the BasicConstraints cA flag (False when the extension is absent).
    """

    der: bytes
    tbs_der: bytes
    signature_algorithm: str
    signature: bytes
    issuer_der: bytes
    subject_der: bytes
    not_before: datetime
    not_after: datetime
    public_key: EcPublicKey | RsaPublicKey
    extension_oids: frozenset[str]
    is_ca: bool = False

    @property
    def sha256_fingerprint(self) -> str:
        return _colon_hex(hashlib.sha256(self.der).digest())


def parse_certificate(der: bytes) -> Certificate:
    """Parse a DER X.509 certificate (RFC 5280, section 4.1). Does not verify it."""
    if not isinstance(der, bytes | bytearray):
        raise JwsError("certificate must be DER bytes")
    data = bytes(der)
    if not data or len(data) > _MAX_CERTIFICATE_BYTES:
        raise JwsError("certificate size is out of bounds")
    body = _der_single(data, _TAG_SEQUENCE).reader()
    tbs = body.read(_TAG_SEQUENCE)
    outer_algorithm = body.read(_TAG_SEQUENCE)
    signature = _der_bit_string_bytes(body.read(_TAG_BIT_STRING))
    body.finish()
    signature_algorithm = _der_algorithm_oid(outer_algorithm)

    fields = tbs.reader()
    if fields.peek_tag() == _TAG_EXPLICIT_0:
        version_reader = fields.read(_TAG_EXPLICIT_0).reader()
        version = _der_integer(version_reader.read(_TAG_INTEGER))
        version_reader.finish()
        if version not in (0, 1, 2):
            raise JwsError("certificate version is not supported")
    fields.read(_TAG_INTEGER)  # serialNumber
    inner_algorithm = fields.read(_TAG_SEQUENCE)
    if inner_algorithm.raw != outer_algorithm.raw:
        raise JwsError("certificate signature algorithms disagree")
    issuer = fields.read(_TAG_SEQUENCE)
    validity = fields.read(_TAG_SEQUENCE).reader()
    not_before = _der_time(validity.read())
    not_after = _der_time(validity.read())
    validity.finish()
    subject = fields.read(_TAG_SEQUENCE)
    public_key = _parse_spki(fields.read(_TAG_SEQUENCE))

    extension_oids: frozenset[str] = frozenset()
    is_ca = False
    seen_extensions = False
    while not fields.at_end():
        element = fields.read()
        if element.tag in (_TAG_IMPLICIT_1, _TAG_IMPLICIT_2) and not seen_extensions:
            continue
        if element.tag == _TAG_EXPLICIT_3 and not seen_extensions:
            extension_oids, is_ca = _parse_extensions(element)
            seen_extensions = True
            continue
        raise JwsError("DER: unexpected TBSCertificate field")

    return Certificate(
        der=data,
        tbs_der=tbs.raw,
        signature_algorithm=signature_algorithm,
        signature=signature,
        issuer_der=issuer.raw,
        subject_der=subject.raw,
        not_before=not_before,
        not_after=not_after,
        public_key=public_key,
        extension_oids=extension_oids,
        is_ca=is_ca,
    )


def verify_certificate_signature(cert: Certificate, issuer_key: EcPublicKey | RsaPublicKey) -> bool:
    """Verify ``cert``'s signature over its TBSCertificate under ``issuer_key``.

    The hash comes from the certificate's signatureAlgorithm; the curve comes
    from the issuer key. X.509 ECDSA signatures are DER ``SEQUENCE{r, s}``,
    unlike the raw r||s form used by JWS.
    """
    hash_name = _ECDSA_SIGNATURE_HASHES.get(cert.signature_algorithm)
    if hash_name is not None:
        if not isinstance(issuer_key, EcPublicKey):
            return False
        fields = _der_single(cert.signature, _TAG_SEQUENCE).reader()
        r = _der_integer(fields.read(_TAG_INTEGER))
        s = _der_integer(fields.read(_TAG_INTEGER))
        fields.finish()
        return verify_ecdsa(issuer_key, cert.tbs_der, r, s, hash_name=hash_name)
    if cert.signature_algorithm == _OID_SHA256_WITH_RSA:
        if not isinstance(issuer_key, RsaPublicKey):
            return False
        return _verify_rsa_pkcs1_v15_sha256(issuer_key, cert.tbs_der, cert.signature)
    raise JwsError("certificate signature algorithm is not supported")


def normalize_fingerprint(value: str) -> str:
    """Normalize a SHA-256 fingerprint to uppercase colon-separated hex."""
    if not isinstance(value, str):
        raise JwsError("fingerprint must be a string")
    compact = "".join(value.split()).replace(":", "").upper()
    if len(compact) != 64 or not set(compact) <= _HEX_DIGITS:
        raise JwsError("fingerprint must be a SHA-256 hex digest")
    return ":".join(compact[index : index + 2] for index in range(0, 64, 2))


def verify_x5c_chain(
    x5c: Sequence[str],
    *,
    trusted_root_fingerprints: Iterable[str],
    at: datetime,
    leaf_oid: str | None = APPLE_LEAF_OID,
    intermediate_oid: str | None = APPLE_INTERMEDIATE_OID,
) -> EcPublicKey:
    """Validate an ``x5c`` chain (leaf first) and return the leaf's P-256 key.

    The root must match a pinned SHA-256 fingerprint and be correctly
    self-signed. Signatures are then checked top-down, so every key used has
    already been authenticated. Each certificate must be valid at ``at``, each
    issuer must be a CA, and issuer/subject names must chain. ``leaf_oid`` must
    be present on the leaf and ``intermediate_oid`` on the certificate that
    issued the leaf (pass None to skip either marker check).
    """
    if isinstance(x5c, str | bytes) or not isinstance(x5c, Sequence):
        raise JwsError("x5c must be a list of certificates")
    if not _MIN_CHAIN_LENGTH <= len(x5c) <= _MAX_CHAIN_LENGTH:
        raise JwsError("x5c must contain between 2 and 4 certificates")
    trusted = frozenset(normalize_fingerprint(item) for item in trusted_root_fingerprints)
    if not trusted:
        raise JwsError("no trusted root fingerprints are configured")
    if not isinstance(at, datetime) or at.tzinfo is None or at.utcoffset() is None:
        raise JwsError("chain validation time must be timezone-aware")

    certs = [parse_certificate(_b64_standard_decode(item)) for item in x5c]
    root = certs[-1]
    if root.sha256_fingerprint not in trusted:
        raise JwsError("x5c chain does not end at a trusted root")
    for cert in certs:
        if not (cert.not_before <= at <= cert.not_after):
            raise JwsError("x5c certificate is not valid at the verification time")
    if root.issuer_der != root.subject_der or not verify_certificate_signature(
        root, root.public_key
    ):
        raise JwsError("trusted root is not correctly self-signed")
    for index in range(len(certs) - 2, -1, -1):
        child = certs[index]
        issuer = certs[index + 1]
        if not issuer.is_ca:
            raise JwsError("x5c issuer is not a CA certificate")
        if child.issuer_der != issuer.subject_der:
            raise JwsError("x5c certificates are not chained in order")
        if not verify_certificate_signature(child, issuer.public_key):
            raise JwsError("x5c certificate signature is invalid")

    leaf = certs[0]
    if leaf_oid is not None and leaf_oid not in leaf.extension_oids:
        raise JwsError("x5c leaf certificate is missing the required marker extension")
    if intermediate_oid is not None and (
        len(certs) < 3 or intermediate_oid not in certs[1].extension_oids
    ):
        raise JwsError("x5c intermediate certificate is missing the required marker extension")
    leaf_key = leaf.public_key
    if not isinstance(leaf_key, EcPublicKey) or leaf_key.curve != _P256.name:
        raise JwsError("x5c leaf key must be EC P-256")
    return leaf_key


# ---------------------------------------------------------------------------
# StoreKit (App Store Server) JWS verifiers
# ---------------------------------------------------------------------------


def _finite_number(value: object) -> float | None:
    """Return ``value`` as a finite float; None for bools, non-numbers, NaN or inf."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) else None


def _chain_validation_time(payload: Mapping[str, Any]) -> datetime:
    """Use the payload's ``signedDate`` (epoch ms) when sane, else now (UTC).

    Apple re-delivers notifications and clients upload old transactions, so the
    chain is judged at signing time. The leaf signature must still verify, so
    an attacker cannot exploit this without an Apple leaf private key.
    """
    now = time.time()
    signed_ms = _finite_number(payload.get("signedDate"))
    if signed_ms is not None:
        signed_seconds = signed_ms / 1000.0
        if _MIN_SANE_SIGNED_DATE <= signed_seconds <= now + _MAX_SIGNED_DATE_FUTURE_SECONDS:
            try:
                return datetime.fromtimestamp(signed_seconds, tz=UTC)
            except (OverflowError, OSError, ValueError):
                return datetime.fromtimestamp(now, tz=UTC)
    return datetime.fromtimestamp(now, tz=UTC)


class StoreKitVerifier(Protocol):
    """Verifies an App Store Server signed payload and returns its claims."""

    def verify(self, signed_payload: str) -> dict[str, Any]:
        """Return the verified payload claims or raise :class:`JwsError`."""
        ...


class AppleStoreKitVerifier:
    """Production StoreKit verifier: ES256 under an x5c chain to a pinned root."""

    def __init__(
        self, trusted_root_fingerprints: Iterable[str] = DEFAULT_APPLE_ROOT_FINGERPRINTS
    ) -> None:
        roots = frozenset(normalize_fingerprint(item) for item in trusted_root_fingerprints)
        if not roots:
            raise JwsError("at least one trusted root fingerprint is required")
        self._trusted_roots = roots

    def verify(self, signed_payload: str) -> dict[str, Any]:
        parts = parse_compact(signed_payload)
        if parts.header.get("alg") != "ES256":
            raise JwsError("StoreKit payload must be signed with ES256")
        x5c = parts.header.get("x5c")
        if not isinstance(x5c, list) or not all(isinstance(item, str) for item in x5c):
            raise JwsError("StoreKit payload has no valid x5c certificate chain")
        leaf_key = verify_x5c_chain(
            x5c,
            trusted_root_fingerprints=self._trusted_roots,
            at=_chain_validation_time(parts.payload),
        )
        if not verify_es256(leaf_key, parts.signing_input, parts.signature):
            raise JwsError("StoreKit payload signature is invalid")
        return dict(parts.payload)


class StaticKeyStoreKitVerifier:
    """Test verifier: ES256 against an injected P-256 key, no x5c handling."""

    def __init__(self, key: EcPublicKey) -> None:
        if key.curve != _P256.name:
            raise JwsError("ES256 requires a P-256 key")
        self._key = key

    def verify(self, signed_payload: str) -> dict[str, Any]:
        parts = parse_compact(signed_payload)
        if parts.header.get("alg") != "ES256":
            raise JwsError("StoreKit payload must be signed with ES256")
        if not verify_es256(self._key, parts.signing_input, parts.signature):
            raise JwsError("StoreKit payload signature is invalid")
        return dict(parts.payload)


class HmacStoreKitVerifier:
    """Test verifier: HS256 against an injected shared key."""

    def __init__(self, key: bytes) -> None:
        if not isinstance(key, bytes) or not key:
            raise JwsError("HMAC key must be non-empty bytes")
        self._key = key

    def verify(self, signed_payload: str) -> dict[str, Any]:
        parts = parse_compact(signed_payload)
        if parts.header.get("alg") != "HS256":
            raise JwsError("payload must be signed with HS256")
        if not verify_hs256(self._key, parts.signing_input, parts.signature):
            raise JwsError("payload signature is invalid")
        return dict(parts.payload)


# ---------------------------------------------------------------------------
# Sign in with Apple identity tokens
# ---------------------------------------------------------------------------


class AppleJwsVerifier(Protocol):
    """Verifies a Sign in with Apple identity token and returns its claims."""

    async def verify_identity_token(
        self, identity_token: str, *, audiences: Sequence[str]
    ) -> dict[str, Any]:
        """Return verified claims or raise :class:`JwsError`."""
        ...


def validate_identity_claims(
    claims: Mapping[str, Any], *, audiences: Sequence[str], now: float
) -> dict[str, Any]:
    """Check iss, aud, exp, iat and sub of identity token claims.

    Call this only after the signature has been verified.
    """
    if not isinstance(claims, Mapping):
        raise JwsError("identity token claims must be an object")
    current = _finite_number(now)
    if current is None:
        raise JwsError("validation time must be a finite number")
    if claims.get("iss") != APPLE_ISSUER:
        raise JwsError("identity token issuer is not Apple")

    configured = [audiences] if isinstance(audiences, str) else list(audiences)
    allowed = {item for item in configured if isinstance(item, str) and item}
    if not allowed:
        raise JwsError("no accepted identity token audiences are configured")
    aud = claims.get("aud")
    presented: set[str]
    if isinstance(aud, str):
        presented = {aud}
    elif isinstance(aud, list) and all(isinstance(item, str) for item in aud):
        presented = set(aud)
    else:
        raise JwsError("identity token audience is missing or malformed")
    if not presented & allowed:
        raise JwsError("identity token audience is not accepted")

    exp = _finite_number(claims.get("exp"))
    if exp is None:
        raise JwsError("identity token has no valid expiry")
    if exp <= current - _CLOCK_SKEW_SECONDS:
        raise JwsError("identity token has expired")
    if "iat" in claims:
        iat = _finite_number(claims.get("iat"))
        if iat is None:
            raise JwsError("identity token issue time is malformed")
        if iat > current + _MAX_IAT_FUTURE_SECONDS:
            raise JwsError("identity token issue time is in the future")

    sub = claims.get("sub")
    if not isinstance(sub, str) or not sub.strip():
        raise JwsError("identity token has no subject")
    return dict(claims)


_JwkSet = tuple[Mapping[str, Any], ...]


def _jwks_keys(jwks: object) -> _JwkSet:
    if not isinstance(jwks, Mapping):
        raise JwsError("JWKS document must be a JSON object")
    keys = jwks.get("keys")
    if not isinstance(keys, list):
        raise JwsError("JWKS document has no keys array")
    return tuple(item for item in keys if isinstance(item, Mapping))


def _identity_token_kid(header: Mapping[str, Any]) -> str:
    alg = header.get("alg")
    if not isinstance(alg, str) or alg not in _IDENTITY_ALGORITHMS:
        raise JwsError("identity token algorithm is not supported")
    kid = header.get("kid")
    if not isinstance(kid, str) or not kid:
        raise JwsError("identity token header has no key id")
    return kid


def _find_jwk(keys: _JwkSet, kid: str) -> Mapping[str, Any] | None:
    for jwk in keys:
        if jwk.get("kid") == kid:
            return jwk
    return None


def _verify_with_jwk(parts: JwsParts, jwk: Mapping[str, Any]) -> None:
    """Verify the signature, binding the header alg to the key type."""
    alg = parts.header.get("alg")
    declared_alg = jwk.get("alg")
    if declared_alg is not None and declared_alg != alg:
        raise JwsError("identity token algorithm does not match its key")
    use = jwk.get("use")
    if use is not None and use != "sig":
        raise JwsError("JWKS key is not a signature key")
    if alg == "RS256":
        valid = verify_rs256(rsa_public_key_from_jwk(jwk), parts.signing_input, parts.signature)
    elif alg == "ES256":
        valid = verify_es256(ec_public_key_from_jwk(jwk), parts.signing_input, parts.signature)
    else:
        raise JwsError("identity token algorithm is not supported")
    if not valid:
        raise JwsError("identity token signature is invalid")


class AppleJwksVerifier:
    """Production identity token verifier backed by Apple's JWKS endpoint.

    Keys are cached for ``cache_seconds``. An unknown ``kid`` triggers exactly
    one refetch per verification. After the cache expires, refresh attempts are
    rate limited, and if a refresh fails while older keys are cached, the cached
    keys stay in use.
    """

    def __init__(
        self,
        jwks_url: str = "https://appleid.apple.com/auth/keys",
        *,
        cache_seconds: float = 3600.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        seconds = _finite_number(cache_seconds)
        if seconds is None or seconds < 0:
            raise ValueError("cache_seconds must be a non-negative number")
        self._jwks_url = jwks_url
        self._cache_seconds = seconds
        self._min_refetch_seconds = min(_JWKS_MIN_REFETCH_SECONDS, seconds)
        self._transport = transport
        self._keys: _JwkSet | None = None
        self._fetched_at: float | None = None
        self._last_attempt: float | None = None

    async def verify_identity_token(
        self, identity_token: str, *, audiences: Sequence[str]
    ) -> dict[str, Any]:
        parts = parse_compact(identity_token)
        kid = _identity_token_kid(parts.header)
        keys, fetched_now = await self._current_keys()
        jwk = _find_jwk(keys, kid)
        if jwk is None and not fetched_now:
            # Key rotation: refetch exactly once. This is not throttled, so a
            # flood of unknown kids costs one outbound fetch per request.
            jwk = _find_jwk(await self._refresh(), kid)
        if jwk is None:
            raise JwsError("identity token is signed by an unknown key")
        _verify_with_jwk(parts, jwk)
        return validate_identity_claims(parts.payload, audiences=audiences, now=time.time())

    def _may_attempt_fetch(self, now: float) -> bool:
        return self._last_attempt is None or now - self._last_attempt >= self._min_refetch_seconds

    async def _current_keys(self) -> tuple[_JwkSet, bool]:
        now = time.monotonic()
        if self._keys is not None:
            fresh = self._fetched_at is not None and now - self._fetched_at < self._cache_seconds
            if fresh or not self._may_attempt_fetch(now):
                return self._keys, False
        return await self._refresh(), True

    async def _refresh(self) -> _JwkSet:
        self._last_attempt = time.monotonic()
        try:
            keys = await self._fetch()
        except JwsError:
            if self._keys is not None:
                return self._keys
            raise
        self._keys = keys
        self._fetched_at = time.monotonic()
        return keys

    async def _fetch(self) -> _JwkSet:
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(10.0, connect=5.0), transport=self._transport
            ) as client:
                response = await client.get(self._jwks_url, headers={"Accept": "application/json"})
        except httpx.HTTPError as exc:
            raise JwsError("unable to fetch Apple signing keys") from exc
        if response.status_code != 200:
            raise JwsError("Apple signing keys endpoint returned an error")
        body = response.content
        if len(body) > _MAX_JWKS_BYTES:
            raise JwsError("Apple signing keys document is too large")
        try:
            document = json.loads(body, parse_constant=_reject_json_constant)
        except (ValueError, RecursionError) as exc:
            raise JwsError("Apple signing keys document is not valid JSON") from exc
        return _jwks_keys(document)


class StaticJwksVerifier:
    """Test identity token verifier over an injected JWKS ``{"keys": [...]}``."""

    def __init__(self, jwks: Mapping[str, Any]) -> None:
        self._keys = _jwks_keys(jwks)

    async def verify_identity_token(
        self, identity_token: str, *, audiences: Sequence[str]
    ) -> dict[str, Any]:
        parts = parse_compact(identity_token)
        kid = _identity_token_kid(parts.header)
        jwk = _find_jwk(self._keys, kid)
        if jwk is None:
            raise JwsError("identity token is signed by an unknown key")
        _verify_with_jwk(parts, jwk)
        return validate_identity_claims(parts.payload, audiences=audiences, now=time.time())
