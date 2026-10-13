"""Exact-name related-party cross-reference; it raises review flags only."""
import re
from datetime import date

from app import audit, models
from app.audit import AuditAction
from app.tenancy import TenantSession


def normalize_party_name(value: str | None) -> str:
    """Normalize spacing/case/punctuation while retaining exact token equality."""
    if not value:
        return ""
    return re.sub(r"[^\w]+", "", " ".join(value.casefold().split()), flags=re.UNICODE)


def _financial_year_end(financial_year: str) -> date:
    start, end = (int(piece) for piece in financial_year.split("-"))
    return date((start // 100) * 100 + end if end > 99 else (start // 100) * 100 + end, 3, 31)


def recheck_engagement(tenant: TenantSession, engagement: models.AuditEngagement,
                       actor: models.User) -> dict:
    """Create one permanent SYSTEM_FLAGGED item per exact master/source match."""
    at_date = _financial_year_end(engagement.financial_year)
    parties = tenant.query(models.RelatedParty).filter(
        models.RelatedParty.client_id == engagement.client_id,
        models.RelatedParty.is_active.is_(True),
    ).all()
    party_by_name = {}
    for party in parties:
        if party.effective_from and party.effective_from > at_date:
            continue
        if party.effective_to and party.effective_to < date(at_date.year - 1, 4, 1):
            continue
        for candidate in [party.name, *(party.aliases or [])]:
            normalized = normalize_party_name(candidate)
            if normalized:
                party_by_name.setdefault(normalized, party)

    sources = []
    closing = tenant.query(models.TrialBalanceUpload).filter(
        models.TrialBalanceUpload.engagement_id == engagement.id,
        models.TrialBalanceUpload.period_type == models.TrialBalancePeriodType.CLOSING,
        models.TrialBalanceUpload.is_current.is_(True),
    ).order_by(models.TrialBalanceUpload.version.desc()).first()
    if closing:
        for line in tenant.query(models.TrialBalanceLine).filter(
            models.TrialBalanceLine.upload_id == closing.id,
        ).all():
            sources.append(("TRIAL_BALANCE", line.id, line.account_name,
                            line.current_year_amount, line.id))
    ledger = tenant.query(models.LedgerUpload).filter(
        models.LedgerUpload.engagement_id == engagement.id,
        models.LedgerUpload.is_current.is_(True),
    ).order_by(models.LedgerUpload.version.desc()).first()
    if ledger:
        for line in tenant.query(models.LedgerLine).filter(
            models.LedgerLine.upload_id == ledger.id,
        ).all():
            sources.append(("LEDGER", line.id, line.party_name, line.open_balance, None))

    created = []
    matched = 0
    for source_type, source_id, source_name, amount, tb_line_id in sources:
        party = party_by_name.get(normalize_party_name(source_name))
        if not party:
            continue
        matched += 1
        source_key = f"RELATED:{party.id}:{source_type}:{source_id}"
        existing = tenant.query(models.RiskFlag).filter(
            models.RiskFlag.engagement_id == engagement.id,
            models.RiskFlag.source_reference_key == source_key,
        ).first()
        if existing:
            continue
        note = (
            f"SYSTEM FLAG — {source_type.replace('_', ' ').title()} entry '{source_name}' "
            f"matches related-party master '{party.name}' ({party.relationship_nature}); "
            f"recorded amount/balance: {amount if amount is not None else 'not available'}. "
            "Mandatory CA review irrespective of amount. Exact name/alias matching is not "
            "a conclusion on relationship classification or disclosure adequacy."
        )
        flag = models.RiskFlag(
            engagement_id=engagement.id,
            trial_balance_line_id=tb_line_id,
            source_reference_key=source_key,
            policy_version=None,
            flag_type=models.RiskFlagType.RELATED_PARTY,
            severity="REVIEW_REQUIRED",
            system_note=note,
            trigger_data={"subtype": "RELATED_PARTY_EXACT_MATCH", "related_party_id": party.id,
                          "source_type": source_type, "source_id": source_id,
                          "source_name": source_name, "matched_master_name": party.name,
                          "match_method": "NORMALIZED_EXACT_NAME_OR_ALIAS",
                          "amount": str(amount) if amount is not None else None},
            status=models.RiskFlagStatus.SYSTEM_FLAGGED,
        )
        tenant.add(flag)
        tenant.flush()
        audit.record(tenant.db, firm_id=tenant.firm_id, user_id=actor.id,
                     action=AuditAction.AUDIT_RISK_FLAG_SYSTEM_GENERATED,
                     entity_type="RiskFlag", entity_id=flag.id,
                     metadata={"engagement_id": engagement.id, "source_reference_key": source_key,
                               "related_party_id": party.id, "source_type": source_type,
                               "source_id": source_id, "status": flag.status.value,
                               "system_note": note})
        created.append(flag.id)
    return {"engagement_id": engagement.id, "matched_sources": matched,
            "flags_created": len(created), "risk_flag_ids": created}
