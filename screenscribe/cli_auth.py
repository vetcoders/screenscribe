"""``screenscribe auth`` command group: account (OAuth) sign-in per provider.

Kept out of ``cli.py`` so the main module only registers the sub-app. Nothing
here prints a token: the verification URL + short user code are the only
secrets-adjacent output, and both are meant for the user's eyes.

``cloud`` is the Screenscribe Cloud subscription (``screenscribe publish``);
``openai`` / ``xai`` are provider accounts used instead of API keys.
"""

from __future__ import annotations

import webbrowser
from datetime import UTC, datetime
from typing import Annotated

import typer
from rich.console import Console
from rich.markup import escape

from .account_auth import (
    PROVIDERS,
    AccountAuthError,
    account_status,
    accounts_path,
    clear_account_tokens,
)
from .account_auth.cloud import (
    CLOUD_PROVIDER,
    CloudStatus,
    clear_cloud_session,
    cloud_status,
    complete_cloud_login,
    load_cloud_session,
    request_cloud_device_code,
)
from .account_auth.device_code import complete_device_code_login, request_device_code

console = Console()

auth_app = typer.Typer(
    name="auth",
    help=(
        "Sign in to Screenscribe Cloud (publish) or to a provider account (OAuth device "
        "flow) as an alternative to API keys."
    ),
    add_completion=False,
    no_args_is_help=True,
)

_PROVIDER_HELP = "Provider: " + " | ".join(sorted({*PROVIDERS, CLOUD_PROVIDER}))


def _provider_or_exit(provider: str) -> str:
    normalized = provider.lower().strip()
    if normalized != CLOUD_PROVIDER and normalized not in PROVIDERS:
        console.print(f"[red]Error:[/] unknown provider {provider!r}. {_PROVIDER_HELP}.")
        raise typer.Exit(2)
    return normalized


def _fmt_expiry(expires_at: int | None) -> str:
    if expires_at is None:
        return "no expiry reported"
    stamp = datetime.fromtimestamp(expires_at, tz=UTC).strftime("%Y-%m-%d %H:%M UTC")
    return (
        f"expires {stamp}" if expires_at > datetime.now(tz=UTC).timestamp() else f"EXPIRED {stamp}"
    )


def _cloud_login(cloud_url: str | None, *, no_browser: bool) -> None:
    """``auth login cloud``: device flow against the resolved Cloud host."""
    try:
        device = request_cloud_device_code(cloud_url)
    except AccountAuthError as error:
        console.print(f"[red]Sign-in failed ({error.kind}):[/] {escape(str(error))}")
        raise typer.Exit(2 if error.kind == "config" else 1) from None

    console.print(f"[bold]Open:[/] {escape(device.verification_url)}")
    console.print(f"[bold]Enter code:[/] [cyan]{escape(device.user_code)}[/]")
    if not no_browser:
        try:
            webbrowser.open(device.verification_url)
        except (webbrowser.Error, OSError):  # browser launch is best-effort
            console.print("[dim]Could not open a browser; use the URL above.[/]")
    console.print(f"[dim]Waiting for approval on {escape(device.host)} (Ctrl+C to abort)...[/]")

    try:
        session = complete_cloud_login(device)
    except AccountAuthError as error:
        console.print(f"[red]Sign-in failed ({error.kind}):[/] {escape(str(error))}")
        raise typer.Exit(1) from None
    except KeyboardInterrupt:
        console.print("[yellow]Aborted.[/] Nothing was stored.")
        raise typer.Exit(130) from None

    who = f" as {session.email}" if session.email else ""
    console.print(f"[green]Signed in to cloud{escape(who)}[/] ({escape(session.host)}).")
    if session.subscription is not None:
        console.print(f"  {escape(session.subscription.describe())}")
    else:
        console.print("  [dim]Subscription details unavailable; see `screenscribe auth status`.[/]")
    console.print(f"[dim]Session stored in {accounts_path()} (0600).[/]")


