"""Stateless, invite-bound sessions shared by HTTP and Icecast listener checks."""
import hashlib
import hmac
import secrets
import time
from http.cookies import SimpleCookie, CookieError

COOKIE = "tsdfm_session"
MAX_AGE = 7 * 24 * 60 * 60


def issue_session(secret: str) -> str:
    payload = f"{int(time.time()) + MAX_AGE}.{secrets.token_hex(16)}"
    signature = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{signature}"


def valid_session(token: str | None, secret: str) -> bool:
    if not token or len(token) > 256:
        return False
    try:
        expires, nonce, signature = token.split(".")
        expected = hmac.new(secret.encode(), f"{expires}.{nonce}".encode(), hashlib.sha256).hexdigest()
        return int(expires) > time.time() and hmac.compare_digest(signature, expected)
    except (ValueError, TypeError):
        return False


def session_from_cookie(header: str) -> str | None:
    try:
        cookies = SimpleCookie()
        cookies.load(header)
        return cookies[COOKIE].value if COOKIE in cookies else None
    except CookieError:
        return None
