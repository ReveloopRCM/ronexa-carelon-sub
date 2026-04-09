"""MFA Resolver — Microsoft Graph API OTP retrieval for Carelon MFA.

Polls the designated mailbox for OTP emails from Carelon's Okta,
extracts the 6-digit code, returns it for the login flow.

Usage (recommended prepare/wait pattern):

    resolver = MFAResolver()
    await resolver.prepare()           # flush stale emails, set cutoff
    # ... click "Send Email" in browser ...
    otp = await resolver.wait_for_otp() # poll for fresh email only

Usage (simple one-shot):

    resolver = MFAResolver()
    otp = await resolver.get_otp()     # flush + poll in one call

Requires Azure AD app registration with Mail.ReadWrite permission.
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone

import httpx

from app.core.settings import settings

logger = logging.getLogger(__name__)

GRAPH_TOKEN_URL = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
GRAPH_MESSAGES_URL = "https://graph.microsoft.com/v1.0/users/{mailbox}/messages"


class MFAResolver:
    """Resolves MFA OTP by polling Graph API for incoming emails."""

    def __init__(
        self,
        tenant_id: str | None = None,
        client_id: str | None = None,
        client_secret: str | None = None,
        mailbox: str | None = None,
    ):
        self.tenant_id = tenant_id or settings.GRAPH_TENANT_ID
        self.client_id = client_id or settings.GRAPH_CLIENT_ID
        self.client_secret = client_secret or settings.GRAPH_CLIENT_SECRET
        self.mailbox = mailbox or settings.GRAPH_MAILBOX
        self._token: str | None = None
        self._token_expires: datetime | None = None
        self._cutoff: datetime | None = None

    async def prepare(self):
        """Flush stale OTP emails and record cutoff time.

        Call this BEFORE triggering the MFA email (before clicking
        'Send Email' in the browser). This ensures the fresh email
        won't be confused with a stale one from a previous attempt.

        After calling prepare(), call wait_for_otp() to poll for the code.
        """
        token = await self._get_token()
        await self._flush_stale_otp_emails(token)
        self._cutoff = datetime.now(timezone.utc)
        logger.info(f"MFA prepared — cutoff set to {self._cutoff.isoformat()}")

    async def wait_for_otp(
        self,
        poll_interval_s: float = 3.0,
        timeout_s: float = 90.0,
    ) -> str:
        """Poll for OTP email received after the prepare() cutoff.

        Must call prepare() first.

        Returns:
            6-digit OTP string.

        Raises:
            MFATimeoutError: If no OTP received within timeout.
        """
        if not self._cutoff:
            raise RuntimeError("Must call prepare() before wait_for_otp()")

        token = await self._get_token()
        deadline = self._cutoff + timedelta(seconds=timeout_s)

        logger.info(
            f"MFA polling started (mailbox={self.mailbox}, "
            f"timeout={timeout_s}s, cutoff={self._cutoff.isoformat()})"
        )

        # Give the email a moment to arrive
        await asyncio.sleep(3)

        attempt = 0
        while datetime.now(timezone.utc) < deadline:
            attempt += 1
            otp = await self._extract_otp_from_email(token, self._cutoff)

            if otp:
                logger.info(f"OTP found on attempt {attempt}: ***{otp[-2:]}")
                self._cutoff = None
                return otp

            logger.debug(f"MFA poll attempt {attempt}, no OTP yet")
            await asyncio.sleep(poll_interval_s)

        raise MFATimeoutError(
            f"No OTP email received within {timeout_s}s for {self.mailbox}"
        )

    async def get_otp(
        self,
        subject_pattern: str = "verification code",
        otp_regex: str = r"\d{6}",
        poll_interval_s: float = 3.0,
        max_attempts: int = 20,
        since_minutes: int = 5,
    ) -> str:
        """Convenience: prepare + wait_for_otp in one call.

        Use prepare() + wait_for_otp() instead when you control
        the timing of the MFA trigger (e.g., clicking 'Send Email').
        """
        await self.prepare()
        return await self.wait_for_otp(
            poll_interval_s=poll_interval_s,
            timeout_s=max_attempts * poll_interval_s,
        )

    async def _flush_stale_otp_emails(self, token: str):
        """Mark all existing unread OTP emails as read.

        This prevents picking up a stale OTP from a previous auth attempt.
        """
        url = GRAPH_MESSAGES_URL.format(mailbox=self.mailbox)
        filter_str = (
            "isRead eq false and "
            "contains(subject, 'verification code')"
        )

        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                url,
                params={
                    "$filter": filter_str,
                    "$select": "id,subject,receivedDateTime",
                    "$top": "20",
                },
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
            )

            if resp.status_code != 200:
                logger.warning(f"Flush stale emails failed: HTTP {resp.status_code}")
                return

            messages = resp.json().get("value", [])
            if messages:
                logger.info(f"Flushing {len(messages)} stale OTP emails")
                for msg in messages:
                    await self._mark_message_as_read(client, token, msg["id"])
            else:
                logger.debug("No stale OTP emails to flush")

    async def _extract_otp_from_email(
        self, token: str, after: datetime
    ) -> str | None:
        """Query Graph API for recent MFA emails and extract OTP."""
        url = GRAPH_MESSAGES_URL.format(mailbox=self.mailbox)
        after_iso = after.strftime("%Y-%m-%dT%H:%M:%SZ")
        filter_str = (
            f"isRead eq false and "
            f"receivedDateTime ge {after_iso} and "
            f"contains(subject, 'verification code')"
        )

        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                url,
                params={
                    "$filter": filter_str,
                    "$select": "id,subject,body,receivedDateTime",
                    "$orderby": "receivedDateTime desc",
                    "$top": "5",
                },
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
            )

            if resp.status_code != 200:
                logger.warning(f"Graph API returned {resp.status_code}")
                return None

            messages = resp.json().get("value", [])
            if not messages:
                return None

            for msg in messages:
                otp = self._extract_otp_from_message(msg)
                if otp:
                    # Mark as read so we don't pick it up again
                    await self._mark_message_as_read(client, token, msg["id"])
                    return otp

            return None

    def _extract_otp_from_message(self, message: dict) -> str | None:
        """Extract 6-digit OTP from email body."""
        body_content = message.get("body", {}).get("content", "")
        body_type = message.get("body", {}).get("contentType", "text")

        # Strip HTML tags
        if body_type == "html" or "<" in body_content:
            clean = re.sub(r"<[^>]+>", " ", body_content)
            clean = re.sub(r"&nbsp;", " ", clean)
            clean = re.sub(r"&#\d+;", " ", clean)
            clean = re.sub(r"\s+", " ", clean).strip()
        else:
            clean = body_content

        logger.debug(
            f"Email body preview: {clean[:200]}"
        )

        # Pattern 1: "code: XXXXXX" or "code is XXXXXX"
        code_match = re.search(
            r"(?:code|passcode|otp|verification)[:\s]+(\d{6})\b",
            clean,
            re.IGNORECASE,
        )
        if code_match:
            return code_match.group(1)

        # Pattern 2: Standalone 6-digit number (fallback)
        all_matches = re.findall(r"\b(\d{6})\b", clean)
        if all_matches:
            return all_matches[0]

        logger.warning(f"No OTP found in email body: {clean[:200]}")
        return None

    async def _mark_message_as_read(
        self, client: httpx.AsyncClient, token: str, message_id: str
    ):
        """Mark email as read to avoid reprocessing."""
        try:
            await client.patch(
                f"https://graph.microsoft.com/v1.0/users/{self.mailbox}/messages/{message_id}",
                json={"isRead": True},
                headers={"Authorization": f"Bearer {token}"},
            )
        except Exception as e:
            logger.warning(f"Failed to mark message as read: {e}")

    async def _get_token(self) -> str:
        """Get or refresh Graph API access token."""
        if (
            self._token
            and self._token_expires
            and datetime.now(timezone.utc) < self._token_expires
        ):
            return self._token

        url = GRAPH_TOKEN_URL.format(tenant=self.tenant_id)
        data = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "scope": "https://graph.microsoft.com/.default",
            "grant_type": "client_credentials",
        }

        async with httpx.AsyncClient() as client:
            resp = await client.post(url, data=data)
            resp.raise_for_status()
            token_data = resp.json()

        self._token = token_data["access_token"]
        self._token_expires = datetime.now(timezone.utc) + timedelta(
            seconds=token_data.get("expires_in", 3600) - 60
        )

        return self._token


class MFATimeoutError(Exception):
    pass
