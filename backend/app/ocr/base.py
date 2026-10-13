"""
Text extraction from uploaded notices.

Two ways to get text out of a document, tried in order of cost and reliability:

1. **PDF text layer.** Most notices are downloaded from the GST portal or
   emailed as digital PDFs, which already contain the text. Reading it is
   instant, free and exact, with no recognition errors to worry about.
2. **OCR.** Needed only for a photograph or a scan, which has pixels but no
   text. Recognition is probabilistic, so a misread digit in a deadline or an
   amount is possible; the result is flagged as OCR so a person knows to
   check it against the original.

Each extractor implements `TextExtractor`. The orchestration lives in
`app.ocr.extract_text`.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional


class ExtractionStatus:
    DONE = "DONE"
    NEEDS_OCR = "NEEDS_OCR"      # no text could be read and OCR is unavailable
    FAILED = "FAILED"            # the file is unreadable (corrupt, password protected)


@dataclass
class ExtractionResult:
    status: str
    text: str = ""
    method: Optional[str] = None        # "PDF_TEXT" | "OCR"
    pages: Optional[int] = None
    message: Optional[str] = None       # a plain-language explanation for the user
    warnings: list[str] = field(default_factory=list)


class TextExtractor(ABC):
    @abstractmethod
    def extract(self, data: bytes) -> ExtractionResult: ...
