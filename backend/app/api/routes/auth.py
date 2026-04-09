"""Authentication routes — proxy login via Ronexa API with local fallback.

Login flow:
  1. Try Ronexa API (api-v2.ronexa.com/api/auth/login)
  2. Fallback: check ADMIN_USERNAME + ADMIN_PASSWORD_HASH env vars
  3. On success: sign JWT, set httpOnly cookie

Token: JWT signed with JWT_SECRET (or SETTINGS_ENCRYPTION_KEY fallback).
Cookie: 'ronexa_session', httpOnly, 8h expiry.
"""
from __future__ import annotations

import logging
import os
import time

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from fastapi.security import APIKeyCookie

from app.core.settings import settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["auth"])

COOKIE_NAME = "ronexa_session"
TOKEN_EXPIRY_HOURS = 8

# Reusable security scheme for protected endpoints
_cookie_scheme = APIKeyCookie(name=COOKIE_NAME, auto_error=False)


def _get_jwt_secret() -> str:
    """Get JWT signing secret."""
    if settings.JWT_SECRET:
        return settings.JWT_SECRET
    # Fallback to encryption key
    key = os.environ.get("SETTINGS_ENCRYPTION_KEY", "")
    if key:
        return key
    # Production must have a secret configured
    if settings.ENVIRONMENT not in ("local", "development", "docker"):
        raise RuntimeError("JWT_SECRET or SETTINGS_ENCRYPTION_KEY must be set in production")
    return "dev-jwt-secret-not-for-production"


def _sign_token(username: str, source: str) -> str:
    """Sign a JWT with username and source."""
    import jwt
    payload = {
        "sub": username,
        "source": source,  # "ronexa" or "local"
        "iat": int(time.time()),
        "exp": int(time.time()) + TOKEN_EXPIRY_HOURS * 3600,
    }
    return jwt.encode(payload, _get_jwt_secret(), algorithm="HS256")


def _decode_token(token: str) -> dict | None:
    """Decode and validate a JWT. Returns payload or None."""
    import jwt
    try:
        return jwt.decode(token, _get_jwt_secret(), algorithms=["HS256"])
    except (jwt.ExpiredSignatureError, jwt.InvalidTokenError):
        return None


async def require_auth(token: str | None = Depends(_cookie_scheme)) -> dict:
    """FastAPI dependency — validates JWT cookie. Use on protected routes.

    Usage in other routers:
        from app.api.routes.auth import require_auth
        @router.get("/protected", dependencies=[Depends(require_auth)])
    """
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    payload = _decode_token(token)
    if not payload:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    return payload


async def _try_ronexa_login(username: str, password: str) -> dict | None:
    """Attempt login via Ronexa API. Returns user info or None."""
    import httpx

    ronexa_url = settings.RONEXA_API_URL
    if not ronexa_url:
        return None

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"{ronexa_url}/api/auth/login",
                json={"username": username, "password": password},
            )
            if resp.status_code == 200:
                data = resp.json()
                if data.get("success") is not False:
                    # Extract token from response or cookie
                    ronexa_token = None
                    for cookie_name, cookie_value in resp.cookies.items():
                        if "token" in cookie_name.lower() or "session" in cookie_name.lower():
                            ronexa_token = cookie_value
                            break

                    # Try to get user info
                    user_info = {"username": username}
                    if ronexa_token:
                        try:
                            me_resp = await client.get(
                                f"{ronexa_url}/api/users/me",
                                cookies=resp.cookies,
                            )
                            if me_resp.status_code == 200:
                                user_info.update(me_resp.json())
                        except Exception:
                            pass

                    logger.info(f"Ronexa login successful: {username}")
                    return user_info
                else:
                    logger.info(f"Ronexa login rejected: {username} — {data.get('error')}")
                    return None
            else:
                logger.info(f"Ronexa login failed ({resp.status_code}): {username}")
                return None
    except Exception as e:
        logger.warning(f"Ronexa API unreachable: {e}")
        return None


def _try_local_login(username: str, password: str) -> dict | None:
    """Check against local admin credentials from env vars."""
    admin_user = settings.ADMIN_USERNAME
    admin_hash = settings.ADMIN_PASSWORD_HASH

    if not admin_user or not admin_hash:
        return None

    if username != admin_user:
        return None

    try:
        import bcrypt
        if bcrypt.checkpw(password.encode(), admin_hash.encode()):
            logger.info(f"Local admin login successful: {username}")
            return {"username": username, "source": "local"}
        else:
            return None
    except Exception as e:
        logger.error(f"bcrypt check failed: {e}")
        return None


@router.post("/proxy-login")
async def proxy_login(request: Request):
    """Login — try Ronexa API first, fall back to local admin."""
    body = await request.json()
    username = body.get("username", "").strip()
    password = body.get("password", "")

    if not username or not password:
        return JSONResponse(
            status_code=400,
            content={"success": False, "error": "Username and password required"},
        )

    # Try Ronexa API first
    user_info = await _try_ronexa_login(username, password)
    source = "ronexa"

    # Fallback to local admin
    if user_info is None:
        user_info = _try_local_login(username, password)
        source = "local"

    if user_info is None:
        return JSONResponse(
            status_code=401,
            content={"success": False, "error": "Invalid username or password"},
        )

    # Sign our JWT
    token = _sign_token(username, source)

    response = JSONResponse(content={
        "success": True,
        "username": username,
        "source": source,
    })
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        httponly=True,
        secure=False,  # TODO: set True after SSL is enabled on carelon.ronexa.com
        samesite="lax",
        max_age=TOKEN_EXPIRY_HOURS * 3600,
        path="/",
    )
    return response


@router.get("/me")
async def get_me(request: Request):
    """Validate session and return user info."""
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return JSONResponse(
            status_code=401,
            content={"authenticated": False, "error": "No session"},
        )

    payload = _decode_token(token)
    if not payload:
        return JSONResponse(
            status_code=401,
            content={"authenticated": False, "error": "Invalid or expired session"},
        )

    return {
        "authenticated": True,
        "username": payload.get("sub"),
        "source": payload.get("source"),
        "expires_at": payload.get("exp"),
    }


@router.post("/proxy-logout")
async def proxy_logout():
    """Clear session cookie."""
    response = JSONResponse(content={"success": True})
    response.delete_cookie(key=COOKIE_NAME, path="/")
    return response
