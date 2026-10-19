"""Password hashing and tokens.

Passwords use the standard library's scrypt KDF; tokens are JWTs (PyJWT).
Two token kinds exist, mirroring Power BI:

* ``user``  - an interactive session (like an Entra ID access token)
* ``embed`` - scoped to one report with an *effective identity*
  (username + RLS roles), for embedding in third-party apps
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import time
from typing import Any

import jwt

_N, _R, _P = 2**14, 8, 1


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    return "scrypt$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(dk).decode()


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, salt_b64, dk_b64 = stored.split("$")
    except ValueError:
        return False
    if scheme != "scrypt":
        return False
    salt = base64.b64decode(salt_b64)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    return hmac.compare_digest(dk, base64.b64decode(dk_b64))


class TokenError(ValueError):
    pass


def issue_token(secret: str, claims: dict[str, Any], ttl_seconds: int) -> str:
    now = int(time.time())
    payload = {"iat": now, "exp": now + ttl_seconds, "iss": "openbi", **claims}
    return jwt.encode(payload, secret, algorithm="HS256")


def decode_token(secret: str, token: str) -> dict[str, Any]:
    try:
        return jwt.decode(token, secret, algorithms=["HS256"], issuer="openbi")
    except jwt.PyJWTError as e:
        raise TokenError(str(e)) from e
