"""Fax Confirmation PDF — audit-grade proof that a fax was delivered.

Produced by the RingCentral fax-status poller when a fax transitions to
`Sent`. Uploaded to Azure Blob at:
    fax-confirmations/{YYYY-MM-DD}/{exam_id}_{message_id}.pdf

Retrieved by the frontend via GET /api/cases/{case_id}/fax-confirmation.

All timestamps rendered in America/Chicago (matches the date-picker fix).
"""
from __future__ import annotations

import io
import logging
from datetime import datetime, timezone
from typing import Any

from reportlab.lib import colors
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import (
    SimpleDocTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)

logger = logging.getLogger(__name__)

CT_TZ = "America/Chicago"


def _fmt_ct(dt: datetime | None) -> str:
    """Format a datetime (any tz, or naive-UTC) as 'Apr 22, 2026 1:14:00 PM CT'."""
    if not dt:
        return "—"
    # Ensure tz-aware; assume naive = UTC (what RC returns + what our DB stores)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    try:
        from zoneinfo import ZoneInfo
        local = dt.astimezone(ZoneInfo(CT_TZ))
    except Exception:
        local = dt
    return local.strftime("%b %d, %Y %-I:%M:%S %p CT")


def _fmt_duration(start: datetime | None, end: datetime | None) -> str:
    if not start or not end:
        return "—"
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    delta = end - start
    secs = int(delta.total_seconds())
    if secs < 0:
        return "—"
    m, s = divmod(secs, 60)
    if m == 0:
        return f"{s}s"
    return f"{m}m {s}s"


def _row(styles, label: str, value: str) -> list:
    return [
        Paragraph(f"<b>{label}</b>", styles["cell_label"]),
        Paragraph(value or "—", styles["cell_value"]),
    ]


