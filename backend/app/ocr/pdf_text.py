"""Read the text layer of a digital PDF, using pypdf (pure Python)."""
import io
import logging

from pypdf import PdfReader
from pypdf.errors import PdfReadError

from app.ocr.base import ExtractionResult, ExtractionStatus, TextExtractor

logger = logging.getLogger("gstr2b.ocr")

# A page that yields fewer characters than this is treated as image-only. A
# scanned page can still carry a few stray characters (a stamped page number,
# a watermark), so "any text at all" is the wrong test.
MIN_CHARS_PER_PAGE = 25


class PdfTextExtractor(TextExtractor):
    def extract(self, data: bytes) -> ExtractionResult:
        try:
            reader = PdfReader(io.BytesIO(data))
            if reader.is_encrypted:
                # Portal PDFs are sometimes "encrypted" with an empty user
                # password purely to restrict copying. Try that first.
                try:
                    opened = reader.decrypt("")
                except Exception:
                    opened = 0
                if not opened:
                    return ExtractionResult(
                        status=ExtractionStatus.FAILED,
                        message="This PDF is password protected. Remove the password "
                                "and upload it again.",
                    )
            pages = len(reader.pages)
            chunks: list[str] = []
            for page in reader.pages:
                try:
                    chunks.append(page.extract_text() or "")
                except Exception as exc:   # one bad page should not sink the rest
                    logger.warning("Could not read a PDF page: %s", exc)
                    chunks.append("")
        except (PdfReadError, ValueError, OSError) as exc:
            return ExtractionResult(
                status=ExtractionStatus.FAILED,
                message=f"This file could not be read as a PDF ({exc}).",
            )
        except Exception as exc:  # pypdf raises assorted types on malformed input
            logger.warning("Unexpected PDF read failure: %s", exc)
            return ExtractionResult(
                status=ExtractionStatus.FAILED,
                message="This file could not be read as a PDF.",
            )

        text = "\n\n".join(c.strip() for c in chunks if c.strip())
        readable = len("".join(text.split()))
        if pages == 0 or readable < MIN_CHARS_PER_PAGE * max(pages, 1):
            return ExtractionResult(
                status=ExtractionStatus.NEEDS_OCR, text=text, method="PDF_TEXT",
                pages=pages,
                message="This PDF looks like a scan: it has no readable text layer. "
                        "Scanned PDFs need OCR, which is not set up. Paste the text "
                        "of the notice by hand, or upload a digital copy.",
            )
        return ExtractionResult(
            status=ExtractionStatus.DONE, text=text, method="PDF_TEXT", pages=pages,
        )
