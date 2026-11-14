from __future__ import annotations

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .config import settings
from .firebase import admin_app

bearer = HTTPBearer(auto_error=False)
ADMIN_EMAIL = "elias.liassides@gmail.com"


def admin_claims(claims: dict) -> bool:
    """Only the designated, verified Firebase identity receives admin access."""
    return claims.get("email_verified") is True and str(claims.get("email", "")).strip().casefold() == ADMIN_EMAIL


def is_admin(request: Request) -> bool:
    return getattr(request.state, "is_admin", False) is True


def current_user(request: Request, credentials: HTTPAuthorizationCredentials | None = Depends(bearer)) -> str:
    if credentials is None:
        raise HTTPException(401, "Sign in to continue", headers={"WWW-Authenticate": "Bearer"})
    if not settings.firebase_project_id:
        raise HTTPException(503, "Firebase Authentication is not configured")
    try:
        from firebase_admin import auth

        claims = auth.verify_id_token(credentials.credentials, app=admin_app())
        user_id = claims.get("uid") or claims.get("sub")
        if not user_id or claims.get("email_verified") is not True:
            raise HTTPException(403, "Verify your email before using the service")
        request.state.is_admin = admin_claims(claims)
        return str(user_id)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(401, "Invalid or expired sign-in token") from exc
