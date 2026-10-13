"""OCR for images, via Tesseract.

Optional: neither `pytesseract` / `Pillow` nor the Tesseract program itself
is in requirements.txt. Nothing here is imported until an image arrives, and
when Tesseract is missing the result says so plainly instead of raising, so
an image upload degrades to "paste the text by hand" rather than an error.

To enable:
    1. Install Tesseract (Windows: the UB Mannheim build; Linux: apt install tesseract-ocr)
    2. pip install pytesseract pillow
    3. If the program is not on PATH, set TESSERACT_CMD to its full path.
"""
import io
import logging
import os

from app.ocr.base import ExtractionResult, ExtractionStatus, TextExtractor

logger = logging.getLogger("gstr2b.ocr")

NOT_INSTALLED = (
    "OCR is not set up on this server, so text could not be read from the image. "
    "Paste the text of the notice by hand instead."
)


class TesseractExtractor(TextExtractor):
    def is_available(self) -> bool:
        try:
            import pytesseract
            from PIL import Image  # noqa: F401
        except ImportError:
            return False
        self._configure(pytesseract)
        try:
            pytesseract.get_tesseract_version()
        except Exception:
            return False
        return True

    @staticmethod
    def _configure(pytesseract) -> None:
        cmd = os.environ.get("TESSERACT_CMD")
        if cmd:
            pytesseract.pytesseract.tesseract_cmd = cmd

    def extract(self, data: bytes) -> ExtractionResult:
        if not self.is_available():
            return ExtractionResult(status=ExtractionStatus.NEEDS_OCR, message=NOT_INSTALLED)

        import pytesseract
        from PIL import Image

        try:
            with Image.open(io.BytesIO(data)) as img:
                img.load()
                text = pytesseract.image_to_string(img.convert("L"), lang="eng")
        except Exception as exc:
            logger.warning("OCR failed: %s", exc)
            return ExtractionResult(
                status=ExtractionStatus.FAILED,
                message="The image could not be read. Try a clearer or larger copy.",
            )
        text = (text or "").strip()
        if len(text) < 25:
            return ExtractionResult(
                status=ExtractionStatus.NEEDS_OCR, method="OCR", pages=1,
                message="OCR found almost no text in the image. Paste the text of the "
                        "notice by hand.",
            )
        return ExtractionResult(
            status=ExtractionStatus.DONE, text=text, method="OCR", pages=1,
            warnings=["Text was recognised from an image and may contain errors. "
                      "Check dates and amounts against the original."],
        )
