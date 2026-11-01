"""Cookie-session authentication for the dashboard.

Single operator, no user table. A successful login mints a random 256-bit
token stored in memory; the browser receives it as an HttpOnly, SameSite=Lax
cookie (Secure when the request arrived over HTTPS). Restarting the process
invalidates every session by design.
"""
from __future__ import annotations

import secrets
import time
from dataclasses import dataclass

from fastapi import Request, Response

from src.config import Settings
from src.infra.logging import get_logger

log = get_logger(__name__)

SESSION_COOKIE = "polytail_session"
_MAX_FAILURES = 10
_FAILURE_WINDOW_S = 300.0


@dataclass
class Session:
    user: str
    expires_at: float


_sessions: dict[str, Session] = {}
_failures: dict[str, list[float]] = {}


def credentials_configured(settings: Settings) -> bool:
    return bool(settings.WEB_PASSWORD)


def auth_required(settings: Settings) -> bool:
    """Every dashboard surface requires a session.

    With WEB_PASSWORD unset nobody can log in, which deliberately closes the
    dashboard rather than exposing positions or the config surface.
    """
    return True


def verify_credentials(settings: Settings, username: str, password: str) -> bool:
    if not settings.WEB_PASSWORD:
        return False
    expected_user = settings.WEB_USERNAME or "admin"
    return (
        secrets.compare_digest(username or "", expected_user)
        and secrets.compare_digest(password or "", settings.WEB_PASSWORD)
    )


def create_session(settings: Settings, user: str) -> str:
    now = time.time()
    _prune(now)
    token = secrets.token_urlsafe(32)
    _sessions[token] = Session(user=user, expires_at=now + max(60, settings.WEB_SESSION_TTL_S))
    return token


def get_session(token: str | None) -> Session | None:
    if not token:
        return None
    session = _sessions.get(token)
    if session is None:
        return None
    if session.expires_at <= time.time():
        _sessions.pop(token, None)
        return None
    return session


def delete_session(token: str | None) -> None:
    if token:
        _sessions.pop(token, None)


def _prune(now: float) -> None:
    for token in [t for t, s in _sessions.items() if s.expires_at <= now]:
        _sessions.pop(token, None)


def current_session(request: Request) -> Session | None:
    return get_session(request.cookies.get(SESSION_COOKIE))


def current_user(request: Request) -> str | None:
    session = current_session(request)
    return session.user if session else None


def is_https(request: Request) -> bool:
    forwarded = request.headers.get("x-forwarded-proto", "")
    first = forwarded.split(",")[0].strip().lower()
    return request.url.scheme == "https" or first == "https"


def set_session_cookie(response: Response, token: str, request: Request,
                       ttl_s: int) -> None:
    response.set_cookie(
        SESSION_COOKIE, token, max_age=max(60, ttl_s), httponly=True,
        samesite="lax", secure=is_https(request), path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/")


_MAX_FAILURE_KEYS = 5000


def client_ip(request: Request) -> str:
    """Throttle key.

    Deliberately the transport peer, NOT X-Forwarded-For: the header is
    attacker-controlled unless a trusted proxy is configured, and keying the
    lockout on it let a client bypass the 10-try limit by rotating the header
    (and grew the failure map without bound). Behind a reverse proxy every
    client shares the proxy address, so one attacker can lock the login out
    for everyone -- the safe failure mode for a single-operator dashboard.
    """
    return request.client.host if request.client else "unknown"


def _prune_failures(now: float) -> None:
    stale = [ip for ip, stamps in _failures.items()
             if not stamps or now - stamps[-1] > _FAILURE_WINDOW_S]
    for ip in stale:
        _failures.pop(ip, None)
    if len(_failures) > _MAX_FAILURE_KEYS:
        ordered = sorted(_failures.items(), key=lambda item: item[1][-1] if item[1] else 0.0)
        for ip, _ in ordered[: len(ordered) - _MAX_FAILURE_KEYS // 2]:
            _failures.pop(ip, None)


def login_blocked(ip: str) -> bool:
    now = time.time()
    _prune_failures(now)
    stamps = [t for t in _failures.get(ip, []) if now - t < _FAILURE_WINDOW_S]
    _failures[ip] = stamps
    return len(stamps) >= _MAX_FAILURES


def record_login_failure(ip: str) -> None:
    now = time.time()
    if len(_failures) >= _MAX_FAILURE_KEYS:
        _prune_failures(now)
    _failures.setdefault(ip, []).append(now)


def reset_login_failures(ip: str) -> None:
    _failures.pop(ip, None)
