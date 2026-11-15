"""Bilingual (EN + PL) customer portal, server-rendered, zero third-party assets.

Pages: ``/``, ``/login``, ``/dashboard``, ``/upload``, ``/billing``,
``/activate`` (RFC 8628 device approval). Scripts and styles are served from
``/static/portal.js`` and ``/static/portal.css`` under a strict
``script-src 'self'`` policy; the script only uses ``textContent`` /
``createElement`` DOM APIs.
"""

from __future__ import annotations

import html
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from .auth import AuthContext, CloudServices, enforce_csrf, get_services, me_body, read_form_or_json, resolve_auth
from .i18n import LANG_COOKIE, LANG_COOKIE_MAX_AGE, SUPPORTED_LANGUAGES, negotiate_language, normalize_language, translate
from .jobs import job_status_body
from .publish import review_summary
from .schemas import JOB_PRESETS
from .security import CSRF_COOKIE, CSRF_FORM_FIELD, new_csrf_value, normalize_user_code, safe_next_path

PORTAL_CSP = "; ".join(
    (
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self'",
        "img-src 'self' data:",
        "connect-src 'self'",
        "font-src 'self'",
        "object-src 'none'",
        "base-uri 'none'",
        "form-action 'self'",
        "frame-ancestors 'none'",
    )
)
STATIC_CACHE = "public, max-age=3600"
# Canonical signature; escapes keep the source ASCII.
SIGNATURE = (
    "\U0001d685\U0001d692\U0001d68b\U0001d68e\U0001d68c\U0001d69b\U0001d68a\U0001d68f"
    "\U0001d69d\U0001d68e\U0001d68d. with AI Agents by Vetcoders (c)2024-2026 LibraxisAI"
)
_ACTIVE_JOB_STATUSES = frozenset({"queued", "processing"})


def _e(value: object) -> str:
    return html.escape("" if value is None else str(value), quote=True)


# -- Page context ------------------------------------------------------------


@dataclass
class Page:
    request: Request
    services: CloudServices
    lang: str
    auth: AuthContext | None
    csrf: str
    csrf_is_new: bool
    lang_from_query: str | None

    def t(self, key: str, **params: object) -> str:
        return _e(translate(self.lang, key, **params))


async def build_page(request: Request) -> Page:
    services = get_services(request)
    auth = await resolve_auth(request)
    query_lang = normalize_language(request.query_params.get("lang"))
    lang = negotiate_language(
        query=query_lang,
        cookie=request.cookies.get(LANG_COOKIE),
        preferred=auth.user.preferred_lang if auth is not None else None,
        accept_language=request.headers.get("accept-language"),
        default=services.settings.default_ui_language,
    )
    existing = request.cookies.get(CSRF_COOKIE)
    return Page(
        request=request,
        services=services,
        lang=lang,
        auth=auth,
        csrf=existing or new_csrf_value(),
        csrf_is_new=not existing,
        lang_from_query=query_lang,
    )


def _nav(page: Page) -> str:
    links = [("/", "nav.home")]
    if page.auth is not None:
        links += [
            ("/dashboard", "nav.dashboard"),
            ("/upload", "nav.upload"),
            ("/billing", "nav.billing"),
            ("/account", "nav.account"),
        ]
    current = page.request.url.path
    current_marker = ' aria-current="page"'
    items = "".join(
        f'<a href="{href}"{current_marker if href == current else ""}>{page.t(key)}</a>'
        for href, key in links
    )
    other = next(lang for lang in SUPPORTED_LANGUAGES if lang != page.lang)
    switch = f'<a class="lang" href="{_e(current)}?{urlencode({"lang": other})}" hreflang="{other}">{page.t("lang.switch")}</a>'
    if page.auth is not None:
        account = (
            '<form method="post" action="/api/v1/auth/logout" class="inline">'
            f'<input type="hidden" name="{CSRF_FORM_FIELD}" value="{_e(page.csrf)}">'
            f'<button type="submit" class="link">{page.t("nav.logout")}</button></form>'
        )
    else:
        account = f'<a href="/login">{page.t("nav.login")}</a>'
    return (
        f'<header class="top"><a class="brand" href="/">{page.t("brand")}</a>'
        f'<nav>{items}</nav><div class="account">{switch}{account}</div></header>'
    )


