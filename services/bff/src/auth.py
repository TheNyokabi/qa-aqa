"""JWT + password helpers. Dev-only auth model."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any

import yaml
from jose import JWTError, jwt
from passlib.context import CryptContext
from pydantic import BaseModel

from .config import JWT_ALG, JWT_SECRET, JWT_TTL_HOURS, SEED_DIR, DEFAULT_TENANT

_pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")


class User(BaseModel):
    email: str
    role: str
    urn: str
    tenant_id: str = DEFAULT_TENANT


_users: dict[str, dict[str, Any]] = {}


def _email_slug(email: str) -> str:
    s = re.sub(r"[^A-Za-z0-9_\-]+", "-", email.split("@", 1)[0])
    return s.strip("-")


def load_users() -> None:
    """Reads seed/users.yaml and bcrypts each password into the in-memory store."""
    path = SEED_DIR / "users.yaml"
    if not path.exists():
        return
    rows = yaml.safe_load(path.read_text()) or []
    for row in rows:
        email = str(row["email"]).lower()
        _users[email] = {
            "email": email,
            "role": row.get("role", "viewer"),
            "hash": _pwd.hash(row["password"]),
            "urn": f"urn:qa-aqa:user:{_email_slug(email)}",
        }


def verify_password(email: str, password: str) -> User | None:
    email = email.lower()
    u = _users.get(email)
    if not u:
        return None
    if not _pwd.verify(password, u["hash"]):
        return None
    return User(email=u["email"], role=u["role"], urn=u["urn"])


def create_token(user: User) -> str:
    now = datetime.now(tz=timezone.utc)
    payload = {
        "sub": user.email,
        "email": user.email,
        "role": user.role,
        "urn": user.urn,
        "tenant_id": user.tenant_id,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(hours=JWT_TTL_HOURS)).timestamp()),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALG)


def decode_token(token: str) -> User | None:
    try:
        claims = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALG])
    except JWTError:
        return None
    return User(
        email=claims["email"],
        role=claims["role"],
        urn=claims["urn"],
        tenant_id=claims.get("tenant_id", DEFAULT_TENANT),
    )
