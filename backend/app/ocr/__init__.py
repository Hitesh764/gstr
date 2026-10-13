"""Extraction entry point: pick the right reader for the file."""
from typing import Optional

from app.ocr.base import ExtractionResult, ExtractionStatus, TextExtractor
from app.ocr.pdf_text import PdfTextExtractor
from app.ocr.tesseract import TesseractExtractor

__all__ = ["ExtractionResult", "ExtractionStatus", "TextExtractor", "extract_text",
           "sniff_content_type", "PdfTextExtractor", "TesseractExtractor"]

_SIGNATURES = (
    (b"%PDF-", "application/pdf"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
)


def sniff_content_type(data: bytes) -> Optional[str]:
    """What the bytes actually are, ignoring the filename and the client's
    claimed Content-Type, both of which an uploader controls."""
    head = data[:16]
    for magic, content_type in _SIGNATURES:
        if head.startswith(magic):
            return content_type
    return None


def extract_text(
    data: bytes, content_type: str, *,
    pdf: Optional[TextExtractor] = None, ocr: Optional[TextExtractor] = None,
) -> ExtractionResult:
    """Get text out of a stored notice. Extractors are injectable for tests."""
    if content_type == "application/pdf":
        return (pdf or PdfTextExtractor()).extract(data)
    if content_type in ("image/png", "image/jpeg"):
        return (ocr or TesseractExtractor()).extract(data)
    return ExtractionResult(
        status=ExtractionStatus.FAILED,
        message=f"Cannot read text from a {content_type or 'unknown'} file.",
    )