def render(page: Page, title_key: str, body: str, *, status_code: int = 200) -> HTMLResponse:
    document = (
        f'<!doctype html><html lang="{page.lang}"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="color-scheme" content="dark">'
        f"<title>{page.t(title_key)} | {page.t('brand')}</title>"
        '<link rel="stylesheet" href="/static/portal.css">'
        '<script src="/static/portal.js" defer></script>'
        f"</head><body>{_nav(page)}<main>{body}</main>"
        f'<footer><p>{page.t("footer.privacy")}</p><p class="sig">{_e(SIGNATURE)}</p></footer>'
        "</body></html>"
    )
    response = HTMLResponse(
        content=document,
        status_code=status_code,
        headers={
            "Content-Security-Policy": PORTAL_CSP,
            "Cache-Control": "no-store",
            "Referrer-Policy": "same-origin",
        },
    )
    settings = page.services.settings
    if page.csrf_is_new:
        response.set_cookie(
            CSRF_COOKIE,
            page.csrf,
            max_age=settings.web_session_ttl_seconds,
            path="/",
            httponly=False,
            samesite="lax",
            secure=settings.secure_cookies,
        )
    if page.lang_from_query is not None:
        response.set_cookie(
            LANG_COOKIE,
            page.lang_from_query,
            max_age=LANG_COOKIE_MAX_AGE,
            path="/",
            httponly=True,
            samesite="lax",
            secure=settings.secure_cookies,
        )
    return response


def login_redirect(request: Request) -> RedirectResponse:
    target = request.url.path
    if request.url.query:
        target = f"{target}?{request.url.query}"
    return RedirectResponse(url=f"/login?{urlencode({'next': target})}", status_code=303)


def _time(value: str | None) -> str:
    if not value:
        return "-"
    return _e(value.replace("T", " ").rstrip("Z")[:16])


def _minutes(value: Any) -> str:
    try:
        return f"{float(value):g}"
    except (TypeError, ValueError):
        return "0"


# -- Page bodies -------------------------------------------------------------


def home_body(page: Page) -> str:
    settings = page.services.settings
    steps = "".join(
        f'<li><h3>{page.t(f"home.step{i}.title")}</h3><p>{page.t(f"home.step{i}.body")}</p></li>'
        for i in (1, 2, 3)
    )
    plans = "".join(
        f'<li><strong>{_e(plan.title())}</strong><span>{page.t("home.plan.minutes", minutes=_minutes(minutes))}</span></li>'
        for plan, minutes in settings.plan_minutes.items()
        if minutes > 0
    )
    if page.auth is not None:
        cta = f'<a class="button" href="/dashboard">{page.t("home.cta.dashboard")}</a>'
    else:
        cta = f'<a class="button" href="/login#register">{page.t("home.cta.start")}</a>'
    return (
        f'<section class="hero"><h1>{page.t("home.title")}</h1>'
        f'<p class="lead">{page.t("tagline")}</p><p>{page.t("home.lead")}</p><p>{cta}</p></section>'
        f'<section><ol class="steps">{steps}</ol></section>'
        f'<section><h2>{page.t("home.pricing")}</h2><ul class="plans">{plans}</ul>'
        f'<p class="muted">{page.t("home.privacy")}</p></section>'
    )


def login_body(page: Page) -> str:
    request = page.request
    next_path = request.query_params.get("next") or "/dashboard"
    error = request.query_params.get("error")
    notice = ""
    if error in {"invalid_credentials", "email_taken", "register_invalid"}:
        notice = f'<p class="alert" role="alert">{page.t(f"error.{error}")}</p>'
    hidden_next = f'<input type="hidden" name="next" value="{_e(next_path)}">'
    return (
        f"{notice}"
        '<div class="grid two">'
        f'<section class="card"><h1>{page.t("login.title")}</h1>'
        '<form method="post" action="/api/v1/auth/login">'
        f"{hidden_next}"
        f'<label>{page.t("login.email")}<input type="email" name="email" autocomplete="email" required></label>'
        f'<label>{page.t("login.password")}<input type="password" name="password" autocomplete="current-password" required></label>'
        f'<button type="submit">{page.t("login.submit")}</button></form></section>'
        f'<section class="card" id="register"><h2>{page.t("register.title")}</h2>'
        '<form method="post" action="/api/v1/auth/register">'
        f"{hidden_next}"
        f'<input type="hidden" name="preferred_lang" value="{page.lang}">'
        f'<label>{page.t("register.name")}<input type="text" name="display_name" autocomplete="name" maxlength="120"></label>'
        f'<label>{page.t("login.email")}<input type="email" name="email" autocomplete="email" required></label>'
        f'<label>{page.t("login.password")}<input type="password" name="password" autocomplete="new-password" minlength="8" required>'
        f'<small>{page.t("register.password_hint")}</small></label>'
        f'<button type="submit">{page.t("register.submit")}</button></form></section></div>'
    )