def build_fax_confirmation_pdf(
    case: Any,                    # app.db.models.Case
    fax_status_data: dict,        # result from ringcentral_fax.get_fax_status()
    submitted_at: datetime | None,# when we POSTed to RC (fax_sent audit timestamp)
    approved_by: str | None,      # fax_rep actor from the fax_approved audit event
    approved_at: datetime | None, # fax_approved audit timestamp
) -> bytes:
    """Build the PDF and return its bytes.

    Args:
        case: the Case ORM row.
        fax_status_data: normalized result from get_fax_status() — must have
            'status', 'last_modified_at', 'fax_page_count', 'to', 'message_id'.
        submitted_at: when we handed off to RingCentral. From fax_sent audit.
        approved_by: rep who approved the fax (e.g. "fax_rep").
        approved_at: timestamp of the fax_approved audit event.

    Returns:
        Raw PDF bytes. The caller uploads to Azure Blob.
    """
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=LETTER,
        leftMargin=0.75 * inch,
        rightMargin=0.75 * inch,
        topMargin=0.75 * inch,
        bottomMargin=0.75 * inch,
        title=f"Fax Confirmation — {case.exam_id}",
        author="Ronexa",
    )

    # ── Styles ──
    base = getSampleStyleSheet()
    styles = {
        "title": ParagraphStyle("title", parent=base["Heading1"],
                                fontSize=16, spaceAfter=6, textColor=colors.HexColor("#1f2937")),
        "subtitle": ParagraphStyle("subtitle", parent=base["Normal"],
                                   fontSize=9, textColor=colors.HexColor("#6b7280"), spaceAfter=18),
        "section": ParagraphStyle("section", parent=base["Heading2"],
                                  fontSize=11, spaceBefore=10, spaceAfter=4,
                                  textColor=colors.HexColor("#111827")),
        "cell_label": ParagraphStyle("cell_label", parent=base["Normal"],
                                     fontSize=9, textColor=colors.HexColor("#6b7280")),
        "cell_value": ParagraphStyle("cell_value", parent=base["Normal"],
                                     fontSize=10, textColor=colors.HexColor("#111827")),
        "footer": ParagraphStyle("footer", parent=base["Normal"],
                                 fontSize=8, textColor=colors.HexColor("#9ca3af"),
                                 spaceBefore=16),
    }

    # ── Status color + icon ──
    status = fax_status_data.get("status") or "Unknown"
    if status == "Sent":
        status_str = "✓ Delivered (Sent)"
        status_color = colors.HexColor("#059669")
    elif status in ("SendingFailed", "Timeout"):
        status_str = f"✗ {status}"
        status_color = colors.HexColor("#dc2626")
    else:
        status_str = status
        status_color = colors.HexColor("#d97706")

    raw = fax_status_data.get("raw") or {}
    to_list = fax_status_data.get("to") or []
    destination = (to_list[0]["phone"] if to_list else None) or "—"
    pages = fax_status_data.get("fax_page_count")
    resolution = fax_status_data.get("fax_resolution")
    delivered_at = fax_status_data.get("last_modified_at")

    # ── Content ──
    story = []
    story.append(Paragraph("Ronexa Fax Confirmation", styles["title"]))
    story.append(Paragraph(
        "Audit-grade record of fax transmission via RingCentral.",
        styles["subtitle"],
    ))

    def _make_table(rows: list[list]) -> Table:
        t = Table(rows, colWidths=[1.9 * inch, 4.6 * inch])
        t.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 4),
            ("RIGHTPADDING", (0, 0), (-1, -1), 4),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ("LINEBELOW", (0, 0), (-1, -1), 0.25, colors.HexColor("#e5e7eb")),
        ]))
        return t

    # Transmission Summary
    story.append(Paragraph("Transmission Summary", styles["section"]))
    status_para = Paragraph(f'<font color="{status_color.hexval()}"><b>{status_str}</b></font>',
                            styles["cell_value"])
    story.append(_make_table([
        [Paragraph("<b>Status</b>", styles["cell_label"]), status_para],
        _row(styles, "Submitted",  _fmt_ct(submitted_at)),
        _row(styles, "Delivered",  _fmt_ct(delivered_at)),
        _row(styles, "Duration",   _fmt_duration(submitted_at, delivered_at)),
        _row(styles, "Pages",      f"{pages} ({resolution or 'High'})" if pages else "—"),
        _row(styles, "Destination", destination),
    ]))

    # Case
    story.append(Spacer(1, 6))
    story.append(Paragraph("Case", styles["section"]))
    patient = f"{case.last_name or ''}, {case.first_name or ''}".strip(", ")
    story.append(_make_table([
        _row(styles, "Patient",            patient),
        _row(styles, "DOB",                case.dob or "—"),
        _row(styles, "Member ID",          case.policy_num or "—"),
        _row(styles, "CPT / ICD",          f"{case.cpt_code or '—'} / {case.icd1 or '—'}"),
        _row(styles, "Center",             case.center_abbr or case.center_npi or "—"),
        _row(styles, "Carelon Order #",    case.auth_number or "—"),
        _row(styles, "Exam ID",            str(case.exam_id) if case.exam_id else "—"),
        _row(styles, "Ronexa Case ID",     case.id or "—"),
    ]))

    # RingCentral
    story.append(Spacer(1, 6))
    story.append(Paragraph("RingCentral", styles["section"]))
    story.append(_make_table([
        _row(styles, "Message ID",  fax_status_data.get("message_id") or "—"),
        _row(styles, "Direction",   raw.get("direction") or "Outbound"),
        _row(styles, "Creation",    _fmt_ct(fax_status_data.get("creation_time"))),
        _row(styles, "Last Update", _fmt_ct(delivered_at)),
    ]))

    # Approval
    story.append(Spacer(1, 6))
    story.append(Paragraph("Approval", styles["section"]))
    story.append(_make_table([
        _row(styles, "Approved by", approved_by or "—"),
        _row(styles, "Approved at", _fmt_ct(approved_at)),
    ]))

    # Clinical Document
    story.append(Spacer(1, 6))
    story.append(Paragraph("Clinical Document", styles["section"]))
    doc_rows = [
        _row(styles, "Blob key", case.clinical_blob_key or case.file_key or "—"),
    ]
    story.append(_make_table(doc_rows))

    # Footer
    story.append(Paragraph(
        f"Generated by Ronexa on {_fmt_ct(datetime.now(timezone.utc))}.",
        styles["footer"],
    ))

    doc.build(story)
    pdf_bytes = buf.getvalue()
    buf.close()
    return pdf_bytes


def upload_fax_confirmation_pdf(
    pdf_bytes: bytes,
    exam_id: str,
    message_id: str,
) -> str:
    """Upload a PDF to Azure Blob and return the blob key.

    Key format: fax-confirmations/{YYYY-MM-DD}/{exam_id}_{message_id}.pdf
    """
    from datetime import date
    from app.ingest.blob_fetcher import _get_container
    from azure.storage.blob import ContentSettings

    blob_key = f"fax-confirmations/{date.today().isoformat()}/{exam_id}_{message_id}.pdf"
    container = _get_container()
    container.get_blob_client(blob_key).upload_blob(
        pdf_bytes,
        overwrite=True,
        content_settings=ContentSettings(content_type="application/pdf"),
    )
    logger.info(f"Uploaded fax confirmation PDF: {blob_key} ({len(pdf_bytes)} bytes)")
    return blob_key
