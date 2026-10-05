"""``screenscribe auth`` command group: account (OAuth) sign-in per provider.

Kept out of ``cli.py`` so the main module only registers the sub-app. Nothing
here prints a token: the verification URL + short user code are the only
secrets-adjacent output, and both are meant for the user's eyes.
"""

from __future__ import annotations

import webbrowser
from datetime import UTC, datetime
from typing import Annotated

import typer
from rich.console import Console

from .account_auth import (
    PROVIDERS,
    AccountAuthError,
    account_status,
    accounts_path,
    clear_account_tokens,
)
from .account_auth.device_code import complete_device_code_login, request_device_code

console = Console()

auth_app = typer.Typer(
    name="auth",
    help="Sign in to a provider account (OAuth device flow) as an alternative to API keys.",
    add_completion=False,
    no_args_is_help=True,
)

_PROVIDER_HELP = "Provider: " + " | ".join(sorted(PROVIDERS))


def _provider_or_exit(provider: str) -> str:
    normalized = provider.lower().strip()
    if normalized not in PROVIDERS:
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


@auth_app.command("login")
def auth_login(
    provider: Annotated[str, typer.Argument(help=_PROVIDER_HELP)],
    no_browser: Annotated[
        bool, typer.Option("--no-browser", help="Print the URL only; do not open a browser.")
    ] = False,
) -> None:
    """Sign in with a provider account via the device-code flow."""
    provider = _provider_or_exit(provider)
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
    if not status.api_bearer_usable:
        console.print(
            "[yellow]Note:[/] OpenAI account sign-in is identity-only. api.openai.com rejects a "
            "ChatGPT account token for REST calls (verified 2026-09-08); STT/LLM/Vision still "
            "need an OpenAI API key."
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
        usable = (
            "usable as API bearer"
            if status.api_bearer_usable
            else "identity-only (API key still required)"
        )
        refresh = "refresh token stored" if status.has_refresh_token else "no refresh token"
        console.print(
            f"  {name:<8} signed in as {who}; {_fmt_expiry(status.expires_at)}; {refresh}; {usable}"
        )


@auth_app.command("logout")
def auth_logout(provider: Annotated[str, typer.Argument(help=_PROVIDER_HELP)]) -> None:
    """Remove the stored tokens for one provider."""
    provider = _provider_or_exit(provider)
    try:
        removed = clear_account_tokens(provider)
    except AccountAuthError as error:
        console.print(f"[red]Logout failed ({error.kind}):[/] {error}")
        raise typer.Exit(1) from None
    if removed:
        console.print(f"[green]Signed out of {provider}.[/]")
    else:
        console.print(f"[dim]{provider}: no stored session.[/]")
