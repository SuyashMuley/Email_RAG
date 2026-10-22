"""Bearer-token authentication: turn a token into a verified email address.

Two interchangeable modes (AUTH_MODE):
- local:  HS256 JWTs signed with LOCAL_JWT_SECRET and issued by `rag token`. A stand-in for an
          identity provider so the system can be run and tested without Google.
- google: Google OpenID Connect ID tokens, verified against Google's public keys and our client id.

Either way the API learns the caller's identity only from a verified token, never from the request.
"""

import json
import time
from collections.abc import Callable
from pathlib import Path

import jwt

from rag.config import Settings

ISSUER = "email-rag-local"
AUDIENCE = "email-rag"
MIN_SECRET_LENGTH = 32


class AuthError(Exception):
    """The token is missing, malformed, expired, or not trusted."""


class ConfigError(RuntimeError):
    """Authentication is not configured safely."""


def issue_local_token(email: str, secret: str, ttl_minutes: int = 60, now: float | None = None) -> str:
    issued = int(now if now is not None else time.time())
    claims = {
        "iss": ISSUER, "aud": AUDIENCE, "sub": email.lower(), "email": email.lower(),
        "iat": issued, "exp": issued + ttl_minutes * 60,
    }
    return jwt.encode(claims, secret, algorithm="HS256")


def verify_local(token: str, secret: str) -> str:
    try:
        claims = jwt.decode(
            token, secret, algorithms=["HS256"],  # pinned: rejects alg=none and key-confusion tricks
            audience=AUDIENCE, issuer=ISSUER, options={"require": ["exp", "iat", "sub"]},
        )
    except jwt.PyJWTError as exc:
        raise AuthError(f"{type(exc).__name__}: {exc}") from exc
    return str(claims["sub"]).lower()


def verify_google(token: str, client_id: str) -> str:
    from google.auth.transport import requests as google_requests
    from google.oauth2 import id_token

    try:
        claims = id_token.verify_oauth2_token(token, google_requests.Request(), client_id)
    except ValueError as exc:  # bad signature, expired, wrong audience or issuer
        raise AuthError(str(exc)) from exc
    if not claims.get("email_verified") or not claims.get("email"):
        raise AuthError("Google account email is not verified")
    return str(claims["email"]).lower()


def google_client_id(settings: Settings) -> str:
    if settings.google_client_id:
        return settings.google_client_id
    path = Path(settings.gmail_credentials_path)
    if path.exists():
        data = json.loads(path.read_text())
        for key in ("installed", "web"):
            if key in data:
                return data[key]["client_id"]
    raise ConfigError("Set GOOGLE_CLIENT_ID or provide credentials.json for AUTH_MODE=google")


def make_verifier(settings: Settings) -> Callable[[str], str]:
    """Build the token -> email function for the configured mode. Fails fast on unsafe config."""
    if settings.auth_mode == "local":
        if len(settings.local_jwt_secret) < MIN_SECRET_LENGTH:
            raise ConfigError(
                f"LOCAL_JWT_SECRET must be set to at least {MIN_SECRET_LENGTH} characters. "
                'Generate one: python -c "import secrets; print(secrets.token_urlsafe(48))"'
            )
        secret = settings.local_jwt_secret
        return lambda token: verify_local(token, secret)
    if settings.auth_mode == "google":
        client_id = google_client_id(settings)
        return lambda token: verify_google(token, client_id)
    raise ConfigError(f"Unknown AUTH_MODE {settings.auth_mode!r} (use 'local' or 'google')")


def fresh_google_id_token(email: str) -> str:
    """Refresh the saved Google credentials and return a new ID token for `email`."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    from rag.ingest.gmail_client import SCOPES, CredentialsMissing, token_path

    path = token_path(email)
    if not path.exists():
        raise CredentialsMissing(f"No token for {email}. Run `rag gmail-auth` first.")
    creds = Credentials.from_authorized_user_file(str(path), SCOPES)
    creds.refresh(Request())
    if not creds.id_token:
        raise CredentialsMissing("Saved token has no ID token. Run `rag gmail-auth` again.")
    return creds.id_token
