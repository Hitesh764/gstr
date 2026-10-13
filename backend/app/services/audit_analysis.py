"""Audit drafting service with a non-weakenable professional-judgment boundary."""
import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

from app.config import settings
from app.services.caro_responses import make_draft

SYSTEM_INSTRUCTION = """You assist an Indian statutory audit team. Produce only a clearly labelled DRAFT observation or suggested wording for a Chartered Accountant to review. Never make, imply, or answer a CARO conclusion; never state that a clause is complied with or not complied with; never state that no arrears, fraud, or going-concern issue exists. Do not infer facts, evidence completeness, causes, legal applicability, fraud, or going concern. Distinguish facts explicitly present in the supplied records from missing information. Use cautious, source-attributed wording and say when data is not recorded in the supplied system snapshot. The CA alone decides the audit conclusion. Return only the proposed draft text, prefixed exactly with 'SYSTEM DRAFT —'."""

_CONCLUSION_PATTERNS = (
    r"\bwe conclude\b", r"\bin our opinion\b", r"\bfully compliant\b",
    r"\bhas complied with all\b", r"\bno arrears\b", r"\bno fraud\b",
    r"\b(is|remains) a going concern\b", r"\bnot a going concern\b",
    r"\btherefore the clause is complied\b", r"\bclause is not applicable\b",
)


class AuditAnalysisError(RuntimeError):
    """A generated response cannot safely be used as a draft."""

    def __init__(self, message: str, attempts: list["AuditAIAttempt"] | None = None):
        super().__init__(message)
        self.attempts = attempts or []


@dataclass(frozen=True)
class AuditAIAttempt:
    provider: str
    model: str
    exact_prompt: str
    exact_response: str
    accepted: bool
    response_sha256: str


@dataclass(frozen=True)
class AuditDraftResult:
    narrative: str
    attempts: list[AuditAIAttempt]


def _response_text(response: Any) -> str:
    blocks = getattr(response, "content", None) or []
    return "\n".join(str(block.text) for block in blocks if getattr(block, "text", None)).strip()


def _rejected(text: str) -> bool:
    # Enforce the persistent draft marker while avoiding locale/encoding
    # dependence on the typographic dash that follows it in generated text.
    return not text.startswith("SYSTEM DRAFT") or any(re.search(pattern, text, re.I) for pattern in _CONCLUSION_PATTERNS)


class AuditAnalysisService:
    """Draft CARO observations. Required system instruction is centralized here."""

    def __init__(self, *, provider: str | None = None, model: str | None = None,
                 api_key: str | None = None, client: Any = None):
        self.provider = (provider or settings.audit_ai_provider).lower()
        self.model = model or settings.audit_ai_model
        self.api_key = settings.anthropic_api_key if api_key is None else api_key
        self._client = client

    def _claude(self):
        if self._client is None:
            if not self.api_key:
                raise AuditAnalysisError("AUDIT_AI_PROVIDER=claude requires ANTHROPIC_API_KEY.")
            import anthropic
            self._client = anthropic.Anthropic(
                api_key=self.api_key, timeout=settings.audit_ai_timeout_seconds, max_retries=1,
            )
        return self._client

    def draft_caro(self, *, clause_number: int, title: str, clause_text: str,
                   source_snapshot: dict) -> AuditDraftResult:
        evidence = json.dumps(source_snapshot, ensure_ascii=False, sort_keys=True, default=str)
        user_prompt = (
            f"Draft cautious review language for CARO clause {clause_number}: {title}.\n"
            f"Versioned clause text (context only): {clause_text}\n"
            f"Recorded evidence snapshot (not independently verified): {evidence}\n\n"
            "Describe only what these records contain. Do not answer the clause or draw a conclusion. "
            "If no relevant record is supplied, state that this system snapshot has no such record and "
            "the CA must verify the audit file. Keep it concise."
        )
        if self.provider == "template":
            text = make_draft(clause_number, title, clause_text, source_snapshot)
            prompt = json.dumps({"system": SYSTEM_INSTRUCTION, "user": user_prompt}, ensure_ascii=False, separators=(",", ":"))
            attempt = self._attempt("template", "deterministic-template-v1", prompt, text, accepted=True)
            return AuditDraftResult(text, [attempt])
        if self.provider != "claude":
            raise AuditAnalysisError("AUDIT_AI_PROVIDER must be 'template' or 'claude'.")

        attempts: list[AuditAIAttempt] = []
        retry_note = ""
        for index in range(1, 3):
            actual_user_prompt = user_prompt + retry_note
            exact_prompt = json.dumps(
                {"system": SYSTEM_INSTRUCTION, "user": actual_user_prompt},
                ensure_ascii=False, separators=(",", ":"),
            )
            try:
                response = self._claude().messages.create(
                    model=self.model,
                    max_tokens=settings.audit_ai_max_tokens,
                    system=SYSTEM_INSTRUCTION,
                    messages=[{"role": "user", "content": actual_user_prompt}],
                )
            except Exception as exc:
                raise AuditAnalysisError("Claude audit drafting failed; no draft was saved.") from exc
            text = _response_text(response)
            accepted = not _rejected(text)
            attempts.append(self._attempt("claude", self.model, exact_prompt, text, accepted))
            if accepted:
                return AuditDraftResult(text, attempts)
            retry_note = (
                "\n\nREGENERATION REQUIRED: The previous draft was rejected because it did not "
                "follow the no-conclusion drafting rule. Rewrite as a source-attributed observation "
                "prefixed 'SYSTEM DRAFT —'. Do not state whether the clause is complied with, whether "
                "arrears/fraud exist, or whether the entity is a going concern."
            )
        raise AuditAnalysisError("Claude produced conclusion-like CARO language twice; no draft was accepted.", attempts)

    @staticmethod
    def _attempt(provider: str, model: str, prompt: str, response: str, accepted: bool) -> AuditAIAttempt:
        return AuditAIAttempt(provider, model, prompt, response, accepted,
                              hashlib.sha256(response.encode("utf-8")).hexdigest())
