"""Clinical Document Extractor — PDF → OCR text + tables.

Azure AI Document Intelligence:
  PDF → Document Intelligence (prebuilt-read) → clean text + tables → stored on case
  The pathway LLM reads this text directly when answering questions.

No LLM interpretation step. No local fallback. If Azure Doc Intelligence
is down or misconfigured, the error surfaces immediately.
"""
from __future__ import annotations

import logging

from app.core.settings import settings

logger = logging.getLogger(__name__)


async def extract_clinical_context(
    pdf_bytes: bytes,
    case_context: dict,
) -> dict:
    """Extract text and tables from a clinical PDF via Azure Document Intelligence.

    Args:
        pdf_bytes: Raw PDF file bytes
        case_context: Dict with cpt_code, cpt_desc, icd1, etc. (metadata only)

    Returns:
        {
            "text": str,           # Full OCR text content
            "tables": list[dict],  # Extracted tables
            "_meta": {...}         # Page count, confidence, etc.
        }

    Raises:
        RuntimeError: If Azure Document Intelligence is not configured
        Exception: If OCR fails (propagated — no silent fallback)
    """
    endpoint = getattr(settings, "AZURE_DOC_INTELLIGENCE_ENDPOINT", "")
    key = getattr(settings, "AZURE_DOC_INTELLIGENCE_KEY", "")

    if not endpoint or not key:
        raise RuntimeError(
            "Azure Document Intelligence not configured. "
            "Set AZURE_DOC_INTELLIGENCE_ENDPOINT and AZURE_DOC_INTELLIGENCE_KEY."
        )

    from azure.ai.documentintelligence import DocumentIntelligenceClient
    from azure.ai.documentintelligence.models import AnalyzeDocumentRequest
    from azure.core.credentials import AzureKeyCredential

    client = DocumentIntelligenceClient(
        endpoint=endpoint,
        credential=AzureKeyCredential(key),
    )

    # prebuilt-read: handles fax noise, handwriting, tables, rotated pages
    poller = client.begin_analyze_document(
        "prebuilt-read",
        AnalyzeDocumentRequest(bytes_source=pdf_bytes),
    )
    result = poller.result()

    # Full text
    full_text = result.content or ""

    # Tables (lab results, vitals, etc.)
    tables = []
    if result.tables:
        for table in result.tables:
            cells_by_row: dict[int, dict[int, str]] = {}
            for cell in table.cells:
                r = cell.row_index
                if r not in cells_by_row:
                    cells_by_row[r] = {}
                cells_by_row[r][cell.column_index] = cell.content
            tables.append({
                "row_count": table.row_count,
                "column_count": table.column_count,
                "rows": [
                    [cells_by_row.get(r, {}).get(c, "") for c in range(table.column_count)]
                    for r in range(table.row_count)
                ],
            })

    # Confidence
    page_count = len(result.pages) if result.pages else 0
    avg_confidence = 0.0
    if result.pages:
        word_confidences = []
        for page in result.pages:
            if page.words:
                word_confidences.extend(w.confidence for w in page.words if w.confidence)
        if word_confidences:
            avg_confidence = sum(word_confidences) / len(word_confidences)

    logger.info(
        f"Document Intelligence: {page_count} pages, "
        f"{len(full_text)} chars, {len(tables)} tables, "
        f"confidence={avg_confidence:.2f}"
    )

    return {
        "text": full_text,
        "tables": tables,
        "_meta": {
            "page_count": page_count,
            "method": "azure_document_intelligence",
            "ocr_confidence": round(avg_confidence, 3),
            "tables_found": len(tables),
            "text_length": len(full_text),
        },
    }
