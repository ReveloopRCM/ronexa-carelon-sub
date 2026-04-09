"""RingCentral Fax Service — sends clinical documentation via fax.

Uses the same OAuth2 JWT grant flow as the SMS service.
Endpoint: POST /restapi/v1.0/account/~/extension/~/fax (multipart/form-data)

Credentials: reads from DB (system_settings) first, falls back to env vars.
"""
from __future__ import annotations

import json
import logging
import time

import aiohttp

from app.core.settings import settings

logger = logging.getLogger(__name__)

# Default Carelon fax number
DEFAULT_CARELON_FAX = "+18007982068"

# Token cache
_access_token: str | None = None
_token_expires_at: float = 0


async def _get_rc_config() -> dict:
    """Load RC credentials from DB, fall back to env vars."""
    config = {
        "client_id": "",
        "client_secret": "",
        "jwt_token": "",
        "server_url": "https://platform.ringcentral.com",
        "fax_number": DEFAULT_CARELON_FAX,
        "fax_enabled": False,
    }

    # Try DB first
    try:
        from app.db.database import async_session_factory
        from app.db.models import SystemSetting
        from app.intelligence.llm_config import decrypt_value

        async with async_session_factory() as db:
            for key in ("rc_client_id", "rc_client_secret", "rc_jwt_token",
                        "rc_server_url", "carelon_fax_number", "rc_fax_enabled"):
                row = await db.get(SystemSetting, key)
                if row and row.value:
                    val = row.value
                    if key == "rc_client_id":
                        config["client_id"] = val
                    elif key == "rc_client_secret":
                        config["client_secret"] = decrypt_value(val) if val else ""
                    elif key == "rc_jwt_token":
                        config["jwt_token"] = decrypt_value(val) if val else ""
                    elif key == "rc_server_url":
                        config["server_url"] = val
                    elif key == "carelon_fax_number":
                        config["fax_number"] = val
                    elif key == "rc_fax_enabled":
                        config["fax_enabled"] = bool(val)
    except Exception as e:
        logger.debug(f"Could not load RC config from DB, using env vars: {e}")

    # Fall back to env vars if DB values are empty
    if not config["client_id"]:
        config["client_id"] = settings.RC_CLIENT_ID
    if not config["client_secret"]:
        config["client_secret"] = settings.RC_CLIENT_SECRET
    if not config["jwt_token"]:
        config["jwt_token"] = settings.RC_JWT_TOKEN
    if config["server_url"] == "https://platform.ringcentral.com" and settings.RC_SERVER_URL:
        config["server_url"] = settings.RC_SERVER_URL

    # If we have env vars, consider fax enabled
    if not config["fax_enabled"] and all([config["client_id"], config["client_secret"], config["jwt_token"]]):
        config["fax_enabled"] = True

    return config


async def _ensure_authenticated() -> tuple[str, str]:
    """Authenticate via JWT grant, return (access_token, server_url)."""
    global _access_token, _token_expires_at

    config = await _get_rc_config()

    if _access_token and time.time() < _token_expires_at - 60:
        return _access_token, config["server_url"]

    if not all([config["client_id"], config["client_secret"], config["jwt_token"]]):
        raise RuntimeError("RingCentral credentials not configured")

    token_url = f"{config['server_url']}/restapi/oauth/token"

    async with aiohttp.ClientSession() as session:
        async with session.post(
            token_url,
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "assertion": config["jwt_token"],
            },
            auth=aiohttp.BasicAuth(config["client_id"], config["client_secret"]),
        ) as resp:
            if resp.status != 200:
                body = await resp.text()
                raise RuntimeError(f"RC auth failed ({resp.status}): {body}")
            data = await resp.json()
            _access_token = data["access_token"]
            _token_expires_at = time.time() + data.get("expires_in", 3600)
            logger.info("RingCentral authenticated for fax")
            return _access_token, config["server_url"]


async def send_fax(
    to: str,
    pdf_bytes: bytes,
    filename: str = "clinical_notes.pdf",
    cover_text: str = "",
) -> dict:
    """Send a fax with PDF attachment via RingCentral.

    Args:
        to: Fax number (E.164 format, e.g. +18007982068)
        pdf_bytes: Raw PDF bytes to attach
        filename: Name for the attachment
        cover_text: Cover page text

    Returns:
        {"ok": True, "message_id": "...", "status": "Queued"} on success
        {"ok": False, "error": "..."} on failure
    """
    token, server_url = await _ensure_authenticated()
    fax_url = f"{server_url}/restapi/v1.0/account/~/extension/~/fax"

    form = aiohttp.FormData()
    # Part 1: JSON body with recipient and cover page
    json_body = {
        "to": [{"phoneNumber": to}],
        "faxResolution": "High",
    }
    if cover_text:
        json_body["coverPageText"] = cover_text

    form.add_field(
        "json",
        json.dumps(json_body),
        content_type="application/json",
    )
    # Part 2: PDF attachment
    form.add_field(
        "attachment",
        pdf_bytes,
        filename=filename,
        content_type="application/pdf",
    )

    async with aiohttp.ClientSession() as session:
        async with session.post(
            fax_url,
            data=form,
            headers={"Authorization": f"Bearer {token}"},
        ) as resp:
            body = await resp.json()
            if resp.status in (200, 201, 202):
                msg_id = body.get("id", "")
                status = body.get("messageStatus", "Queued")
                logger.info(f"Fax sent: id={msg_id}, status={status}, to={to}")
                return {"ok": True, "message_id": str(msg_id), "status": status}
            else:
                error = body.get("message", str(body))
                logger.error(f"Fax failed ({resp.status}): {error}")
                return {"ok": False, "error": error}


async def fax_clinical_notes(
    clinical_blob_key: str,
    order_id: str,
    patient_name: str,
    cpt_code: str,
    member_id: str = "",
    dob: str = "",
) -> dict:
    """Fetch clinical notes from blob storage and fax to Carelon.

    Cover sheet format (matches Carelon reference):
        Please review for authorization for case #{OrderID}
        Member ID {MemberNumber}
        DOB: {DOB}
        CPT {CPTCode}
    """
    import asyncio
    from app.ingest.blob_fetcher import fetch_blob_bytes

    config = await _get_rc_config()
    if not config["fax_enabled"]:
        logger.info("Fax disabled — skipping")
        return {"ok": False, "error": "Fax is disabled"}

    fax_number = config["fax_number"]

    logger.info(f"Faxing clinical notes for Order {order_id} ({patient_name}, CPT {cpt_code})")

    # Fetch clinical PDF from blob storage (sync call — run in thread to avoid blocking)
    pdf_bytes = await asyncio.to_thread(fetch_blob_bytes, clinical_blob_key)
    if not pdf_bytes:
        return {"ok": False, "error": f"Could not fetch blob: {clinical_blob_key}"}

    # Cover sheet matching the reference format
    cover_text = (
        f"Please review for authorization for case #{order_id}\n"
        f"Member ID {member_id}\n"
        f"DOB: {dob}\n"
        f"CPT {cpt_code}"
    )

    filename = f"clinical_notes_{order_id}.pdf"

    return await send_fax(
        to=fax_number,
        pdf_bytes=pdf_bytes,
        filename=filename,
        cover_text=cover_text,
    )