def subscription_card(page: Page, subscription: dict[str, Any]) -> str:
    if not subscription.get("active"):
        return (
            f'<section class="card"><h2>{page.t("dashboard.subscription")}</h2>'
            f'<p>{page.t("dashboard.inactive")}</p>'
            f'<p><a class="button" href="/billing">{page.t("billing.choose")}</a></p></section>'
        )
    rows = (
        ("dashboard.plan", _e(str(subscription.get("plan", "")).title())),
        ("dashboard.source", _e(str(subscription.get("source", "")).replace("_", " "))),
        ("dashboard.renews", _time(subscription.get("period_end"))),
        ("dashboard.minutes", f'{_minutes(subscription.get("minutes_used"))} / {_minutes(subscription.get("minutes_limit"))}'),
        ("dashboard.remaining", _minutes(subscription.get("minutes_remaining"))),
    )
    cells = "".join(f"<dt>{page.t(key)}</dt><dd>{value}</dd>" for key, value in rows)
    return f'<section class="card"><h2>{page.t("dashboard.subscription")}</h2><dl>{cells}</dl></section>'


def jobs_table(page: Page, jobs: list[dict[str, Any]]) -> str:
    if not jobs:
        return f'<p class="muted">{page.t("dashboard.jobs.empty")}</p>'
    rows = []
    for job in jobs:
        status = str(job.get("status") or "")
        active = ' data-job-active="1"' if status in _ACTIVE_JOB_STATUSES else ""
        if job.get("share_url"):
            outcome = f'<a href="{_e(job["share_url"])}">{page.t("dashboard.open")}</a>'
        elif job.get("error"):
            outcome = f'<span class="muted">{_e(job["error"])}</span>'
        else:
            outcome = ""
        rows.append(
            f"<tr{active}><td>{_time(job.get('created_at'))}</td>"
            f"<td>{page.t(f'status.{status}') if status else ''}</td>"
            f"<td>{_e(job.get('progress_pct', 0))}%</td><td>{outcome}</td></tr>"
        )
    return (
        f'<table><thead><tr><th>{page.t("col.created")}</th><th>{page.t("col.status")}</th>'
        f'<th>{page.t("col.progress")}</th><th>{page.t("col.actions")}</th></tr></thead>'
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def reviews_table(page: Page, reviews: list[dict[str, Any]]) -> str:
    if not reviews:
        return f'<p class="muted">{page.t("dashboard.reviews.empty")}</p>'
    rows = []
    for review in reviews:
        share = _e(review["share_id"])
        visibility = str(review.get("visibility") or "private")
        rows.append(
            f'<tr data-review-row="{share}"><td><a href="{_e(review["share_url"])}">{_e(review.get("title") or review.get("video_name"))}</a></td>'
            f"<td>{page.t(f'visibility.{visibility}')}</td><td>{_time(review.get('created_at'))}</td>"
            f'<td><button type="button" class="danger" data-delete-review="{share}" '
            f'data-confirm="{page.t("dashboard.delete.confirm")}">{page.t("dashboard.delete")}</button></td></tr>'
        )
    return (
        f'<table><thead><tr><th>{page.t("col.title")}</th><th>{page.t("col.visibility")}</th>'
        f'<th>{page.t("col.created")}</th><th>{page.t("col.actions")}</th></tr></thead>'
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def dashboard_data(services: CloudServices, auth: AuthContext) -> dict[str, Any]:
    user = auth.user
    return {
        "me": me_body(services, user),
        "jobs": [job_status_body(services, job) | {"created_at": job.created_at} for job in services.store.list_jobs(user.user_id, limit=20)],
        "reviews": [review_summary(services, record) for record in services.store.list_reviews(user.user_id, limit=50)],
    }


def dashboard_body(page: Page, data: dict[str, Any]) -> str:
    return (
        f'<h1>{page.t("dashboard.title")}</h1>'
        f'{subscription_card(page, data["me"]["subscription"])}'
        f'<section class="card"><h2>{page.t("dashboard.jobs")}</h2>{jobs_table(page, data["jobs"])}'
        f'<p><a class="button" href="/upload">{page.t("nav.upload")}</a></p></section>'
        f'<section class="card"><h2>{page.t("dashboard.reviews")}</h2>{reviews_table(page, data["reviews"])}</section>'
    )


def _options(page: Page, prefix: str, values: tuple[str, ...], selected: str) -> str:
    return "".join(
        f'<option value="{value}"{" selected" if value == selected else ""}>{page.t(f"{prefix}.{value}")}</option>'
        for value in values
    )


def upload_body(page: Page, subscription: dict[str, Any]) -> str:
    notice = ""
    if not subscription.get("active"):
        notice = (
            f'<p class="alert">{page.t("upload.needs_plan")} '
            f'<a href="/billing">{page.t("billing.choose")}</a></p>'
        )
    messages = " ".join(
        f'data-msg-{name}="{page.t(f"upload.{name}")}"'
        for name in ("uploading", "queued", "done", "failed", "needs_plan", "too_large", "error")
    )
    return (
        f'<h1>{page.t("upload.title")}</h1>{notice}'
        f'<form id="upload-form" class="card" {messages} data-billing-url="/billing" '
        f'data-billing-label="{page.t("billing.choose")}" data-open-label="{page.t("dashboard.open")}">'
        f'<label>{page.t("upload.file")}<input type="file" name="file" accept="video/*" required></label>'
        f'<label>{page.t("upload.language")}<select name="language">'
        f'{_options(page, "upload.language", ("auto", "en", "pl"), "auto")}</select></label>'
        f'<label>{page.t("upload.preset")}<select name="preset">'
        f'{_options(page, "upload.preset", JOB_PRESETS, "programming")}</select></label>'
        f'<label>{page.t("upload.visibility")}<select name="visibility">'
        f'{_options(page, "visibility", ("private", "unlisted", "team"), "private")}</select></label>'
        f'<label>{page.t("upload.prompt")}<textarea name="prompt" rows="3" maxlength="4000"></textarea></label>'
        f'<button type="submit">{page.t("upload.submit")}</button>'
        '<progress id="upload-progress" max="100" value="0" hidden></progress>'
        '<p id="upload-status" role="status" aria-live="polite"></p>'
        '<p id="upload-result"></p></form>'
    )


def billing_body(page: Page, auth: AuthContext, subscription: dict[str, Any]) -> str:
    settings = page.services.settings
    state = page.request.query_params.get("checkout")
    notice = ""
    if state == "success":
        notice = f'<p class="ok" role="status">{page.t("billing.success")}</p>'
    elif state == "cancel":
        notice = f'<p class="muted" role="status">{page.t("billing.cancelled")}</p>'
    source = str(subscription.get("source") or "none")
    if subscription.get("active"):
        current = (
            f'<p>{page.t("billing.current")}: <strong>{_e(str(subscription.get("plan", "")).title())}</strong>'
            f' | {page.t("dashboard.renews")}: {_time(subscription.get("period_end"))}</p>'
        )
    else:
        current = f'<p>{page.t("billing.none")}</p>'
    if source == "app_store":
        current += f'<p class="muted">{page.t("billing.appstore")}</p>'
    actions = ""
    if not settings.stripe_enabled:
        actions = f'<p class="muted">{page.t("billing.unconfigured")}</p>'
    else:
        if auth.user.stripe_customer_id:
            actions += f'<p><button type="button" data-billing-portal="1">{page.t("billing.manage")}</button></p>'
        if not subscription.get("active") or source == "stripe":
            plans = "".join(
                f'<li><strong>{_e(plan.title())}</strong>'
                f'<span>{page.t("home.plan.minutes", minutes=_minutes(settings.minutes_for_plan(plan)))}</span>'
                f'<button type="button" data-checkout-plan="{_e(plan)}">{page.t("billing.subscribe")}</button></li>'
                for plan in settings.stripe_prices
            )
            if plans:
                actions += f'<h2>{page.t("billing.choose")}</h2><ul class="plans">{plans}</ul>'
    return (
        f'<h1>{page.t("billing.title")}</h1>{notice}'
        f'<section class="card" id="billing" data-error="{page.t("billing.error")}">{current}{actions}'
        '<p id="billing-status" role="status" aria-live="polite"></p></section>'
    )


def account_body(page: Page, auth: AuthContext, subscription: dict[str, Any]) -> str:
    user = auth.user
    plan = (
        _e(str(subscription.get("plan") or "none").title())
        if subscription.get("active")
        else page.t("billing.none")
    )
    return (
        f'<h1>{page.t("account.title")}</h1>'
        '<section class="card"><dl>'
        f'<dt>{page.t("account.email")}</dt><dd>{_e(user.email)}</dd>'
        f'<dt>{page.t("account.created")}</dt><dd>{_time(user.created_at)}</dd>'
        f'<dt>{page.t("billing.current")}</dt><dd>{plan}</dd>'
        "</dl></section>"
        f'<section class="card"><h2>{page.t("account.delete.title")}</h2>'
        f'<p>{page.t("account.delete.body")}</p>'
        f'<form method="post" action="/api/v1/auth/account/delete" data-confirm-submit="{page.t("account.delete.confirm")}">'
        f'<input type="hidden" name="{CSRF_FORM_FIELD}" value="{_e(page.csrf)}">'
        f'<button type="submit" class="danger">{page.t("account.delete.submit")}</button>'
        "</form></section>"
    )


def activate_body(page: Page, *, user_code: str = "", outcome: str | None = None) -> str:
    message = ""
    if outcome in {"approved", "denied", "invalid"}:
        css = "ok" if outcome == "approved" else "alert"
        message = f'<p class="{css}" role="status">{page.t(f"activate.{outcome}")}</p>'
        if outcome != "invalid":
            return f'<section class="card"><h1>{page.t("activate.title")}</h1>{message}</section>'
    return (
        f'<section class="card"><h1>{page.t("activate.title")}</h1>{message}'
        f'<p>{page.t("activate.lead")}</p>'
        '<form method="post" action="/activate">'
        f'<input type="hidden" name="{CSRF_FORM_FIELD}" value="{_e(page.csrf)}">'
        f'<label>{page.t("activate.code")}<input type="text" name="user_code" value="{_e(user_code)}" '
        'autocomplete="one-time-code" autocapitalize="characters" spellcheck="false" '
        'pattern="[A-Za-z0-9]{4}-?[A-Za-z0-9]{4}" maxlength="9" required></label>'
        f'<div class="row"><button type="submit" name="decision" value="approve">{page.t("activate.approve")}</button>'
        f'<button type="submit" name="decision" value="deny" class="secondary">{page.t("activate.deny")}</button></div>'
        "</form></section>"
    )


# -- Static assets -----------------------------------------------------------

PORTAL_CSS = """:root {
  color-scheme: dark;
  --bg: #0b0b0c; --panel: #141416; --line: #2a2a2e; --text: #ececee;
  --muted: #9a9aa2; --accent: #f4f4f5; --danger: #e5484d; --ok: #46a758;
  font-family: ui-sans-serif, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--text); line-height: 1.55; }
a { color: var(--accent); }
header.top { display: flex; gap: 1.5rem; align-items: center; padding: 1rem 1.5rem;
  border-bottom: 1px solid var(--line); flex-wrap: wrap; }
header.top .brand { font-weight: 700; text-decoration: none; letter-spacing: .02em; }
header.top nav { display: flex; gap: 1rem; flex: 1; flex-wrap: wrap; }
header.top nav a { color: var(--muted); text-decoration: none; }
header.top nav a[aria-current="page"], header.top nav a:hover { color: var(--text); }
header.top .account { display: flex; gap: 1rem; align-items: center; }
main { max-width: 64rem; margin: 0 auto; padding: 2rem 1.5rem 3rem; }
h1 { font-size: 1.75rem; margin: 0 0 1rem; }
h2 { font-size: 1.2rem; margin: 0 0 .75rem; }
.hero { padding: 2rem 0 1rem; }
.hero .lead { font-size: 1.2rem; color: var(--text); }
.muted, small { color: var(--muted); }
.card { background: var(--panel); border: 1px solid var(--line); border-radius: 10px;
  padding: 1.25rem 1.5rem; margin: 0 0 1.25rem; }
.grid.two { display: grid; gap: 1.25rem; grid-template-columns: repeat(auto-fit, minmax(18rem, 1fr)); }
.steps { display: grid; gap: 1rem; grid-template-columns: repeat(auto-fit, minmax(14rem, 1fr));
  padding: 0; list-style: none; counter-reset: step; }
.steps li { background: var(--panel); border: 1px solid var(--line); border-radius: 10px; padding: 1rem 1.25rem; }
.steps h3 { margin: 0 0 .25rem; font-size: 1rem; }
.plans { list-style: none; padding: 0; display: grid; gap: .75rem; }
.plans li { display: flex; gap: 1rem; align-items: center; justify-content: space-between;
  border: 1px solid var(--line); border-radius: 8px; padding: .75rem 1rem; flex-wrap: wrap; }
form label { display: block; margin: 0 0 .9rem; font-size: .95rem; }
input, select, textarea { display: block; width: 100%; margin-top: .3rem; padding: .55rem .7rem;
  background: var(--bg); color: var(--text); border: 1px solid var(--line); border-radius: 6px; font: inherit; }
input:focus, select:focus, textarea:focus, button:focus-visible, a:focus-visible {
  outline: 2px solid var(--accent); outline-offset: 2px; }
button, .button { display: inline-block; padding: .55rem 1.1rem; border-radius: 6px; border: 1px solid var(--accent);
  background: var(--accent); color: var(--bg); font: inherit; font-weight: 600; cursor: pointer; text-decoration: none; }
button.secondary { background: transparent; color: var(--text); border-color: var(--line); }
button.danger { background: transparent; color: var(--danger); border-color: var(--danger); padding: .3rem .7rem; }
button.link { background: none; border: none; color: var(--muted); padding: 0; font-weight: 400; }
button.link:hover { color: var(--text); }
button[disabled] { opacity: .5; cursor: progress; }
form.inline { display: inline; margin: 0; }
.row { display: flex; gap: .75rem; flex-wrap: wrap; }
table { width: 100%; border-collapse: collapse; font-size: .95rem; }
th, td { text-align: left; padding: .55rem .5rem; border-bottom: 1px solid var(--line); vertical-align: top; }
th { color: var(--muted); font-weight: 500; }
dl { display: grid; grid-template-columns: max-content 1fr; gap: .35rem 1.25rem; margin: 0; }
dt { color: var(--muted); }
dd { margin: 0; }
progress { width: 100%; height: .6rem; margin-top: .5rem; accent-color: var(--accent); }
.alert { border: 1px solid var(--danger); color: var(--text); padding: .7rem 1rem; border-radius: 8px; }
.ok { border: 1px solid var(--ok); padding: .7rem 1rem; border-radius: 8px; }
footer { border-top: 1px solid var(--line); color: var(--muted); font-size: .85rem;
  padding: 1.25rem 1.5rem; text-align: center; }
footer .sig { margin: .25rem 0 0; }
"""

PORTAL_JS = """(function () {
  "use strict";

  function csrfToken() {
    var match = document.cookie.match(/(?:^|;\\s*)ss_csrf=([^;]+)/);
    return match ? decodeURIComponent(match[1]) : "";
  }

  function newKey() {
    if (window.crypto && typeof window.crypto.randomUUID === "function") {
      return window.crypto.randomUUID();
    }
    return String(Date.now()) + "-" + String(Math.random()).slice(2);
  }

  function setText(node, text) {
    if (node) { node.textContent = text || ""; }
  }

  function clear(node) {
    while (node && node.firstChild) { node.removeChild(node.firstChild); }
  }

  function link(href, label) {
    var anchor = document.createElement("a");
    anchor.href = href;
    anchor.textContent = label;
    return anchor;
  }

  function safeExternal(raw) {
    try {
      var url = new URL(raw, window.location.href);
      return url.protocol === "https:" || url.origin === window.location.origin ? url.href : null;
    } catch (e) {
      return null;
    }
  }

  function postJSON(url, body) {
    return fetch(url, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken() },
      body: JSON.stringify(body || {})
    }).then(function (response) {
      return response.json().catch(function () { return {}; }).then(function (data) {
        return { ok: response.ok, status: response.status, data: data };
      });
    });
  }

  // Destructive form confirmation (account deletion).
  document.addEventListener("submit", function (event) {
    var formEl = event.target;
    if (!(formEl instanceof HTMLFormElement)) { return; }
    var promptText = formEl.getAttribute("data-confirm-submit");
    if (promptText && !window.confirm(promptText)) { event.preventDefault(); }
  });

  // Review deletion (dashboard).
  document.addEventListener("click", function (event) {
    var target = event.target;
    if (!(target instanceof HTMLElement)) { return; }
    var share = target.getAttribute("data-delete-review");
    if (!share) { return; }
    if (!window.confirm(target.getAttribute("data-confirm") || "Delete?")) { return; }
    target.setAttribute("disabled", "disabled");
    fetch("/api/v1/reviews/" + encodeURIComponent(share), {
      method: "DELETE",
      credentials: "same-origin",
      headers: { "X-CSRF-Token": csrfToken() }
    }).then(function (response) {
      if (response.ok) {
        var row = document.querySelector('[data-review-row="' + share.replace(/[^A-Za-z0-9_-]/g, "") + '"]');
        if (row && row.parentNode) { row.parentNode.removeChild(row); }
      } else {
        target.removeAttribute("disabled");
      }
    }).catch(function () { target.removeAttribute("disabled"); });
  });

  // Billing (checkout + customer portal).
  var billing = document.getElementById("billing");
  if (billing) {
    var billingStatus = document.getElementById("billing-status");
    billing.addEventListener("click", function (event) {
      var target = event.target;
      if (!(target instanceof HTMLElement)) { return; }
      var plan = target.getAttribute("data-checkout-plan");
      var portal = target.getAttribute("data-billing-portal");
      if (!plan && !portal) { return; }
      target.setAttribute("disabled", "disabled");
      var request = plan
        ? postJSON("/api/v1/billing/stripe/checkout", { plan: plan })
        : postJSON("/api/v1/billing/stripe/portal", {});
      request.then(function (result) {
        var next = result.ok ? safeExternal(result.data.checkout_url || result.data.portal_url || "") : null;
        if (next) {
          window.location.assign(next);
          return;
        }
        target.removeAttribute("disabled");
        setText(billingStatus, billing.getAttribute("data-error"));
      }).catch(function () {
        target.removeAttribute("disabled");
        setText(billingStatus, billing.getAttribute("data-error"));
      });
    });
  }

  // Upload + live job progress (SSE).
  var form = document.getElementById("upload-form");
  if (form) {
    var progress = document.getElementById("upload-progress");
    var status = document.getElementById("upload-status");
    var result = document.getElementById("upload-result");
    var submit = form.querySelector('button[type="submit"]');
    var msg = function (name) { return form.getAttribute("data-msg-" + name) || ""; };

    var finish = function () { if (submit) { submit.removeAttribute("disabled"); } };

    var follow = function (job) {
      if (!window.EventSource || !job.events_url) {
        setText(status, msg("queued"));
        finish();
        return;
      }
      var source = new EventSource(job.events_url, { withCredentials: true });
      source.addEventListener("job_progress", function (event) {
        try {
          var data = JSON.parse(event.data);
          progress.value = Number(data.progress_pct || 0);
          setText(status, msg("queued") + " " + String(data.stage || "") + " " + String(data.progress_pct || 0) + "%");
        } catch (e) { /* ignore malformed frame */ }
      });
      source.addEventListener("job_completed", function (event) {
        source.close();
        progress.value = 100;
        setText(status, msg("done"));
        clear(result);
        try {
          var data = JSON.parse(event.data);
          var href = safeExternal(data.share_url || "");
          if (href) { result.appendChild(link(href, form.getAttribute("data-open-label") || href)); }
        } catch (e) { /* ignore */ }
        finish();
      });
      source.addEventListener("job_failed", function (event) {
        source.close();
        var detail = "";
        try { detail = String(JSON.parse(event.data).error || ""); } catch (e) { detail = ""; }
        setText(status, msg("failed") + (detail ? " " + detail : ""));
        finish();
      });
    };

    form.addEventListener("submit", function (event) {
      event.preventDefault();
      clear(result);
      var body = new FormData(form);
      var xhr = new XMLHttpRequest();
      xhr.open("POST", "/api/v1/jobs");
      xhr.withCredentials = true;
      xhr.setRequestHeader("X-CSRF-Token", csrfToken());
      xhr.setRequestHeader("Idempotency-Key", newKey());
      xhr.responseType = "json";
      progress.hidden = false;
      progress.value = 0;
      setText(status, msg("uploading"));
      if (submit) { submit.setAttribute("disabled", "disabled"); }
      xhr.upload.addEventListener("progress", function (e) {
        if (e.lengthComputable) { progress.value = Math.round((e.loaded / e.total) * 100); }
      });
      xhr.addEventListener("load", function () {
        var data = xhr.response || {};
        if (xhr.status === 202 || xhr.status === 200) {
          progress.value = 0;
          setText(status, msg("queued"));
          follow(data);
          return;
        }
        finish();
        if (xhr.status === 402) {
          setText(status, msg("needs_plan"));
          result.appendChild(link(form.getAttribute("data-billing-url") || "/billing",
            form.getAttribute("data-billing-label") || "Billing"));
        } else if (xhr.status === 413) {
          setText(status, msg("too_large"));
        } else {
          setText(status, msg("error") + (data && data.detail && typeof data.detail === "string" ? " " + data.detail : ""));
        }
      });
      xhr.addEventListener("error", function () {
        finish();
        setText(status, msg("error"));
      });
      xhr.send(body);
    });
  }

  // Dashboard: refresh while jobs are running.
  if (document.querySelector("[data-job-active]")) {
    window.setTimeout(function () { window.location.reload(); }, 10000);
  }
})();
"""


# -- Router ------------------------------------------------------------------


def build_portal_router() -> APIRouter:
    router = APIRouter(include_in_schema=False)

    @router.get("/", response_class=HTMLResponse)
    async def home(request: Request) -> Response:
        page = await build_page(request)
        return render(page, "home.title", home_body(page))

    @router.get("/login", response_class=HTMLResponse)
    async def login(request: Request) -> Response:
        page = await build_page(request)
        if page.auth is not None and page.auth.via_cookie and not request.query_params.get("error"):
            return RedirectResponse(url=safe_next_path(request.query_params.get("next")), status_code=303)
        return render(page, "login.title", login_body(page))

    @router.get("/dashboard", response_class=HTMLResponse)
    async def dashboard(request: Request) -> Response:
        page = await build_page(request)
        if page.auth is None:
            return login_redirect(request)
        data = await run_in_threadpool(dashboard_data, page.services, page.auth)
        return render(page, "dashboard.title", dashboard_body(page, data))

    @router.get("/upload", response_class=HTMLResponse)
    async def upload(request: Request) -> Response:
        page = await build_page(request)
        if page.auth is None:
            return login_redirect(request)
        me = await run_in_threadpool(me_body, page.services, page.auth.user)
        return render(page, "upload.title", upload_body(page, me["subscription"]))

    @router.get("/billing", response_class=HTMLResponse)
    async def billing(request: Request) -> Response:
        page = await build_page(request)
        if page.auth is None:
            return login_redirect(request)
        me = await run_in_threadpool(me_body, page.services, page.auth.user)
        return render(page, "billing.title", billing_body(page, page.auth, me["subscription"]))

    @router.get("/account", response_class=HTMLResponse)
    async def account(request: Request) -> Response:
        page = await build_page(request)
        if page.auth is None:
            return login_redirect(request)
        me = await run_in_threadpool(me_body, page.services, page.auth.user)
        return render(page, "account.title", account_body(page, page.auth, me["subscription"]))

    @router.get("/activate", response_class=HTMLResponse)
    async def activate(request: Request) -> Response:
        page = await build_page(request)
        if page.auth is None:
            return login_redirect(request)
        code = normalize_user_code(request.query_params.get("user_code", "")) or request.query_params.get(
            "user_code", ""
        )[:9]
        return render(page, "activate.title", activate_body(page, user_code=code))

    @router.post("/activate", response_class=HTMLResponse)
    async def activate_decide(request: Request) -> Response:
        page = await build_page(request)
        if page.auth is None:
            return login_redirect(request)
        if page.auth.via_cookie:
            await enforce_csrf(request)
        data = await read_form_or_json(request)
        code = normalize_user_code(data.get("user_code", ""))
        approve = data.get("decision", "approve") != "deny"
        services = page.services
        user_id = page.auth.user.user_id
        decided = bool(code) and await run_in_threadpool(
            lambda: services.store.decide_device_code(
                code, user_id=user_id, approve=approve, now=services.now()
            )
        )
        if not decided:
            return render(page, "activate.title", activate_body(page, user_code=code, outcome="invalid"), status_code=400)
        outcome = "approved" if approve else "denied"
        return render(page, "activate.title", activate_body(page, outcome=outcome))

    @router.get("/static/portal.css")
    async def portal_css() -> Response:
        return Response(
            content=PORTAL_CSS,
            media_type="text/css",
            headers={"Cache-Control": STATIC_CACHE, "X-Content-Type-Options": "nosniff"},
        )

    @router.get("/static/portal.js")
    async def portal_js() -> Response:
        return Response(
            content=PORTAL_JS,
            media_type="text/javascript",
            headers={"Cache-Control": STATIC_CACHE, "X-Content-Type-Options": "nosniff"},
        )

    return router


__all__ = [
    "PORTAL_CSP",
    "PORTAL_CSS",
    "PORTAL_JS",
    "SIGNATURE",
    "Page",
    "account_body",
    "activate_body",
    "billing_body",
    "build_page",
    "build_portal_router",
    "dashboard_body",
    "dashboard_data",
    "home_body",
    "login_body",
    "login_redirect",
    "render",
    "upload_body",
]
