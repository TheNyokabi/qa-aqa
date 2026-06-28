"""FastAPI dependencies for auth + role checks."""
from __future__ import annotations

from fastapi import Depends, Header, HTTPException

from .auth import User, decode_token

ROLE_ORDER = {"viewer": 0, "reviewer": 1, "admin": 2}


def current_user(authorization: str | None = Header(None)) -> User:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing Authorization Bearer token")
    token = authorization[len("Bearer "):]
    user = decode_token(token)
    if user is None:
        raise HTTPException(status_code=401, detail="invalid token")
    return user


def require_role(min_role: str):
    threshold = ROLE_ORDER[min_role]

    def _checker(user: User = Depends(current_user)) -> User:
        if ROLE_ORDER.get(user.role, -1) < threshold:
            raise HTTPException(status_code=403, detail=f"requires role >= {min_role}")
        return user

    return _checker
