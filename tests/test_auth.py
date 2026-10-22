import json
import time

import jwt
import pytest

from rag.auth import tokens
from rag.auth.tokens import AuthError, ConfigError, issue_local_token, make_verifier, verify_google, verify_local
from rag.config import Settings

SECRET = "unit-test-secret-" + "x" * 32


def settings(**kw) -> Settings:
    return Settings(_env_file=None, local_jwt_secret=SECRET, **kw)


def test_local_token_roundtrip_lowercases_email():
    assert verify_local(issue_local_token("Pat@Example.com", SECRET), SECRET) == "pat@example.com"


def test_expired_token_is_rejected():
    old = issue_local_token("a@x.com", SECRET, ttl_minutes=60, now=time.time() - 7200)
    with pytest.raises(AuthError):
        verify_local(old, SECRET)


def test_wrong_secret_is_rejected():
    with pytest.raises(AuthError):
        verify_local(issue_local_token("a@x.com", "another-secret-" + "y" * 32), SECRET)


def test_alg_none_token_is_rejected():
    forged = jwt.encode(
        {"iss": tokens.ISSUER, "aud": tokens.AUDIENCE, "sub": "a@x.com", "iat": int(time.time()), "exp": int(time.time()) + 600},
        key=None, algorithm="none",
    )
    with pytest.raises(AuthError):
        verify_local(forged, SECRET)


@pytest.mark.parametrize("override", [{"aud": "someone-else"}, {"iss": "evil"}])
def test_wrong_audience_or_issuer_is_rejected(override):
    claims = {"iss": tokens.ISSUER, "aud": tokens.AUDIENCE, "sub": "a@x.com", "iat": int(time.time()), "exp": int(time.time()) + 600}
    with pytest.raises(AuthError):
        verify_local(jwt.encode({**claims, **override}, SECRET, algorithm="HS256"), SECRET)


def test_token_without_expiry_is_rejected():
    with pytest.raises(AuthError):
        verify_local(jwt.encode({"iss": tokens.ISSUER, "aud": tokens.AUDIENCE, "sub": "a@x.com", "iat": 1}, SECRET, algorithm="HS256"), SECRET)


def test_make_verifier_refuses_weak_or_missing_secret():
    with pytest.raises(ConfigError):
        make_verifier(Settings(_env_file=None, local_jwt_secret=""))
    with pytest.raises(ConfigError):
        make_verifier(Settings(_env_file=None, local_jwt_secret="short"))


def test_make_verifier_rejects_unknown_mode():
    with pytest.raises(ConfigError):
        make_verifier(settings(auth_mode="magic"))


def test_google_verifier_accepts_verified_email(monkeypatch):
    seen = {}

    def fake(token, request, audience):
        seen["audience"] = audience
        return {"email": "Pat@Gmail.com", "email_verified": True}

    monkeypatch.setattr("google.oauth2.id_token.verify_oauth2_token", fake)
    assert verify_google("tok", "client-123") == "pat@gmail.com"
    assert seen["audience"] == "client-123"


def test_google_verifier_rejects_unverified_email_and_invalid_tokens(monkeypatch):
    monkeypatch.setattr("google.oauth2.id_token.verify_oauth2_token", lambda *a, **k: {"email": "a@x.com", "email_verified": False})
    with pytest.raises(AuthError):
        verify_google("tok", "client")

    def boom(*a, **k):
        raise ValueError("Token has wrong audience")

    monkeypatch.setattr("google.oauth2.id_token.verify_oauth2_token", boom)
    with pytest.raises(AuthError):
        verify_google("tok", "client")


def test_google_client_id_sources(tmp_path):
    assert tokens.google_client_id(settings(google_client_id="explicit")) == "explicit"
    creds = tmp_path / "credentials.json"
    creds.write_text(json.dumps({"installed": {"client_id": "from-file"}}))
    assert tokens.google_client_id(settings(gmail_credentials_path=str(creds))) == "from-file"
    with pytest.raises(ConfigError):
        tokens.google_client_id(settings(gmail_credentials_path=str(tmp_path / "missing.json")))