@auth_app.command("login")
def auth_login(
    provider: Annotated[str, typer.Argument(help=_PROVIDER_HELP)],
    no_browser: Annotated[
        bool, typer.Option("--no-browser", help="Print the URL only; do not open a browser.")
    ] = False,
    cloud_url: Annotated[
        str | None,
        typer.Option(
            "--cloud-url",
            help=(
                "Screenscribe Cloud base URL (cloud only). Default: SCREENSCRIBE_CLOUD_URL, "
                "then the signed-in host, then the production service."
            ),
        ),
    ] = None,
) -> None:
    """Sign in to Screenscribe Cloud or a provider account via the device-code flow."""
    provider = _provider_or_exit(provider)
    if provider == CLOUD_PROVIDER:
        _cloud_login(cloud_url, no_browser=no_browser)
        return
    if cloud_url is not None:
        console.print("[red]Error:[/] --cloud-url applies only to `screenscribe auth login cloud`.")
        raise typer.Exit(2)
    try:
        device = request_device_code(provider)
    except AccountAuthError as error:
        console.print(f"[red]Sign-in failed ({error.kind}):[/] {error}")
        raise typer.Exit(1) from None

    console.print(f"[bold]Open:[/] {device.verification_url}")
    console.print(f"[bold]Enter code:[/] [cyan]{device.user_code}[/]")
    if not no_browser:
        try:
            webbrowser.open(device.verification_url)
        except Exception:  # browser launch is best-effort
            console.print("[dim]Could not open a browser; use the URL above.[/]")
    console.print("[dim]Waiting for approval (up to 15 minutes, Ctrl+C to abort)...[/]")

    try:
        complete_device_code_login(device)
    except AccountAuthError as error:
        console.print(f"[red]Sign-in failed ({error.kind}):[/] {error}")
        raise typer.Exit(1) from None
    except KeyboardInterrupt:
        console.print("[yellow]Aborted.[/] Nothing was stored.")
        raise typer.Exit(130) from None

    status = account_status(provider)
    who = f" as {status.email}" if status.email else ""
    console.print(
        f"[green]Signed in to {provider}{who}.[/] Tokens stored in {accounts_path()} (0600)."
    )
    if provider == "openai":
        console.print(
            "[yellow]Note:[/] this ChatGPT account serves LLM + vision through the ChatGPT route "
            "(chatgpt.com), not api.openai.com, which stays identity-only for account tokens. "
            "Select it with `screenscribe config auth chatgpt`; STT stays on your current "
            "provider (ChatGPT has no speech-to-text)."
        )


@auth_app.command("status")
def auth_status() -> None:
    """Show sign-in state for every supported provider (never prints tokens)."""
    console.print(f"[dim]Account store: {accounts_path()}[/]")
    for name in sorted(PROVIDERS):
        try:
            status = account_status(name)
        except AccountAuthError as error:
            console.print(f"  {name:<8} [red]error[/] ({error.kind}): {error}")
            continue
        if not status.signed_in:
            console.print(f"  {name:<8} not signed in")
            continue
        who = status.email or "(no email in id_token)"
        if status.needs_login:
            console.print(
                f"  {name:<8} [red]sign-in ended[/] for {who} ({status.disabled_cause}); "
                f"run `screenscribe auth login {name}`"
            )
            continue
        usable = (
            f"usable as API bearer on {', '.join(status.bearer_hosts)}"
            if status.api_bearer_usable
            else "identity-only (API key still required)"
        )
        refresh = "refresh token stored" if status.has_refresh_token else "no refresh token"
        console.print(
            f"  {name:<8} signed in as {who}; {_fmt_expiry(status.expires_at)}; {refresh}; {usable}"
        )
    _print_cloud_status()


def _print_cloud_status() -> None:
    """Cloud line of ``auth status``: no network at all when signed out."""
    try:
        stored = load_cloud_session()
    except AccountAuthError as error:
        console.print(f"  cloud [red]error[/] ({error.kind}): {escape(str(error))}")
        return
    if stored is None:
        console.print("  cloud not signed in")
        return
    try:
        status = cloud_status()
    except AccountAuthError as error:
        status = CloudStatus(stored, live=False, note=str(error))
    if status is None:
        console.print("  cloud not signed in")
        return
    session = status.session
    who = escape(session.email or "(email unknown)")
    host = escape(session.host)
    if status.needs_login:
        note = f": {escape(status.note)}" if status.note else ""
        console.print(f"  cloud [red]sign-in ended[/] for {who} ({host}){note}")
        console.print("    run `screenscribe auth login cloud`")
        return
    marker = "" if status.live else " [dim]" + escape(f"[{status.note or 'cached'}]") + "[/]"
    console.print(f"  cloud signed in as {who} ({host}){marker}")
    if session.subscription is None:
        console.print("    [dim]subscription details unavailable[/]")
        return
    for line in session.subscription.summary_lines():
        console.print(f"    {escape(line)}")


@auth_app.command("logout")
def auth_logout(provider: Annotated[str, typer.Argument(help=_PROVIDER_HELP)]) -> None:
    """Remove the stored tokens for one provider."""
    provider = _provider_or_exit(provider)
    try:
        if provider == CLOUD_PROVIDER:
            removed = clear_cloud_session()
        else:
            removed = clear_account_tokens(provider)
    except AccountAuthError as error:
        console.print(f"[red]Logout failed ({error.kind}):[/] {error}")
        raise typer.Exit(1) from None
    if removed:
        console.print(f"[green]Signed out of {provider}.[/]")
    else:
        console.print(f"[dim]{provider}: no stored session.[/]")
