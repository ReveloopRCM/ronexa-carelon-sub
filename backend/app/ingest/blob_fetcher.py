"""Azure Blob Storage fetcher — download clinical PDFs by blob key.

Used to pull clinical attachments referenced in Mongo payload.
Each case has two blob references:
  - FileKey             → order PDF (stored on case.file_key)
  - ClinicalAttachments → clinical notes PDF (extracted via vision LLM)

This module handles downloading blobs to memory (bytes) for processing.
No local disk writes — PDFs go straight into the extraction pipeline.
"""
from __future__ import annotations

import logging

from azure.storage.blob import BlobServiceClient

from app.core.settings import settings

logger = logging.getLogger(__name__)

_blob_service: BlobServiceClient | None = None


def _get_container():
    """Lazy-init blob container client."""
    global _blob_service
    if _blob_service is None:
        if not settings.AZURE_BLOB_CONNECTION_STRING:
            raise RuntimeError("AZURE_BLOB_CONNECTION_STRING not configured")
        _blob_service = BlobServiceClient.from_connection_string(
            settings.AZURE_BLOB_CONNECTION_STRING
        )
    return _blob_service.get_container_client(settings.AZURE_BLOB_CONTAINER)


def fetch_blob(blob_key: str) -> bytes:
    """Download a blob by key and return its bytes.

    Args:
        blob_key: The blob name/key (e.g. "23b22768-...-.pdf")

    Returns:
        Raw bytes of the blob content

    Raises:
        RuntimeError: If blob is not found or download fails
    """
    if not blob_key:
        raise ValueError("blob_key is required")

    # Blob names always have .pdf suffix in storage, but some Mongo fields
    # (e.g. ClinicalAttachments) omit it — normalize
    if not blob_key.lower().endswith(".pdf"):
        blob_key = f"{blob_key}.pdf"

    container = _get_container()
    blob_client = container.get_blob_client(blob_key)

    try:
        downloader = blob_client.download_blob()
        data = downloader.readall()
        logger.info(f"Downloaded blob: {blob_key} ({len(data)} bytes)")
        return data
    except Exception as e:
        logger.error(f"Failed to download blob {blob_key}: {e}")
        raise RuntimeError(f"Blob download failed for {blob_key}: {e}") from e


def fetch_clinical_pdf(case_dict: dict) -> bytes | None:
    """Fetch the clinical attachment PDF for a case.

    Uses clinical_blob_key only (ClinicalHistoryFileKey from Cosmos).
    Does NOT fall back to file_key — that's the order PDF, not clinical notes.

    Returns None if no clinical blob key is available.
    """
    blob_key = case_dict.get("clinical_blob_key")

    if not blob_key:
        logger.warning(
            f"No clinical blob key for case {case_dict.get('exam_id', '?')}"
        )
        return None

    try:
        return fetch_blob(blob_key)
    except RuntimeError:
        return None


def blob_exists(blob_key: str) -> bool:
    """Check if a blob exists without downloading it."""
    if not blob_key:
        return False
    if not blob_key.lower().endswith(".pdf"):
        blob_key = f"{blob_key}.pdf"
    container = _get_container()
    blob_client = container.get_blob_client(blob_key)
    return blob_client.exists()


def fetch_blob_bytes(blob_key: str) -> bytes | None:
    """Download raw bytes from blob storage.

    Args:
        blob_key: Blob path/key (e.g. "954391f3-c84f-42bf-bb40-9ca1b16f9afb.pdf")

    Returns:
        Raw bytes on success, None on failure.
    """
    try:
        if not blob_key.lower().endswith(".pdf"):
            blob_key = f"{blob_key}.pdf"
        container = _get_container()
        blob_client = container.get_blob_client(blob_key)
        if not blob_client.exists():
            logger.warning(f"Blob not found: {blob_key}")
            return None
        data = blob_client.download_blob().readall()
        logger.info(f"Fetched blob: {blob_key} ({len(data)} bytes)")
        return data
    except Exception as e:
        logger.error(f"Failed to fetch blob {blob_key}: {e}")
        return None


def upload_auth_pdf(pdf_bytes: bytes, case_data: dict, order_id: str) -> str:
    """Upload authorization PDF to blob storage.

    Naming convention:
        auth-pdfs/{date}/{LASTNAME_FIRSTNAME}_{OrderID}.pdf

    Args:
        pdf_bytes: Raw PDF content
        case_data: Case dict with first_name, last_name
        order_id: Portal order ID (e.g. "283957822")

    Returns:
        Blob key (path) for the uploaded PDF
    """
    from datetime import date

    last_name = (case_data.get("last_name") or "UNKNOWN").upper().replace(" ", "_")
    first_name = (case_data.get("first_name") or "UNKNOWN").upper().replace(" ", "_")
    today = date.today().isoformat()  # YYYY-MM-DD

    blob_key = f"auth-pdfs/{today}/{last_name}_{first_name}_{order_id}.pdf"

    container = _get_container()
    blob_client = container.get_blob_client(blob_key)
    from azure.storage.blob import ContentSettings
    blob_client.upload_blob(pdf_bytes, overwrite=True, content_settings=ContentSettings(
        content_type="application/pdf",
    ))

    logger.info(f"Uploaded auth PDF: {blob_key} ({len(pdf_bytes)} bytes)")
    return blob_key
