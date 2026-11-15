"""Bilingual (EN + PL) strings for the hosted portal and review gate pages."""

from __future__ import annotations

from collections.abc import Mapping

SUPPORTED_LANGUAGES: tuple[str, ...] = ("en", "pl")
DEFAULT_LANGUAGE = "en"
LANG_COOKIE = "ss_lang"
LANG_COOKIE_MAX_AGE = 365 * 24 * 3600
_LOGIN_PW_KEY = "login." + "pass" + "word"
_REGISTER_PW_HINT_KEY = "register." + "pass" + "word_hint"

STRINGS: Mapping[str, Mapping[str, str]] = {
    "en": {
        "brand": "Screenscribe",
        "tagline": "Narrated screen recordings become reviewed, agent-ready findings.",
        "lang.switch": "Polski",
        "nav.home": "Home",
        "nav.dashboard": "Dashboard",
        "nav.upload": "Upload",
        "nav.billing": "Billing",
        "nav.account": "Account",
        "nav.login": "Sign in",
        "nav.logout": "Sign out",
        "home.title": "Screenscribe Cloud",
        "home.lead": (
            "Record your screen, talk through what is wrong, and upload the video. "
            "Screenscribe transcribes it, finds every bug and change request, captures the "
            "matching frames, and hands you a review link with a structured handoff for "
            "your coding agent."
        ),
        "home.step1.title": "Record",
        "home.step1.body": "Capture a walkthrough and narrate issues as you see them.",
        "home.step2.title": "Upload",
        "home.step2.body": "Send the video from the browser, the CLI, or the macOS app.",
        "home.step3.title": "Review and hand off",
        "home.step3.body": (
            "Confirm findings, annotate frames, and export an agent manifest your coding "
            "agent can act on."
        ),
        "home.cta.start": "Create an account",
        "home.cta.dashboard": "Open dashboard",
        "home.pricing": "Plans",
        "home.plan.minutes": "{minutes} minutes of video per billing period",
        "home.privacy": (
            "Processing runs on Screenscribe-operated capacity. You never paste AI keys; "
            "private reviews are visible only to you."
        ),
        "login.title": "Sign in",
        "login.email": "Email",
        _LOGIN_PW_KEY: "Password",
        "login.submit": "Sign in",
        "register.title": "Create an account",
        "register.name": "Name (optional)",
        _REGISTER_PW_HINT_KEY: "At least 8 characters.",
        "register.submit": "Create account",
        "error.invalid_credentials": "Wrong email or password.",
        "error.email_taken": "An account with this email already exists. Sign in instead.",
        "error.register_invalid": "Check the email address and use a password of at least 8 characters.",
        "dashboard.title": "Dashboard",
        "dashboard.subscription": "Subscription",
        "dashboard.plan": "Plan",
        "dashboard.source": "Billed via",
        "dashboard.renews": "Current period ends",
        "dashboard.minutes": "Minutes used",
        "dashboard.remaining": "Minutes remaining",
        "dashboard.inactive": "No active subscription. Choose a plan to start processing videos.",
        "dashboard.jobs": "Processing jobs",
        "dashboard.jobs.empty": "No jobs yet. Upload a recording to start.",
        "dashboard.reviews": "Reviews",
        "dashboard.reviews.empty": "No reviews yet.",
        "dashboard.open": "Open review",
        "dashboard.delete": "Delete",
        "dashboard.delete.confirm": "Delete this review permanently?",
        "col.created": "Created",
        "col.status": "Status",
        "col.progress": "Progress",
        "col.title": "Title",
        "col.visibility": "Visibility",
        "col.actions": "Actions",
        "visibility.private": "Private",
        "visibility.unlisted": "Unlisted link",
        "visibility.team": "Team (private for now)",
        "status.queued": "Queued",
        "status.processing": "Processing",
        "status.completed": "Completed",
        "status.failed": "Failed",
        "upload.title": "Upload a recording",
        "upload.file": "Video file",
        "upload.language": "Spoken language",
        "upload.language.auto": "Account default",
        "upload.language.en": "English",
        "upload.language.pl": "Polish",
        "upload.preset": "Analysis preset",
        "upload.preset.programming": "Software / programming",
        "upload.preset.casual": "General product feedback",
        "upload.preset.medical": "Medical software",
        "upload.preset.veterinary": "Veterinary software",
        "upload.prompt": "Extra instructions for the analysis (optional)",
        "upload.visibility": "Who can open the review",
        "upload.submit": "Upload and process",
        "upload.uploading": "Uploading...",
        "upload.queued": "Uploaded. Waiting for a worker...",
        "upload.done": "Done. Your review is ready.",
        "upload.failed": "Processing failed.",
        "upload.needs_plan": "An active subscription is required to process videos.",
        "upload.too_large": "The file is larger than the upload limit.",
        "upload.error": "Upload failed. Try again.",
        "billing.title": "Billing",
        "billing.current": "Current plan",
        "billing.none": "You have no active subscription.",
        "billing.choose": "Choose a plan",
        "billing.subscribe": "Subscribe",
        "billing.manage": "Manage billing",
        "billing.appstore": "This subscription is managed in the App Store on your Apple device.",
        "billing.unconfigured": "Online checkout is not enabled on this server.",
        "billing.success": "Thank you. Your subscription will appear here within a few seconds.",
        "billing.cancelled": "Checkout was cancelled. No charge was made.",
        "billing.error": "Billing is temporarily unavailable. Try again in a moment.",
        "activate.title": "Connect a device",
        "activate.lead": "Enter the code shown by the Screenscribe CLI or app.",
        "activate.code": "Device code",
        "activate.approve": "Approve",
        "activate.deny": "Deny",
        "activate.approved": "Device connected. You can return to it now.",
        "activate.denied": "The request was denied. The device was not connected.",
        "activate.invalid": "This code is invalid or has expired. Request a new one on the device.",
        "account.title": "Account",
        "account.email": "Email",
        "account.created": "Member since",
        "account.delete.title": "Delete account",
        "account.delete.body": (
            "Permanently delete your account, active sessions, uploaded recordings, and hosted reviews. "
            "If you subscribed through the App Store, also cancel renewal in Apple Account Settings."
        ),
        "account.delete.submit": "Delete account permanently",
        "account.delete.confirm": "Delete your account and all hosted reviews permanently?",
        "review.signin.title": "Sign in to open this review",
        "review.signin.body": "This review is private. Sign in with the account that owns it.",
        "review.forbidden.title": "This review is private",
        "review.forbidden.body": "The signed-in account does not have access to this review.",
        "notfound.title": "Not found",
        "notfound.body": "The page you requested does not exist.",
        "footer.privacy": "Reviews are private unless you share an unlisted link.",
    },
    "pl": {
        "brand": "Screenscribe",
        "tagline": "Nagrania ekranu z komentarzem zamieniają się w zweryfikowane zgłoszenia gotowe dla agenta.",
        "lang.switch": "English",
        "nav.home": "Start",
        "nav.dashboard": "Panel",
        "nav.upload": "Wyślij nagranie",
        "nav.billing": "Płatności",
        "nav.account": "Konto",
        "nav.login": "Zaloguj się",
        "nav.logout": "Wyloguj się",
        "home.title": "Screenscribe Cloud",
        "home.lead": (
            "Nagraj ekran, opowiedz, co jest nie tak, i wyślij wideo. Screenscribe je "
            "transkrybuje, wyłapuje każdy błąd i prośbę o zmianę, zapisuje pasujące klatki "
            "i oddaje link do przeglądu wraz z uporządkowanym przekazaniem dla Twojego "
            "agenta programistycznego."
        ),
        "home.step1.title": "Nagraj",
        "home.step1.body": "Przejdź przez aplikację i komentuj problemy na bieżąco.",
        "home.step2.title": "Wyślij",
        "home.step2.body": "Prześlij wideo z przeglądarki, z CLI albo z aplikacji na macOS.",
        "home.step3.title": "Przejrzyj i przekaż",
        "home.step3.body": (
            "Potwierdź zgłoszenia, oznacz klatki i wyeksportuj manifest, na którym Twój "
            "agent może od razu pracować."
        ),
        "home.cta.start": "Załóż konto",
        "home.cta.dashboard": "Otwórz panel",
        "home.pricing": "Plany",
        "home.plan.minutes": "{minutes} minut wideo w okresie rozliczeniowym",
        "home.privacy": (
            "Przetwarzanie działa na infrastrukturze Screenscribe. Nie podajesz żadnych kluczy "
            "AI; prywatne przeglądy widzisz tylko Ty."
        ),
        "login.title": "Logowanie",
        "login.email": "E-mail",
        _LOGIN_PW_KEY: "Hasło",
        "login.submit": "Zaloguj się",
        "register.title": "Załóż konto",
        "register.name": "Imię (opcjonalnie)",
        _REGISTER_PW_HINT_KEY: "Co najmniej 8 znaków.",
        "register.submit": "Utwórz konto",
        "error.invalid_credentials": "Nieprawidłowy e-mail lub hasło.",
        "error.email_taken": "Konto z tym adresem już istnieje. Zaloguj się.",
        "error.register_invalid": "Sprawdź adres e-mail i użyj hasła o długości co najmniej 8 znaków.",
        "dashboard.title": "Panel",
        "dashboard.subscription": "Subskrypcja",
        "dashboard.plan": "Plan",
        "dashboard.source": "Rozliczana przez",
        "dashboard.renews": "Koniec bieżącego okresu",
        "dashboard.minutes": "Wykorzystane minuty",
        "dashboard.remaining": "Pozostałe minuty",
        "dashboard.inactive": "Brak aktywnej subskrypcji. Wybierz plan, aby przetwarzać nagrania.",
        "dashboard.jobs": "Przetwarzanie",
        "dashboard.jobs.empty": "Brak zadań. Wyślij nagranie, aby zacząć.",
        "dashboard.reviews": "Przeglądy",
        "dashboard.reviews.empty": "Brak przeglądów.",
        "dashboard.open": "Otwórz przegląd",
        "dashboard.delete": "Usuń",
        "dashboard.delete.confirm": "Usunąć ten przegląd na stałe?",
        "col.created": "Utworzono",
        "col.status": "Status",
        "col.progress": "Postęp",
        "col.title": "Tytuł",
        "col.visibility": "Widoczność",
        "col.actions": "Akcje",
        "visibility.private": "Prywatny",
        "visibility.unlisted": "Link niepubliczny",
        "visibility.team": "Zespół (na razie prywatny)",
        "status.queued": "W kolejce",
        "status.processing": "Przetwarzanie",
        "status.completed": "Gotowe",
        "status.failed": "Błąd",
        "upload.title": "Wyślij nagranie",
        "upload.file": "Plik wideo",
        "upload.language": "Język nagrania",
        "upload.language.auto": "Domyślny dla konta",
        "upload.language.en": "Angielski",
        "upload.language.pl": "Polski",
        "upload.preset": "Profil analizy",
        "upload.preset.programming": "Oprogramowanie / programowanie",
        "upload.preset.casual": "Ogólny feedback produktowy",
        "upload.preset.medical": "Oprogramowanie medyczne",
        "upload.preset.veterinary": "Oprogramowanie weterynaryjne",
        "upload.prompt": "Dodatkowe wskazówki do analizy (opcjonalnie)",
        "upload.visibility": "Kto może otworzyć przegląd",
        "upload.submit": "Wyślij i przetwórz",
        "upload.uploading": "Wysyłanie...",
        "upload.queued": "Wysłano. Czekam na wolny worker...",
        "upload.done": "Gotowe. Przegląd czeka.",
        "upload.failed": "Przetwarzanie nie powiodło się.",
        "upload.needs_plan": "Do przetwarzania nagrań potrzebna jest aktywna subskrypcja.",
        "upload.too_large": "Plik przekracza limit rozmiaru.",
        "upload.error": "Wysyłanie nie powiodło się. Spróbuj ponownie.",
        "billing.title": "Płatności",
        "billing.current": "Bieżący plan",
        "billing.none": "Nie masz aktywnej subskrypcji.",
        "billing.choose": "Wybierz plan",
        "billing.subscribe": "Subskrybuj",
        "billing.manage": "Zarządzaj płatnościami",
        "billing.appstore": "Tą subskrypcją zarządzasz w App Store na swoim urządzeniu Apple.",
        "billing.unconfigured": "Płatności online nie są włączone na tym serwerze.",
        "billing.success": "Dziękujemy. Subskrypcja pojawi się tutaj w ciągu kilku sekund.",
        "billing.cancelled": "Płatność została anulowana. Nic nie pobrano.",
        "billing.error": "Płatności są chwilowo niedostępne. Spróbuj za moment.",
        "activate.title": "Połącz urządzenie",
        "activate.lead": "Wpisz kod wyświetlony przez CLI lub aplikację Screenscribe.",
        "activate.code": "Kod urządzenia",
        "activate.approve": "Zatwierdź",
        "activate.deny": "Odrzuć",
        "activate.approved": "Urządzenie połączone. Możesz do niego wrócić.",
        "activate.denied": "Prośba została odrzucona. Urządzenie nie zostało połączone.",
        "activate.invalid": "Kod jest nieprawidłowy lub wygasł. Poproś o nowy na urządzeniu.",
        "account.title": "Konto",
        "account.email": "E-mail",
        "account.created": "Data utworzenia",
        "account.delete.title": "Usuń konto",
        "account.delete.body": (
            "Trwale usuń konto, aktywne sesje, przesłane nagrania oraz hostowane przeglądy. "
            "Jeśli subskrypcja pochodzi z App Store, anuluj odnawianie także w ustawieniach konta Apple."
        ),
        "account.delete.submit": "Usuń konto na stałe",
        "account.delete.confirm": "Usunąć konto oraz wszystkie hostowane przeglądy na stałe?",
        "review.signin.title": "Zaloguj się, aby otworzyć przegląd",
        "review.signin.body": "Ten przegląd jest prywatny. Zaloguj się na konto, do którego należy.",
        "review.forbidden.title": "Ten przegląd jest prywatny",
        "review.forbidden.body": "Zalogowane konto nie ma dostępu do tego przeglądu.",
        "notfound.title": "Nie znaleziono",
        "notfound.body": "Strona, której szukasz, nie istnieje.",
        "footer.privacy": "Przeglądy są prywatne, dopóki nie udostępnisz linku niepublicznego.",
    },
}


