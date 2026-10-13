"""Notice analysers. `get_notice_analyzer()` returns the configured one."""
import logging
from functools import lru_cache

from app.config import settings
from app.notice_ai.base import (
    NOTICE_TYPES, AnalysisError, DraftResult, EvidenceItem, EvidencePack,
    NoticeAnalyzer, NoticeFacts,
)
from app.notice_ai.mock import MockNoticeAnalyzer

__all__ = [
    "NOTICE_TYPES", "AnalysisError", "DraftResult", "EvidenceItem", "EvidencePack",
    "NoticeAnalyzer", "NoticeFacts", "MockNoticeAnalyzer", "get_notice_analyzer",
]

logger = logging.getLogger("gstr2b.notice_ai")


@lru_cache
def get_notice_analyzer() -> NoticeAnalyzer:
    provider = (settings.notice_ai_provider or "mock").lower()

    if provider == "mock":
        if settings.environment != "development":
            # A keyword matcher reading a real client's statutory notice, in a
            # deployed environment, would produce confident nonsense.
            raise AnalysisError(
                "NOTICE_AI_PROVIDER=mock is only allowed when ENVIRONMENT=development. "
                "Set NOTICE_AI_PROVIDER=claude and ANTHROPIC_API_KEY.",
                code="NOT_CONFIGURED",
            )
        return MockNoticeAnalyzer()

    if provider == "claude":
        from app.notice_ai.claude import ClaudeNoticeAnalyzer
        return ClaudeNoticeAnalyzer(
            api_key=settings.anthropic_api_key,
            model=settings.notice_ai_model,
            timeout=settings.notice_ai_timeout_seconds,
            max_tokens=settings.notice_ai_max_tokens,
        )

    raise AnalysisError(
        f"Unknown NOTICE_AI_PROVIDER {settings.notice_ai_provider!r}. Use 'claude' or 'mock'.",
        code="NOT_CONFIGURED",
    )
