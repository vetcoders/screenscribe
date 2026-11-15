"""Cloud portal SSR, EN/PL language negotiation, CSP, zero-BYOK audit, healthz/readyz, and CLI entrypoint."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from screenscribe.cloud.__main__ import build_parser, main as cloud_main, run_init_db
from screenscribe.cloud.app import create_cloud_app
from screenscribe.cloud.config import CloudSettings
from screenscribe.cloud.portal import SIGNATURE
from tests.cloud_test_helpers import auth_payload, make_cloud_settings


def test_portal_bilingual_negotiation_csp_zero_byok_and_health(tmp_path: Path) -> None:
    settings = make_cloud_settings(tmp_path / "storage")
    app = create_cloud_app(settings)

    byok_pattern = re.compile(r"api[_-]?key|libraxis_api_key|openai_api_key|byok", re.IGNORECASE)

    with TestClient(app) as client:
        # Health and readiness probes
        hz = client.get("/healthz")
        assert hz.status_code == 200
        assert hz.json() == {"status": "ok"}

        rz = client.get("/readyz")
        assert rz.status_code == 200
        assert rz.json()["status"] == "ready"
        assert rz.json()["checks"]["ai_capacity"] is True

        # Static assets
        css = client.get("/static/portal.css")
        js = client.get("/static/portal.js")
        assert css.status_code == 200
        assert js.status_code == 200
        assert "innerHTML" not in js.text

        # Anonymous home in English + Accept-Language Polish + ?lang=pl cookie persistence
        home_en = client.get("/")
        assert home_en.status_code == 200
        assert '<html lang="en">' in home_en.text
        assert "script-src 'self'" in home_en.headers["Content-Security-Policy"]
        assert SIGNATURE in home_en.text or "LibraxisAI" in home_en.text
        assert not byok_pattern.search(home_en.text)

        home_accept_pl = client.get("/", headers={"Accept-Language": "pl-PL,pl;q=0.9,en;q=0.8"})
        assert '<html lang="pl">' in home_accept_pl.text

        login_pl = client.get("/login?lang=pl")
        assert login_pl.status_code == 200
        assert '<html lang="pl">' in login_pl.text
        assert not byok_pattern.search(login_pl.text)

        # Unauthenticated protected portal routes redirect to /login
        for protected in ("/dashboard", "/upload", "/billing", "/account", "/activate"):
            redir = client.get(protected, follow_redirects=False)
            assert redir.status_code == 303
            assert redir.headers["location"].startswith("/login?")

        # Sign in via form POST and inspect all authenticated portal pages in both PL and EN
        form_reg = client.post(
            "/api/v1/auth/register",
            data=auth_payload("portal@example.com", "portal-member-phrase", display_name="Portal"),
            headers={
                "Origin": "http://testserver",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            follow_redirects=False,
        )
        assert form_reg.status_code == 303

        for lang in ("pl", "en"):
            for route in ("/dashboard", "/upload", "/billing", "/account", "/activate?user_code=ABCD-EFGH"):
                sep = "&" if "?" in route else "?"
                resp = client.get(f"{route}{sep}lang={lang}")
                assert resp.status_code == 200
                assert f'<html lang="{lang}">' in resp.text
                assert not byok_pattern.search(resp.text)


def test_cloud_settings_from_env_and_cli_entrypoint(tmp_path: Path, monkeypatch: Any) -> None:
    storage = tmp_path / "cli_storage"
    env = {
        "SCREENSCRIBE_CLOUD_STORAGE_DIR": str(storage),
        "SCREENSCRIBE_CLOUD_BASE_URL": "https://app.screenscribe.vetcoders.io",
        "SCREENSCRIBE_CLOUD_PLAN_MINUTES": "starter=90,pro=400",
        "SCREENSCRIBE_CLOUD_STRIPE_PRICE_PRO": "price_pro_live",
        "SCREENSCRIBE_CLOUD_DEFAULT_LANG": "pl",
    }
    cfg = CloudSettings.from_env(env)
    assert cfg.secure_cookies is True
    assert cfg.minutes_for_plan("starter") == 90.0
    assert cfg.plan_for_price("price_pro_live") == "pro"
    assert cfg.default_ui_language == "pl"

    parser = build_parser(cfg)
    parsed = parser.parse_args(["serve", "--port", "9099"])
    assert parsed.command == "serve"
    assert parsed.port == 9099

    assert run_init_db(cfg) == 0
    assert cfg.database_file.is_file()

    monkeypatch.setenv("SCREENSCRIBE_CLOUD_STORAGE_DIR", str(storage))
    assert cloud_main(["init-db"]) == 0
