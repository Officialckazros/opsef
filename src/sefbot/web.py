"""Small public web surface for OpSef legal and health endpoints.

The Discord client owns :class:`WebService` in production and supplies a
readiness callback for its Discord and database state.  Keeping the HTTP
surface here avoids importing Discord or the bot's configuration at module
import time, which also makes health checks safe during partial startup.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import html
import inspect
import ipaddress
import logging
import os
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Final, TypeAlias

from aiohttp import web

log = logging.getLogger("sefbot.web")

LEGAL_VERSION: Final = "2.0"
LEGAL_EFFECTIVE_DATE: Final = "19 August 2026"
PUBLIC_BASE_URL: Final = "https://kozzyx.org/sefbot"
TERMS_URL: Final = f"{PUBLIC_BASE_URL}/terms"
PRIVACY_URL: Final = f"{PUBLIC_BASE_URL}/privacy"
DEFAULT_HOST: Final = "0.0.0.0"  # noqa: S104, RUF100 - container listener
DEFAULT_PORT: Final = 8080
MAX_REQUEST_BYTES: Final = 1_024
READINESS_TIMEOUT_SECONDS: Final = 2.0

_STYLE: Final = """
:root{color-scheme:dark;font-family:system-ui,sans-serif;background:#111;color:#eee}
body{max-width:52rem;margin:4rem auto;padding:0 1.2rem;line-height:1.65}
a{color:#8fc7ff}h1,h2{line-height:1.2}nav{display:flex;gap:1rem;flex-wrap:wrap}
.card{border:1px solid #333;border-radius:.8rem;padding:1rem 1.2rem;background:#181818}
code{background:#222;padding:.1rem .3rem;border-radius:.25rem}
""".strip()
_STYLE_HASH: Final = base64.b64encode(
    hashlib.sha256(_STYLE.encode("utf-8")).digest()
).decode("ascii")
_READINESS_COMPONENTS: Final = frozenset({"service", "discord", "database"})

ReadinessResult: TypeAlias = bool | Mapping[str, bool]
ReadinessProvider: TypeAlias = Callable[
    [], ReadinessResult | Awaitable[ReadinessResult]
]


class WebConfigurationError(ValueError):
    """Raised when the public web service is configured unsafely."""


@dataclass(slots=True)
class ReadinessState:
    """Mutable lifecycle state suitable for a bot-owned ``WebService``."""

    discord: bool = False
    database: bool = False

    def __call__(self) -> Mapping[str, bool]:
        return {"discord": self.discord, "database": self.database}


def _document(title: str, body: str) -> str:
    safe_title = html.escape(title)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <meta name="robots" content="index,follow">
  <title>{safe_title} | OpSef</title>
  <style>{_STYLE}</style>
</head>
<body>
  <nav aria-label="Legal and service pages">
    <a href="/sefbot">OpSef</a>
    <a href="/sefbot/terms">Terms</a>
    <a href="/sefbot/privacy">Privacy</a>
  </nav>
  <main>{body}</main>
</body>
</html>"""


def _landing_page() -> str:
    return _document(
        "Discord bot",
        """
<h1>OpSef Discord bot</h1>
<div class="card">
  <p>OpSef is a Discord assistant with opt-in memory and administration tools.</p>
  <p>Read the Terms and Privacy Notice before using the bot. Health endpoints
  report service availability without exposing user, guild, or provider data.</p>
</div>
""",
    )


def _terms_page(contact: str) -> str:
    safe_contact = html.escape(contact)
    return _document(
        "Terms of Service",
        f"""
<h1>Terms of Service</h1>
<p><strong>Version {LEGAL_VERSION}</strong> — effective {LEGAL_EFFECTIVE_DATE}</p>
<p>By accepting these terms, you may use OpSef where it is installed and where
you have permission to interact with it. You must follow Discord's terms,
applicable law, and the rules of the relevant server.</p>
<h2>Acceptable use</h2>
<p>Do not use OpSef to harass people, bypass access controls, distribute illegal
material, expose private information, or interfere with Discord or third-party
services. Automated output can be wrong; verify it before relying on it.</p>
<h2>Administrative actions</h2>
<p>State-changing actions require an authorized user to review and confirm a
preview. Server administrators remain responsible for configuration and for
actions they approve.</p>
<h2>Availability and changes</h2>
<p>The service is provided without a guarantee of uninterrupted availability.
Material changes require acceptance of a new terms version.</p>
<h2>Contact</h2>
<p>Questions or reports: <span>{safe_contact}</span>.</p>
""",
    )


def _privacy_page(contact: str) -> str:
    safe_contact = html.escape(contact)
    return _document(
        "Privacy Notice",
        f"""
<h1>Privacy Notice</h1>
<p><strong>Version {LEGAL_VERSION}</strong> — effective {LEGAL_EFFECTIVE_DATE}</p>
<h2>Data processed</h2>
<p>Discord supplies identifiers, display names, command inputs, and message or
attachment content needed to answer a request. Moderation and voice features
are disabled until a server administrator enables them. Enabled AI features
may send the minimum required request content to configured AI, search, speech,
or media providers. OpSef does not sell personal data.</p>
<h2>Memory and retention</h2>
<p>Raw message history is off by default and requires both server enablement and
the user's separate opt-in. Opted-in raw history is retained for no more than
30 days. Terms acceptance alone is not consent to raw-history storage. Explicit
memories and settings remain until they are deleted or are no longer needed to
provide the service. Minimal security and action-audit records may be retained
to investigate abuse without storing unnecessary message content.</p>
<h2>Controls</h2>
<p>Use <code>/privacy status</code>, <code>/privacy opt-in</code>,
<code>/privacy opt-out</code>, <code>/privacy export</code>, or
<code>/privacy delete</code>. Exports are private, and deletion covers data
owned by the requesting user. Discord and configured providers may keep their
own operational records under their respective policies.</p>
<h2>Security and contact</h2>
<p>Access to another member's current-server intelligence is restricted to
authorized moderators. Report privacy or security concerns to
<span>{safe_contact}</span>.</p>
""",
    )


def _html_response(content: str) -> web.Response:
    return web.Response(text=content, content_type="text/html", charset="utf-8")


def _valid_bind_host(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        labels = host.split(".")
        return bool(labels) and all(
            1 <= len(label) <= 63
            and label.isascii()
            and label[0].isalnum()
            and label[-1].isalnum()
            and all(char.isalnum() or char == "-" for char in label)
            for label in labels
        )


def _normalize_bind_host(host: object) -> str:
    if not isinstance(host, str):
        raise WebConfigurationError("web host is invalid")
    normalized = host.strip()
    if (
        not normalized
        or len(normalized) > 253
        or any(not char.isprintable() or char.isspace() for char in normalized)
        or not _valid_bind_host(normalized)
    ):
        raise WebConfigurationError("web host is invalid")
    return normalized


@web.middleware
async def _security_headers(
    request: web.Request,
    handler: Callable[[web.Request], Awaitable[web.StreamResponse]],
) -> web.StreamResponse:
    try:
        response = await handler(request)
    except web.HTTPException as error:
        response = web.Response(
            status=error.status,
            reason=error.reason,
            text=error.text,
            headers=error.headers,
        )
    response.headers["Content-Security-Policy"] = (
        "default-src 'none'; base-uri 'none'; form-action 'none'; "
        f"style-src 'sha256-{_STYLE_HASH}'; frame-ancestors 'none'"
    )
    response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
    response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
    response.headers["Permissions-Policy"] = (
        "camera=(), geolocation=(), microphone=(), payment=(), usb=()"
    )
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Server"] = "OpSef"
    response.headers["Strict-Transport-Security"] = (
        "max-age=31536000; includeSubDomains"
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    cacheable_page = (
        request.method in {"GET", "HEAD"}
        and response.status == 200
        and request.path in {"/sefbot", "/sefbot/terms", "/sefbot/privacy"}
    )
    if cacheable_page:
        response.headers["Cache-Control"] = "public, max-age=300"
        response.headers["X-Robots-Tag"] = "index, follow"
    else:
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Robots-Tag"] = "noindex, nofollow"
    return response


async def _resolve_readiness(provider: ReadinessProvider) -> dict[str, bool]:
    result = provider()
    if inspect.isawaitable(result):
        result = await result
    if isinstance(result, bool):
        return {"service": result}
    if not isinstance(result, Mapping) or not result:
        raise TypeError("readiness provider must return bool or a non-empty mapping")

    components: dict[str, bool] = {}
    for name, ready in result.items():
        if (
            not isinstance(name, str)
            or name not in _READINESS_COMPONENTS
            or not isinstance(ready, bool)
        ):
            raise TypeError("invalid readiness component")
        components[name] = ready
    return components


def create_app(
    *, privacy_contact: str, readiness: ReadinessProvider | None = None
) -> web.Application:
    """Create the HTTP application without starting a listening socket."""

    if not isinstance(privacy_contact, str):
        raise WebConfigurationError("SEFBOT_PRIVACY_CONTACT must be text")
    contact = privacy_contact.strip()
    if (
        not contact
        or len(contact) > 200
        or any(not char.isprintable() for char in contact)
    ):
        raise WebConfigurationError(
            "SEFBOT_PRIVACY_CONTACT must be a non-empty contact up to 200 characters"
        )
    readiness_provider = readiness or ReadinessState()
    if not callable(readiness_provider):
        raise WebConfigurationError("readiness provider must be callable")
    app = web.Application(
        middlewares=[_security_headers], client_max_size=MAX_REQUEST_BYTES
    )

    async def landing(_request: web.Request) -> web.Response:
        return _html_response(_landing_page())

    async def terms(_request: web.Request) -> web.Response:
        return _html_response(_terms_page(contact))

    async def privacy(_request: web.Request) -> web.Response:
        return _html_response(_privacy_page(contact))

    async def health(_request: web.Request) -> web.Response:
        return web.json_response({"status": "ok"})

    async def ready(_request: web.Request) -> web.Response:
        try:
            async with asyncio.timeout(READINESS_TIMEOUT_SECONDS):
                components = await _resolve_readiness(readiness_provider)
        except Exception as error:  # noqa: BLE001, RUF100 - health checks fail closed
            log.warning("Readiness provider failed (%s)", type(error).__name__)
            return web.json_response({"status": "not_ready"}, status=503)
        is_ready = all(components.values())
        payload = {"status": "ready" if is_ready else "not_ready", **components}
        return web.json_response(payload, status=200 if is_ready else 503)

    async def redirect_landing(_request: web.Request) -> web.StreamResponse:
        raise web.HTTPPermanentRedirect(location="/sefbot")

    async def redirect_terms(_request: web.Request) -> web.StreamResponse:
        raise web.HTTPPermanentRedirect(location="/sefbot/terms")

    async def redirect_privacy(_request: web.Request) -> web.StreamResponse:
        raise web.HTTPPermanentRedirect(location="/sefbot/privacy")

    app.router.add_get("/sefbot", landing)
    app.router.add_get("/sefbot/", redirect_landing)
    app.router.add_get("/sefbot/terms", terms)
    app.router.add_get("/sefbot/privacy", privacy)
    app.router.add_get("/sefbot/tos", redirect_terms)
    app.router.add_get("/opsef-tos.html", redirect_terms)
    app.router.add_get("/opsef-privacy.html", redirect_privacy)
    app.router.add_get("/healthz", health)
    app.router.add_get("/readyz", ready)
    return app


class WebService:
    """Lifecycle wrapper used by the Discord client."""

    def __init__(
        self,
        *,
        privacy_contact: str,
        readiness: ReadinessProvider,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
    ) -> None:
        try:
            normalized_port = int(port)
        except (TypeError, ValueError) as error:
            raise WebConfigurationError("web port must be an integer") from error
        if not 1 <= normalized_port <= 65_535:
            raise WebConfigurationError("web port must be between 1 and 65535")
        normalized_host = _normalize_bind_host(host)
        self._app = create_app(
            privacy_contact=privacy_contact, readiness=readiness
        )
        self._host = normalized_host
        self._port = normalized_port
        self._runner: web.AppRunner | None = None
        self._lifecycle_lock = asyncio.Lock()

    async def start(self) -> None:
        async with self._lifecycle_lock:
            if self._runner is not None:
                return
            runner = web.AppRunner(self._app, access_log=None)
            try:
                await runner.setup()
                await web.TCPSite(runner, self._host, self._port).start()
            except BaseException:  # noqa: BLE001, RUF100 - includes cancellation cleanup
                await runner.cleanup()
                raise
            self._runner = runner
            log.info("Public web service listening on %s:%d", self._host, self._port)

    async def close(self) -> None:
        async with self._lifecycle_lock:
            runner, self._runner = self._runner, None
            if runner is not None:
                await runner.cleanup()


def _environment_port() -> int:
    raw_port = os.getenv("PORT", str(DEFAULT_PORT))
    try:
        port = int(raw_port)
    except ValueError as exc:
        raise WebConfigurationError("PORT must be an integer") from exc
    if not 1 <= port <= 65_535:
        raise WebConfigurationError("PORT must be between 1 and 65535")
    return port


def main() -> None:
    try:
        contact = os.getenv("SEFBOT_PRIVACY_CONTACT", "")
        app = create_app(privacy_contact=contact)
        port = _environment_port()
        host = _normalize_bind_host(os.getenv("SEFBOT_WEB_HOST", DEFAULT_HOST))
    except WebConfigurationError as error:
        raise SystemExit(f"web configuration error: {error}") from None
    web.run_app(
        app,
        host=host,
        port=port,
        access_log=None,
        print=None,
    )


if __name__ == "__main__":
    main()
