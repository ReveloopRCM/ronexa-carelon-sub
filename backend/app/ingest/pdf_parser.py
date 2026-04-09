"""PDF parser — extract page images from clinical PDFs.

Handles two types of clinical PDFs:
1. iTextSharp-wrapped faxes (image-only) — extract embedded images directly
2. Regular clinical documents — render pages to images via pymupdf

Pipeline: pymupdf → PNG/JPEG → Claude vision extraction
"""
from __future__ import annotations

import io

import fitz  # pymupdf
from PIL import Image


MIN_IMAGE_SIZE = 200  # Below this width/height, image is a thumbnail


def extract_page_images(pdf_bytes: bytes) -> list[dict]:
    """Extract usable images from each page of a clinical PDF.

    Strategy:
    1. Try extracting embedded images (fast, works for iTextSharp fax PDFs)
    2. If images are too small (thumbnails) or missing, render pages instead

    Returns list of {image_bytes, media_type, page_num, width, height, ...}.
    """
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")

    # First pass: try embedded image extraction
    embedded = _extract_embedded_images(doc)

    # Check if embedded images are usable (not thumbnails)
    if embedded and all(p["width"] >= MIN_IMAGE_SIZE and p["height"] >= MIN_IMAGE_SIZE for p in embedded):
        doc.close()
        return embedded

    # Fallback: render pages as images
    rendered = _render_pages(doc)
    doc.close()
    return rendered


def _extract_embedded_images(doc: fitz.Document) -> list[dict]:
    """Extract embedded images from PDF (works for iTextSharp fax PDFs)."""
    pages = []

    for i, page in enumerate(doc):
        imgs = page.get_images(full=True)
        if not imgs:
            continue

        xref = imgs[0][0]
        info = doc.extract_image(xref)
        img_bytes = info["image"]
        w, h = info["width"], info["height"]

        # Calculate effective DPI from page dimensions
        page_w_in = page.rect.width / 72
        page_h_in = page.rect.height / 72
        dpi_h = w / page_w_in
        dpi_v = h / page_h_in

        # Normalize non-square fax pixels (200x100 DPI standard fax)
        normalized = False
        if dpi_h / dpi_v > 1.3:
            pil_img = Image.open(io.BytesIO(img_bytes))
            new_h = int(pil_img.height * (dpi_h / dpi_v))
            pil_img = pil_img.resize((pil_img.width, new_h), Image.LANCZOS)
            buf = io.BytesIO()
            pil_img.save(buf, format="PNG")
            img_bytes = buf.getvalue()
            normalized = True

        media_type = _detect_media_type(img_bytes)

        pages.append({
            "image_bytes": img_bytes,
            "media_type": media_type,
            "page_num": i + 1,
            "total_pages_in_pdf": len(doc),
            "width": w,
            "height": h,
            "dpi_h": dpi_h,
            "dpi_v": dpi_v,
            "normalized": normalized,
        })

    return pages


def _render_pages(doc: fitz.Document, dpi: int = 200) -> list[dict]:
    """Render PDF pages to images (works for all PDF types)."""
    pages = []
    zoom = dpi / 72
    mat = fitz.Matrix(zoom, zoom)

    for i, page in enumerate(doc):
        pix = page.get_pixmap(matrix=mat)
        img_bytes = pix.tobytes("png")
        w, h = pix.width, pix.height

        pages.append({
            "image_bytes": img_bytes,
            "media_type": "image/png",
            "page_num": i + 1,
            "total_pages_in_pdf": len(doc),
            "width": w,
            "height": h,
            "dpi_h": dpi,
            "dpi_v": dpi,
            "normalized": False,
        })

    return pages


def _detect_media_type(img_bytes: bytes) -> str:
    """Detect image media type from magic bytes."""
    if img_bytes[:3] == b'\xff\xd8\xff':
        return "image/jpeg"
    elif img_bytes[:8] == b'\x89PNG\r\n\x1a\n':
        return "image/png"
    else:
        return "image/png"  # Safe default for Anthropic API