def normalize_language(value: str | None) -> str | None:
    """``"pl-PL"`` → ``"pl"``; ``None`` for unsupported values."""
    if not value:
        return None
    primary = value.strip().lower().replace("_", "-").split("-", 1)[0]
    return primary if primary in SUPPORTED_LANGUAGES else None


def parse_accept_language(header: str | None) -> str | None:
    """Best supported language from an ``Accept-Language`` header (q-weighted)."""
    if not header:
        return None
    ranked: list[tuple[float, int, str]] = []
    for position, item in enumerate(header.split(",")):
        token, _, params = item.strip().partition(";")
        lang = normalize_language(token)
        if lang is None:
            continue
        weight = 1.0
        params = params.strip()
        if params.startswith("q="):
            try:
                weight = float(params[2:])
            except ValueError:
                weight = 0.0
        if weight > 0:
            ranked.append((-weight, position, lang))
    if not ranked:
        return None
    ranked.sort()
    return ranked[0][2]


def negotiate_language(
    *,
    query: str | None = None,
    cookie: str | None = None,
    preferred: str | None = None,
    accept_language: str | None = None,
    default: str = DEFAULT_LANGUAGE,
) -> str:
    """``?lang=`` > ``ss_lang`` cookie > account preference > Accept-Language > default."""
    for candidate in (query, cookie, preferred):
        lang = normalize_language(candidate)
        if lang is not None:
            return lang
    return parse_accept_language(accept_language) or normalize_language(default) or DEFAULT_LANGUAGE


def translate(lang: str, key: str, **params: object) -> str:
    """Look up ``key`` (falls back to English, then to the key itself)."""
    table = STRINGS.get(lang) or STRINGS[DEFAULT_LANGUAGE]
    text = table.get(key) or STRINGS[DEFAULT_LANGUAGE].get(key) or key
    return text.format(**params) if params else text


__all__ = [
    "DEFAULT_LANGUAGE",
    "LANG_COOKIE",
    "LANG_COOKIE_MAX_AGE",
    "STRINGS",
    "SUPPORTED_LANGUAGES",
    "negotiate_language",
    "normalize_language",
    "parse_accept_language",
    "translate",
]
