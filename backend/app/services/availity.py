"""Availity Eligibility Service — queries member eligibility + auth_org
via the Ronexa-hosted Availity proxy API (api-v2.ronexa.com).

Primary use: resolve MEMBER_NOT_FOUND cases from the Carelon portal by
checking Availity for the patient's current coverage and the
auth_org_name (Carelon, eviCore, etc.).

Phase 1 scope: proxy only. We authenticate, submit bulk jobs, list jobs,
and fetch job details. No local storage — Availity is the source of
truth for its own data. No Case DB writes.

Credentials load from DB (system_settings) first, fall back to env vars.
Session cookie is cached in-module with a mutex to prevent concurrent
logins (user's single-login-at-a-time requirement).
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import aiohttp

from app.core.settings import settings

logger = logging.getLogger(__name__)

# Session cache — Availity uses an access_token httpOnly cookie.
# We store the cookie value and refresh lazily. A lock prevents
# concurrent login requests (which would trigger multiple sessions).
_session_cookie: str | None = None
_session_expires_at: float = 0.0
_session_lock = asyncio.Lock()

# Default cookie lifetime assumption (no expires_in in Availity login response).
# Refresh every 45 min to stay comfortably inside typical session windows.
_DEFAULT_SESSION_SECONDS = 45 * 60


async def _get_availity_config() -> dict:
    """Load Availity credentials from DB, fall back to env vars."""
    config = {
        "base_url": settings.AVAILITY_BASE_URL or "https://api-v2.ronexa.com",
        "username": "",
        "password": "",
        "auto_send_enabled": False,
        "auto_send_interval_minutes": 30,
    }

    # DB first
    try:
        from app.db.database import async_session_factory
        from app.db.models import SystemSetting
        from app.intelligence.llm_config import decrypt_value

        async with async_session_factory() as db:
            for key in (
                "availity_base_url",
                "availity_username",
                "availity_password",
                "availity_auto_send_enabled",
                "availity_auto_send_interval_minutes",
            ):
                row = await db.get(SystemSetting, key)
                if row and row.value is not None:
                    val = row.value
                    if key == "availity_base_url" and val:
                        config["base_url"] = val
                    elif key == "availity_username":
                        config["username"] = val
                    elif key == "availity_password":
                        config["password"] = decrypt_value(val) if val else ""
                    elif key == "availity_auto_send_enabled":
                        config["auto_send_enabled"] = bool(val)
                    elif key == "availity_auto_send_interval_minutes":
                        try:
                            config["auto_send_interval_minutes"] = max(30, int(val))
                        except (TypeError, ValueError):
                            pass
    except Exception as e:
        logger.debug(f"Could not load Availity config from DB: {e}")

    # Env fallback
    if not config["username"]:
        config["username"] = settings.AVAILITY_USERNAME
    if not config["password"]:
        config["password"] = settings.AVAILITY_PASSWORD

    return config


async def _ensure_authenticated() -> tuple[str, str]:
    """Log in (if needed) and return (cookie_value, base_url).

    Uses a module-level lock so only one login request is ever in flight.
    Cached cookie is reused until _DEFAULT_SESSION_SECONDS have elapsed.
    """
    global _session_cookie, _session_expires_at

    config = await _get_availity_config()
    base_url = config["base_url"].rstrip("/")

    # Fast path — cookie still valid
    if _session_cookie and time.time() < _session_expires_at - 60:
        return _session_cookie, base_url

    async with _session_lock:
        # Re-check inside the lock (another task may have just logged in)
        if _session_cookie and time.time() < _session_expires_at - 60:
            return _session_cookie, base_url

        if not all([config["username"], config["password"]]):
            raise RuntimeError("Availity credentials not configured")

        login_url = f"{base_url}/api/auth/login"
        async with aiohttp.ClientSession() as http:
            async with http.post(
                login_url,
                json={"username": config["username"], "password": config["password"]},
                timeout=aiohttp.ClientTimeout(total=20),
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    raise RuntimeError(
                        f"Availity auth failed ({resp.status}): {body[:200]}"
                    )
                # Extract access_token cookie from Set-Cookie
                cookie_morsel = resp.cookies.get("access_token")
                if not cookie_morsel:
                    # Fall back to any cookie the server set (some APIs use bearer)
                    raise RuntimeError(
                        "Availity login succeeded but no access_token cookie"
                    )
                _session_cookie = cookie_morsel.value
                _session_expires_at = time.time() + _DEFAULT_SESSION_SECONDS
                logger.info("Availity authenticated — session cached")
                return _session_cookie, base_url


def _cookie_header(cookie: str) -> dict:
    return {"Cookie": f"access_token={cookie}"}


# ── Proxy calls ──


async def submit_job(cases: list[dict]) -> dict:
    """POST /api/eligibility-jobs with a batch of cases.

    Each case dict must have the 8 required Availity fields. Returns the
    {job_id, status, case_count} response payload.
    """
    cookie, base_url = await _ensure_authenticated()
    async with aiohttp.ClientSession() as http:
        async with http.post(
            f"{base_url}/api/eligibility-jobs",
            json={"cases": cases},
            headers=_cookie_header(cookie),
            timeout=aiohttp.ClientTimeout(total=60),
        ) as resp:
            body = await resp.text()
            if resp.status not in (200, 201):
                logger.warning(f"Availity submit_job failed ({resp.status}): {body[:300]}")
                raise RuntimeError(f"Availity submit failed ({resp.status}): {body[:200]}")
            import json
            return json.loads(body)


async def list_jobs(status: str | None = None, page: int = 1, page_size: int = 20) -> dict:
    """GET /api/eligibility-jobs — paginated job list."""
    cookie, base_url = await _ensure_authenticated()
    params: dict[str, Any] = {"page": page, "page_size": page_size}
    if status:
        params["status"] = status
    async with aiohttp.ClientSession() as http:
        async with http.get(
            f"{base_url}/api/eligibility-jobs",
            params=params,
            headers=_cookie_header(cookie),
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            if resp.status != 200:
                body = await resp.text()
                raise RuntimeError(f"Availity list_jobs failed ({resp.status}): {body[:200]}")
            return await resp.json()


async def get_job(job_id: str) -> dict:
    """GET /api/eligibility-jobs/{job_id} — full job detail."""
    cookie, base_url = await _ensure_authenticated()
    async with aiohttp.ClientSession() as http:
        async with http.get(
            f"{base_url}/api/eligibility-jobs/{job_id}",
            headers=_cookie_header(cookie),
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            if resp.status == 404:
                return {}
            if resp.status != 200:
                body = await resp.text()
                raise RuntimeError(f"Availity get_job failed ({resp.status}): {body[:200]}")
            return await resp.json()


async def delete_job(job_id: str) -> bool:
    """DELETE /api/eligibility-jobs/{job_id}."""
    cookie, base_url = await _ensure_authenticated()
    async with aiohttp.ClientSession() as http:
        async with http.delete(
            f"{base_url}/api/eligibility-jobs/{job_id}",
            headers=_cookie_header(cookie),
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            if resp.status == 204:
                return True
            if resp.status == 404:
                return False
            body = await resp.text()
            raise RuntimeError(f"Availity delete_job failed ({resp.status}): {body[:200]}")


async def test_connection() -> dict:
    """Try to authenticate. Returns {ok: bool, message: str}."""
    global _session_cookie, _session_expires_at
    # Force re-auth so we actually hit the login endpoint
    _session_cookie = None
    _session_expires_at = 0.0
    try:
        cookie, base_url = await _ensure_authenticated()
        return {"ok": True, "message": f"Authenticated against {base_url}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ── Case → Availity payload mapping ──


def _format_dob_mmddyyyy(dob: Any) -> str:
    """Availity expects MM/DD/YYYY. Case.dob is stored as a string in
    various formats ('MM/DD/YYYY', 'YYYY-MM-DD', 'MM/DD/YYYY 00:00:00').
    """
    if not dob:
        return ""
    s = str(dob).strip().split(" ")[0]  # drop any trailing timestamp
    # Already in MM/DD/YYYY
    if len(s) == 10 and s[2] == "/" and s[5] == "/":
        return s
    # YYYY-MM-DD → MM/DD/YYYY
    if len(s) == 10 and s[4] == "-" and s[7] == "-":
        return f"{s[5:7]}/{s[8:10]}/{s[0:4]}"
    return s  # pass through; Availity will reject if bad


def case_to_availity_payload(case) -> dict:
    """Map a Case ORM row to the 8-field Availity payload.

    Uses Case columns first, falls back to raw_data (Cosmos DB fields).
    """
    raw = case.raw_data or {}

    # State: 2-letter code required. raw_data has CenterState which may
    # be a full name ("TEXAS") or a 2-letter code. Use portal_compiler's
    # _STATE_MAP for full-name → 2-letter conversion.
    from app.compiler.portal_compiler import _STATE_MAP
    state_raw = (raw.get("CenterState") or "").strip().upper()
    if len(state_raw) == 2:
        state = state_raw
    else:
        state = _STATE_MAP.get(state_raw, "")

    return {
        "organisation":  raw.get("CenterDesc") or "Envision Imaging",
        "payer":         raw.get("CarrierDesc") or "",
        "center_npi":    case.center_npi or "",
        "policy_number": case.policy_num or "",
        "last_name":     case.last_name or "",
        "first_name":    case.first_name or "",
        "date_of_birth": _format_dob_mmddyyyy(case.dob),
        "state":         state,
    }


def payload_is_complete(payload: dict) -> bool:
    """All 8 fields must be non-empty for Availity to accept the row."""
    required = (
        "organisation", "payer", "center_npi", "policy_number",
        "last_name", "first_name", "date_of_birth", "state",
    )
    return all(str(payload.get(k, "")).strip() for k in required)
