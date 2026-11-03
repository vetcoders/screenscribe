"""Offline CA-bundle regression for GHSA-6q48-6c7w-9gw2; no TLS requests."""

from __future__ import annotations

import ssl
import tomllib
from pathlib import Path

import certifi
import httpx
import pytest
from packaging.requirements import Requirement
from packaging.version import Version

REPO_ROOT = Path(__file__).resolve().parents[1]
COMPROMISED_ROOT = "emSign ECC Root CA - G3"
LEGITIMATE_ROOT = "ISRG Root X1"


def test_runtime_dependency_excludes_affected_certifi_for_unlocked_installs() -> None:
    manifest = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    requirements = [Requirement(value) for value in manifest["project"]["dependencies"]]
    certifi_requirements = [value for value in requirements if value.name == "certifi"]
    assert len(certifi_requirements) == 1, "Publish the certifi floor as a runtime dependency"
    requirement = certifi_requirements[0]
    assert requirement.marker is None, "The trust-store floor must cover every supported platform"
    assert Version("2026.7.22") not in requirement.specifier
    assert Version("2026.10.18") not in requirement.specifier
    assert Version("2026.10.19") in requirement.specifier

    lock = tomllib.loads((REPO_ROOT / "uv.lock").read_text(encoding="utf-8"))
    locked = next(package for package in lock["package"] if package["name"] == "certifi")
    assert Version(locked["version"]) in requirement.specifier


@pytest.fixture(params=["certifi", "httpx"])
def default_tls_context(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> ssl.SSLContext:
    # Inspect the shipped default bundle, independently of an operator's custom
    # CA configuration. monkeypatch restores both settings after each test.
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("SSL_CERT_DIR", raising=False)
    if request.param == "httpx":
        return httpx.create_ssl_context()
    return ssl.create_default_context(cafile=certifi.where())


def _trusted_common_names(context: ssl.SSLContext) -> set[str]:
    return {
        value
        for certificate in context.get_ca_certs()
        for rdn in certificate.get("subject", ())
        for key, value in rdn
        if key == "commonName"
    }


def test_compromised_emsign_root_is_not_trusted(default_tls_context: ssl.SSLContext) -> None:
    assert COMPROMISED_ROOT not in _trusted_common_names(default_tls_context)


def test_default_tls_keeps_certificate_hostname_and_legitimate_ca_verification(
    default_tls_context: ssl.SSLContext,
) -> None:
    assert default_tls_context.verify_mode == ssl.CERT_REQUIRED
    assert default_tls_context.check_hostname is True
    assert LEGITIMATE_ROOT in _trusted_common_names(default_tls_context)
